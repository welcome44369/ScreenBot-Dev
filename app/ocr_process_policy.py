"""Project-owned process policy for Tesseract command-line invocations."""
from __future__ import annotations

from dataclasses import dataclass
from errno import ENOENT
import os
import shlex
import string
import subprocess
import sys
import threading

from packaging.version import InvalidVersion, parse
import pytesseract


@dataclass(frozen=True)
class TesseractLaunch:
    """Resolved command and platform-specific subprocess arguments."""

    command: tuple[str, ...]
    kwargs: dict


class TesseractProcessPolicy:
    """Launch only Tesseract with ScreenBot's fail-closed Windows policy.

    This class deliberately owns the three Tesseract subprocess paths used by
    ScreenBot.  It does not patch ``subprocess`` or pytesseract globals.
    """

    def __init__(
        self,
        *,
        tesseract_cmd=None,
        platform=None,
        popen=None,
        environment=None,
        creationflags=0,
    ):
        self.tesseract_cmd = str(
            tesseract_cmd or pytesseract.pytesseract.tesseract_cmd
        )
        self._platform = platform or sys.platform
        # Resolve the default at launch time so the existing opt-in diagnostic
        # observer can wrap subprocess creation during a one-shot probe.
        self._popen = popen
        self._environment = environment if environment is not None else os.environ
        self._creationflags = int(creationflags or 0)
        self._cache_lock = threading.Lock()
        self._languages = None
        self._version = None

    @property
    def is_windows(self):
        return self._platform == "win32"

    def _launch_kwargs(self, *, capture_stdout=True, merge_stderr=False):
        kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            "stderr": subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            "env": self._environment,
            "shell": False,
        }
        if not self.is_windows:
            return kwargs

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = (
            self._creationflags
            | int(subprocess.CREATE_NO_WINDOW)
        )
        return kwargs

    def prepare_launch(
        self,
        arguments,
        *,
        capture_stdout=True,
        merge_stderr=False,
    ):
        command = (self.tesseract_cmd, *(str(value) for value in arguments))
        return TesseractLaunch(
            command=command,
            kwargs=self._launch_kwargs(
                capture_stdout=capture_stdout,
                merge_stderr=merge_stderr,
            ),
        )

    def _start(
        self,
        arguments,
        *,
        capture_stdout=True,
        merge_stderr=False,
    ):
        launch = self.prepare_launch(
            arguments,
            capture_stdout=capture_stdout,
            merge_stderr=merge_stderr,
        )
        try:
            popen = self._popen or subprocess.Popen
            return popen(list(launch.command), **launch.kwargs)
        except OSError as exc:
            if exc.errno == ENOENT:
                raise pytesseract.pytesseract.TesseractNotFoundError() from exc
            raise

    @staticmethod
    def _terminate_after_timeout(process):
        process.terminate()
        try:
            process.wait(timeout=1)
        except Exception:
            process.kill()
            process.wait()

    def _communicate(self, process, timeout=0):
        try:
            return process.communicate(timeout=timeout or None)
        except subprocess.TimeoutExpired as exc:
            self._terminate_after_timeout(process)
            raise RuntimeError("Tesseract process timeout") from exc

    @staticmethod
    def _raise_for_error(process, error_bytes):
        if process.returncode:
            raise pytesseract.pytesseract.TesseractError(
                process.returncode,
                pytesseract.pytesseract.get_errors(error_bytes or b""),
            )

    def get_languages(self, config=""):
        with self._cache_lock:
            if self._languages is not None:
                return list(self._languages)

            arguments = ["--list-langs"]
            if config:
                arguments.extend(
                    shlex.split(config, posix=not self.is_windows)
                )
            process = self._start(arguments, merge_stderr=True)
            output, _ = self._communicate(process)
            if process.returncode not in (0, 1):
                raise pytesseract.pytesseract.TesseractNotFoundError()

            languages = []
            for line in (output or b"").decode(
                pytesseract.pytesseract.DEFAULT_ENCODING
            ).splitlines():
                language = line.strip()
                if pytesseract.pytesseract.LANG_PATTERN.match(language):
                    languages.append(language)
            self._languages = tuple(languages)
            return list(self._languages)

    def get_version(self):
        with self._cache_lock:
            if self._version is not None:
                return self._version

            process = self._start(["--version"], merge_stderr=True)
            output, _ = self._communicate(process)
            self._raise_for_error(process, output)
            raw_version = (output or b"").decode(
                pytesseract.pytesseract.DEFAULT_ENCODING
            )
            version_text, *_ = raw_version.lstrip(
                string.printable[10:]
            ).partition(" ")
            version_text, *_ = version_text.partition("-")
            try:
                version = parse(version_text)
                if version < pytesseract.pytesseract.TESSERACT_MIN_VERSION:
                    raise InvalidVersion(version_text)
            except InvalidVersion as exc:
                raise SystemExit(
                    f'Invalid tesseract version: "{raw_version}"'
                ) from exc
            self._version = version
            return version

    def image_to_data(self, image, *, lang=None, config="", timeout=0):
        if self.get_version() < pytesseract.pytesseract.TESSERACT_MIN_VERSION:
            raise pytesseract.pytesseract.TSVNotSupported()

        with pytesseract.pytesseract.save(image) as (
            output_base,
            input_filename,
        ):
            arguments = [input_filename, output_base]
            if lang is not None:
                arguments.extend(["-l", lang])
            tsv_config = f"-c tessedit_create_tsv=1 {config.strip()}".strip()
            arguments.extend(
                shlex.split(tsv_config, posix=not self.is_windows)
            )

            process = self._start(arguments)
            _, error_bytes = self._communicate(process, timeout=timeout)
            self._raise_for_error(process, error_bytes)

            output_path = f"{output_base}{os.extsep}tsv"
            with open(output_path, "rb") as output_file:
                tsv = output_file.read().decode(
                    pytesseract.pytesseract.DEFAULT_ENCODING
                )
            return pytesseract.pytesseract.file_to_dict(tsv, "\t", -1)
