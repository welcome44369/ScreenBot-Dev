"""Focused regression tests for ScreenBot's Tesseract launch policy."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ocr_process_policy import TesseractProcessPolicy


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, timeout=False):
        self.stdout_value = stdout
        self.stderr_value = stderr
        self.returncode = returncode
        self.timeout = timeout
        self.terminated = False
        self.killed = False

    def communicate(self, timeout=None):
        if self.timeout:
            raise subprocess.TimeoutExpired("tesseract", timeout)
        return self.stdout_value, self.stderr_value

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("tesseract", timeout)
        return self.returncode

    def kill(self):
        self.killed = True


class RecordingPopen:
    def __init__(self, processes, output_tsv=None):
        self.processes = list(processes)
        self.calls = []
        self.output_tsv = output_tsv

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.output_tsv is not None and len(command) >= 3:
            Path(f"{command[2]}.tsv").write_bytes(
                self.output_tsv.encode("utf-8")
            )
        return self.processes.pop(0)


class TesseractProcessPolicyTest(unittest.TestCase):
    def test_windows_launch_uses_hidden_startup_and_no_window(self):
        recorder = RecordingPopen([FakeProcess()])
        policy = TesseractProcessPolicy(
            tesseract_cmd=r"C:\Tesseract\tesseract.exe",
            platform="win32",
            popen=recorder,
            creationflags=0x00000200,
        )
        launch = policy.prepare_launch(["--version"])
        self.assertEqual(
            launch.kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW,
            subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(launch.kwargs["creationflags"] & 0x00000200, 0x00000200)
        self.assertTrue(
            launch.kwargs["startupinfo"].dwFlags
            & subprocess.STARTF_USESHOWWINDOW
        )
        self.assertEqual(launch.kwargs["startupinfo"].wShowWindow, subprocess.SW_HIDE)
        self.assertFalse(launch.kwargs["shell"])

    def test_non_windows_launch_does_not_add_windows_kwargs(self):
        policy = TesseractProcessPolicy(
            tesseract_cmd="/usr/bin/tesseract",
            platform="linux",
            popen=RecordingPopen([]),
        )
        launch = policy.prepare_launch(["--version"])
        self.assertNotIn("creationflags", launch.kwargs)
        self.assertNotIn("startupinfo", launch.kwargs)
        self.assertFalse(launch.kwargs["shell"])

    def test_languages_are_parsed_and_cached(self):
        recorder = RecordingPopen(
            [FakeProcess(b"List of available languages (2):\r\neng\r\nchi_tra\r\n")]
        )
        policy = TesseractProcessPolicy(platform="win32", popen=recorder)
        self.assertEqual(policy.get_languages(), ["eng", "chi_tra"])
        self.assertEqual(policy.get_languages(), ["eng", "chi_tra"])
        self.assertEqual(len(recorder.calls), 1)

    def test_nonzero_ocr_return_preserves_tesseract_error(self):
        recorder = RecordingPopen(
            [
                FakeProcess(b"tesseract 5.4.0\r\n"),
                FakeProcess(stderr=b"bad image\r\n", returncode=2),
            ]
        )
        policy = TesseractProcessPolicy(platform="win32", popen=recorder)
        with self.assertRaises(Exception) as context:
            policy.image_to_data(Image.new("RGB", (2, 2), "white"))
        self.assertEqual(getattr(context.exception, "status", None), 2)
        self.assertIn("bad image", str(getattr(context.exception, "message", "")))

    def test_timeout_terminates_and_kills_child(self):
        process = FakeProcess(timeout=True)
        policy = TesseractProcessPolicy(
            platform="win32",
            popen=RecordingPopen([process]),
        )
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            policy._communicate(process, timeout=0.01)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)

    def test_image_to_data_preserves_tsv_dictionary_contract(self):
        tsv = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
            "left\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t2\t3\t4\t5\t96.0\tapple\n"
        )
        recorder = RecordingPopen(
            [
                FakeProcess(b"tesseract 5.4.0\r\n"),
                FakeProcess(),
            ],
            output_tsv=tsv,
        )
        policy = TesseractProcessPolicy(platform="win32", popen=recorder)

        def fake_save(_image):
            class SaveContext:
                def __enter__(self):
                    self.root = tempfile.TemporaryDirectory()
                    base = str(Path(self.root.name) / "result")
                    return base, str(Path(self.root.name) / "input.png")

                def __exit__(self, *_args):
                    self.root.cleanup()

            return SaveContext()

        with patch("app.ocr_process_policy.pytesseract.pytesseract.save", fake_save):
            result = policy.image_to_data(
                Image.new("RGB", (2, 2), "white"),
                lang="eng",
                config="--psm 6",
            )
        self.assertIn("text", result, (result, recorder.calls))
        self.assertEqual(result["text"], ["apple"])
        command, kwargs = recorder.calls[-1]
        self.assertIn("-l", command)
        self.assertIn("eng", command)
        self.assertIn("tessedit_create_tsv=1", command)
        self.assertIn("--psm", command)
        self.assertFalse(kwargs["shell"])

    def test_policy_does_not_patch_global_subprocess_functions(self):
        source = (
            PROJECT_ROOT / "app" / "ocr_process_policy.py"
        ).read_text(encoding="utf-8-sig")
        self.assertNotIn("subprocess.Popen =", source)
        self.assertNotIn("pytesseract.pytesseract.subprocess =", source)
        self.assertNotIn("SetForegroundWindow", source)
        self.assertNotIn("HWND_TOPMOST", source)

    def test_default_popen_is_resolved_at_launch_for_gated_diagnostics(self):
        policy = TesseractProcessPolicy(platform="win32")
        process = FakeProcess(b"List of available languages (1):\r\neng\r\n")
        with patch(
            "app.ocr_process_policy.subprocess.Popen",
            return_value=process,
        ) as observed:
            self.assertEqual(policy.get_languages(), ["eng"])
        observed.assert_called_once()
        self.assertEqual(
            observed.call_args.kwargs["creationflags"]
            & subprocess.CREATE_NO_WINDOW,
            subprocess.CREATE_NO_WINDOW,
        )


if __name__ == "__main__":
    unittest.main()
