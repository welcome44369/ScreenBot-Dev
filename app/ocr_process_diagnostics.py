"""Environment-gated, input-free attribution for one OCR observation.

The diagnostic deliberately observes the existing pytesseract launch path.
It never changes OCR arguments, startup flags, creation flags, target window
state, or application foreground state.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4

import psutil
import pytesseract

from app.overlay_diagnostics import EVENT_NAMES, OverlayDiagnosticWin32Adapter


_POPEN_PATCH_LOCK = threading.Lock()
_EVENT_SYSTEM_FOREGROUND = 0x0003
_EVENT_OBJECT_CREATE = 0x8000
_EVENT_OBJECT_DESTROY = 0x8001
_EVENT_OBJECT_SHOW = 0x8002
_EVENT_OBJECT_HIDE = 0x8003
_WINEVENT_OUTOFCONTEXT = 0x0000
_PM_REMOVE = 0x0001
_OCR_EVENT_NAMES = {
    **EVENT_NAMES,
    _EVENT_OBJECT_CREATE: "EVENT_OBJECT_CREATE",
}
_PROCESS_NAMES = {
    "cmd.exe",
    "conhost.exe",
    "openconsole.exe",
    "powershell.exe",
    "pwsh.exe",
    "python.exe",
    "pythonw.exe",
    "tesseract.exe",
}


def _utc_now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _json_safe(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return repr(value)


class OcrProcessWin32Adapter(OverlayDiagnosticWin32Adapter):
    """Read-only WinEvent subset required by the bounded OCR probe."""

    def __init__(self):
        super().__init__()
        self._hook_thread = None
        self._hook_ready = threading.Event()
        self._hook_stop = threading.Event()
        self._hook_handles = ()

    def install_win_event_hook(self, callback):
        if not self.user32:
            return None
        self.uninstall_win_event_hook(self._hook_handles)
        self._hook_ready.clear()
        self._hook_stop.clear()
        self._hook_thread = threading.Thread(
            target=self._win_event_loop,
            args=(callback,),
            name="ScreenBotOcrWinEventPump",
            daemon=True,
        )
        self._hook_thread.start()
        self._hook_ready.wait(timeout=1.0)
        return self._hook_handles

    def _win_event_loop(self, callback):
        callback_type = ctypes.WINFUNCTYPE(
            None,
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.HWND,
            ctypes.c_long,
            ctypes.c_long,
            wintypes.DWORD,
            wintypes.DWORD,
        )

        @callback_type
        def hook(_handle, event, hwnd, object_id, child_id, event_thread, event_time):
            callback(
                {
                    "event": int(event),
                    "hwnd": int(hwnd or 0),
                    "object_id": int(object_id),
                    "child_id": int(child_id),
                    "event_thread": int(event_thread),
                    "event_time": int(event_time),
                }
            )

        self._hook_callback = hook
        self.user32.SetWinEventHook.argtypes = (
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HMODULE,
            callback_type,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        self.user32.SetWinEventHook.restype = wintypes.HANDLE
        self.user32.UnhookWinEvent.argtypes = (wintypes.HANDLE,)
        self.user32.UnhookWinEvent.restype = wintypes.BOOL
        self.user32.PeekMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        )
        self.user32.PeekMessageW.restype = wintypes.BOOL
        self.user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        self.user32.TranslateMessage.restype = wintypes.BOOL
        self.user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        self.user32.DispatchMessageW.restype = wintypes.LRESULT
        handles = []
        try:
            for event in (
                _EVENT_SYSTEM_FOREGROUND,
                _EVENT_OBJECT_CREATE,
                _EVENT_OBJECT_DESTROY,
                _EVENT_OBJECT_SHOW,
                _EVENT_OBJECT_HIDE,
            ):
                handle = self.user32.SetWinEventHook(
                    event,
                    event,
                    None,
                    hook,
                    0,
                    0,
                    _WINEVENT_OUTOFCONTEXT,
                )
                if handle:
                    handles.append(handle)
            self._hook_handles = tuple(handles)
            self._hook_ready.set()
            message = wintypes.MSG()
            while not self._hook_stop.wait(0.005):
                while self.user32.PeekMessageW(
                    ctypes.byref(message),
                    None,
                    0,
                    0,
                    _PM_REMOVE,
                ):
                    self.user32.TranslateMessage(ctypes.byref(message))
                    self.user32.DispatchMessageW(ctypes.byref(message))
        finally:
            for handle in handles:
                self.user32.UnhookWinEvent(handle)
            self._hook_handles = ()
            self._hook_callback = None
            self._hook_ready.set()

    def uninstall_win_event_hook(self, _handles):
        self._hook_stop.set()
        thread = self._hook_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._hook_thread = None

    def lightweight_window_state(self, hwnd):
        if not self.user32 or not hwnd:
            return None
        pid = wintypes.DWORD()
        thread_id = self.user32.GetWindowThreadProcessId(
            wintypes.HWND(int(hwnd)),
            ctypes.byref(pid),
        )
        return {
            "hwnd": int(hwnd),
            "pid": int(pid.value),
            "thread_id": int(thread_id),
            "is_visible": bool(
                self.user32.IsWindowVisible(wintypes.HWND(int(hwnd)))
            ),
        }


class OcrProcessDiagnostics:
    """Own a single bounded probe and its observation-only instrumentation."""

    def __init__(self, root_path, logger=None, enabled=None, adapter=None):
        self.root_path = Path(root_path)
        self.logger = logger or logging.getLogger("ScreenBot.OcrProcessDiagnostics")
        self.enabled = (
            os.environ.get("SCREENBOT_OCR_PROCESS_DIAGNOSTIC") == "1"
            if enabled is None
            else bool(enabled)
        )
        self.adapter = adapter or OcrProcessWin32Adapter()
        self._state_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._active = False
        self._stop_event = threading.Event()
        self._thread_local = threading.local()
        self._jsonl_path = None
        self._text_path = None
        self._probe_id = None
        self._observation_id = None
        self._variant_counter = 0
        self._variant_by_token = {}
        self._process_started = {}
        self._event_hook_handles = ()
        self._sampler_thread = None
        self._window_sampler_thread = None
        self._process_watchers = []
        self._baseline_pids = set()
        self._deadline = None
        self._consumed = False

    @property
    def active(self):
        with self._state_lock:
            return self._active

    @property
    def consumed(self):
        with self._state_lock:
            return self._consumed

    @property
    def log_paths(self):
        return {
            "jsonl": str(self._jsonl_path) if self._jsonl_path else None,
            "text": str(self._text_path) if self._text_path else None,
        }

    def _open_log(self):
        directory = self.root_path / "logs" / "ocr_process_diagnostics"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self._jsonl_path = directory / f"ocr_process_{stamp}.jsonl"
        self._text_path = directory / f"ocr_process_{stamp}.log"

    def record(self, event, **payload):
        if not self.enabled or self._jsonl_path is None:
            return
        record = {
            "timestamp": _utc_now(),
            "monotonic_ns": time.monotonic_ns(),
            "event": str(event),
            "probe_id": self._probe_id,
            "observation_id": self._observation_id,
            **payload,
        }
        safe = _json_safe(record)
        line = json.dumps(safe, ensure_ascii=False, sort_keys=True)
        summary = (
            f"{safe['timestamp']} {safe['event']} "
            f"probe={safe.get('probe_id')} observation={safe.get('observation_id')} "
            f"{json.dumps({key: value for key, value in safe.items() if key not in {'timestamp', 'monotonic_ns', 'event', 'probe_id', 'observation_id'}}, ensure_ascii=False, sort_keys=True)}"
        )
        try:
            with self._write_lock:
                with self._jsonl_path.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
                with self._text_path.open("a", encoding="utf-8") as stream:
                    stream.write(summary + "\n")
        except OSError:
            self.logger.exception("OCR process diagnostic log write failed")

    def variant_started(self, variant_id, **metadata):
        if not self.active:
            return None
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise TimeoutError("The one-shot OCR diagnostic exceeded 12 seconds")
        with self._state_lock:
            self._variant_counter += 1
            token = f"variant-{self._variant_counter}"
            entry = {
                "variant_id": str(variant_id),
                "variant_index": self._variant_counter,
                "started_monotonic_ns": time.monotonic_ns(),
                **metadata,
            }
            self._variant_by_token[token] = entry
            self._thread_local.variant_token = token
        self.record("OCR_VARIANT_STARTED", variant_token=token, **entry)
        return token

    def variant_finished(self, token, *, error=None):
        if token is None:
            return
        with self._state_lock:
            entry = self._variant_by_token.get(token, {})
            if getattr(self._thread_local, "variant_token", None) == token:
                self._thread_local.variant_token = None
        elapsed_ms = None
        if entry.get("started_monotonic_ns"):
            elapsed_ms = (time.monotonic_ns() - entry["started_monotonic_ns"]) / 1_000_000
        self.record(
            "OCR_VARIANT_FINISHED",
            variant_token=token,
            variant_id=entry.get("variant_id"),
            variant_index=entry.get("variant_index"),
            duration_ms=elapsed_ms,
            error=error,
        )

    def _current_variant(self):
        token = getattr(self._thread_local, "variant_token", None)
        entry = self._variant_by_token.get(token, {})
        return token, entry

    @staticmethod
    def _startupinfo_payload(startupinfo):
        if startupinfo is None:
            return None
        return {
            "dwFlags": int(getattr(startupinfo, "dwFlags", 0) or 0),
            "wShowWindow": int(getattr(startupinfo, "wShowWindow", 0) or 0),
            "uses_startf_useshowwindow": bool(
                int(getattr(startupinfo, "dwFlags", 0) or 0)
                & int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0) or 0)
            ),
            "uses_sw_hide": (
                int(getattr(startupinfo, "wShowWindow", -1))
                == int(getattr(subprocess, "SW_HIDE", -2))
            ),
        }

    def _popen_requested(self, args, kwargs):
        token, variant = self._current_variant()
        executable = kwargs.get("executable")
        if executable is None and isinstance(args, (list, tuple)) and args:
            executable = args[0]
        return {
            "variant_token": token,
            "variant_id": variant.get("variant_id"),
            "variant_index": variant.get("variant_index"),
            "args": args,
            "executable_argument": executable,
            "cwd": kwargs.get("cwd") or os.getcwd(),
            "shell": bool(kwargs.get("shell", False)),
            "environment_overridden": "env" in kwargs,
            "stdin_mode": repr(kwargs.get("stdin")),
            "stdout_mode": repr(kwargs.get("stdout")),
            "stderr_mode": repr(kwargs.get("stderr")),
            "startupinfo": self._startupinfo_payload(kwargs.get("startupinfo")),
            "creationflags": int(kwargs.get("creationflags", 0) or 0),
            "launcher_pid": os.getpid(),
        }

    def _process_details(self, pid):
        details = {
            "pid": int(pid),
            "parent_pid": None,
            "process_name": None,
            "executable": None,
            "command_line": None,
            "creation_time": None,
        }
        try:
            process = psutil.Process(int(pid))
            details.update(
                parent_pid=process.ppid(),
                process_name=process.name(),
                executable=process.exe(),
                command_line=process.cmdline(),
                creation_time=process.create_time(),
            )
        except (psutil.Error, OSError):
            pass
        return details

    @contextmanager
    def _trace_popen(self):
        original_popen = subprocess.Popen
        diagnostics = self

        def watch_process(process, request):
            while process.poll() is None and not diagnostics._stop_event.wait(0.005):
                pass
            started = diagnostics._process_started.get(int(process.pid))
            diagnostics.record(
                "PROCESS_FINISHED",
                **request,
                **diagnostics._process_details(process.pid),
                returncode=process.returncode,
                duration_ms=(
                    (time.monotonic_ns() - started) / 1_000_000
                    if started
                    else None
                ),
            )

        def traced_popen(args, *popen_args, **popen_kwargs):
            request = diagnostics._popen_requested(args, popen_kwargs)
            executable = str(request.get("executable_argument") or "")
            trace_this = bool(request.get("variant_token")) or (
                Path(executable).name.casefold() == "tesseract.exe"
            )
            if not trace_this:
                return original_popen(args, *popen_args, **popen_kwargs)
            diagnostics.record("PROCESS_LAUNCH_REQUESTED", **request)
            try:
                process = original_popen(args, *popen_args, **popen_kwargs)
            except Exception as exc:
                diagnostics.record(
                    "PROCESS_LAUNCH_FAILED",
                    **request,
                    exception_type=type(exc).__name__,
                    exception=str(exc),
                )
                raise
            diagnostics._process_started[int(process.pid)] = time.monotonic_ns()
            details = diagnostics._process_details(process.pid)
            if details.get("parent_pid") is None:
                # This Popen boundary itself proves the direct parent even if
                # the child exits before psutil can reopen its process handle.
                details["parent_pid"] = os.getpid()
            diagnostics.record(
                "PROCESS_STARTED",
                **request,
                **details,
            )
            watcher = threading.Thread(
                target=watch_process,
                args=(process, request),
                name=f"ScreenBotOcrChildWatcher-{process.pid}",
                daemon=True,
            )
            diagnostics._process_watchers.append(watcher)
            watcher.start()
            # Return the exact Popen object supplied by the original factory.
            return process

        with _POPEN_PATCH_LOCK:
            subprocess.Popen = traced_popen
            try:
                yield
            finally:
                subprocess.Popen = original_popen

    def _process_sampler(self):
        known = set(self._baseline_pids)
        descendants = {os.getpid()}
        while not self._stop_event.wait(0.01):
            try:
                items = list(
                    psutil.process_iter(
                        ["pid", "ppid", "name", "exe", "cmdline", "create_time"]
                    )
                )
            except (psutil.Error, OSError):
                continue
            for process in items:
                info = process.info
                pid = int(info.get("pid") or 0)
                if not pid or pid in known:
                    continue
                known.add(pid)
                ppid = int(info.get("ppid") or 0)
                name = str(info.get("name") or "").casefold()
                related = ppid in descendants
                if related:
                    descendants.add(pid)
                if related or name in _PROCESS_NAMES:
                    self.record(
                        "PROCESS_SNAPSHOT_FIRST_SEEN",
                        pid=pid,
                        parent_pid=ppid,
                        process_name=info.get("name"),
                        executable=info.get("exe"),
                        command_line=info.get("cmdline"),
                        creation_time=info.get("create_time"),
                        screenbot_descendant=related,
                    )

    def _lightweight_window_state(self, hwnd):
        probe = getattr(self.adapter, "lightweight_window_state", None)
        if callable(probe):
            try:
                return probe(hwnd)
            except Exception:
                return None
        snapshot = self._window_snapshot(hwnd)
        return {
            "hwnd": int(hwnd),
            "pid": int(snapshot.get("pid") or 0),
            "thread_id": int(snapshot.get("thread_id") or 0),
            "is_visible": bool(snapshot.get("is_visible")),
        }

    def _window_sampler(self):
        previous = {}
        previous_foreground = int(self.adapter.foreground_hwnd() or 0)
        try:
            for hwnd in self.adapter.enumerate_windows():
                state = self._lightweight_window_state(hwnd)
                if state:
                    previous[int(hwnd)] = state
        except Exception:
            previous = {}
        while not self._stop_event.wait(0.01):
            try:
                hwnds = tuple(int(hwnd) for hwnd in self.adapter.enumerate_windows())
                foreground = int(self.adapter.foreground_hwnd() or 0)
            except Exception:
                continue
            current = {}
            for hwnd in hwnds:
                state = self._lightweight_window_state(hwnd)
                if state is None:
                    continue
                current[hwnd] = state
                old = previous.get(hwnd)
                transition = None
                if old is None:
                    transition = "created_visible" if state["is_visible"] else "created_hidden"
                elif bool(old.get("is_visible")) != bool(state["is_visible"]):
                    transition = "shown" if state["is_visible"] else "hidden"
                if transition is not None:
                    self.record(
                        "WINDOW_SNAPSHOT_TRANSITION",
                        transition=transition,
                        window=self._window_snapshot(hwnd),
                        foreground_hwnd=foreground,
                        active_child_pids=sorted(self._process_started),
                    )
            for hwnd, old in previous.items():
                if hwnd not in current:
                    self.record(
                        "WINDOW_SNAPSHOT_TRANSITION",
                        transition="destroyed",
                        window=old,
                        foreground_hwnd=foreground,
                        active_child_pids=sorted(self._process_started),
                    )
            if foreground != previous_foreground:
                self.record(
                    "FOREGROUND_SNAPSHOT_CHANGED",
                    previous_foreground_hwnd=previous_foreground,
                    foreground_hwnd=foreground,
                    foreground_window=self._window_snapshot(foreground),
                    active_child_pids=sorted(self._process_started),
                )
            previous = current
            previous_foreground = foreground

    def _on_win_event(self, payload):
        event = int(payload.get("event") or 0)
        hwnd = int(payload.get("hwnd") or 0)
        snapshot = None
        try:
            snapshot_value = self.adapter.snapshot_window(hwnd)
            snapshot = (
                snapshot_value.to_dict()
                if hasattr(snapshot_value, "to_dict")
                else dict(snapshot_value)
            )
        except Exception as exc:
            snapshot = {"hwnd": hwnd, "snapshot_error": type(exc).__name__}
        self.record(
            "WINDOW_EVENT",
            win_event=_OCR_EVENT_NAMES.get(event, f"0x{event:04X}"),
            event_id=event,
            source=payload,
            window={
                **snapshot,
                "parent_pid": self._process_details(
                    int(snapshot.get("pid") or 0)
                ).get("parent_pid"),
            },
            foreground_hwnd=self.adapter.foreground_hwnd(),
        )

    def _window_snapshot(self, hwnd):
        try:
            value = self.adapter.snapshot_window(int(hwnd or 0))
            return value.to_dict() if hasattr(value, "to_dict") else dict(value)
        except Exception as exc:
            return {
                "hwnd": int(hwnd or 0),
                "snapshot_error": type(exc).__name__,
                "snapshot_error_detail": str(exc),
            }

    def _z_order(self):
        result = []
        try:
            foreground = self.adapter.foreground_hwnd()
            for hwnd in self.adapter.enumerate_windows():
                snapshot = self._window_snapshot(hwnd)
                if snapshot.get("is_visible") or int(hwnd) == int(foreground):
                    result.append(
                        {
                            key: snapshot.get(key)
                            for key in (
                                "hwnd",
                                "pid",
                                "process_name",
                                "title",
                                "class_name",
                                "root_hwnd",
                                "is_visible",
                                "is_topmost",
                                "is_foreground",
                            )
                        }
                    )
        except Exception as exc:
            return [{"snapshot_error": type(exc).__name__}]
        return result

    def run_probe(self, probe_callable, *, target_snapshot, compact_hwnd=0):
        """Run exactly one caller-supplied capture/OCR operation."""
        if not self.enabled:
            raise RuntimeError("OCR process diagnostic is not enabled")
        with self._state_lock:
            if self._active:
                raise RuntimeError("An OCR process diagnostic is already running")
            if self._consumed:
                raise RuntimeError(
                    "The one-shot OCR process diagnostic has already been consumed"
                )
            self._active = True
            self._consumed = True
            self._stop_event.clear()
            self._probe_id = f"probe-{uuid4().hex}"
            self._observation_id = f"observation-{uuid4().hex}"
            self._variant_counter = 0
            self._variant_by_token.clear()
            self._process_started.clear()
            self._process_watchers.clear()
            self._deadline = time.monotonic() + 12.0
            self._open_log()

        target_hwnd = int(getattr(target_snapshot, "root_hwnd", 0) or 0)
        self.record(
            "PROBE_STARTED",
            pid=os.getpid(),
            python_executable=sys.executable,
            pytesseract_path=getattr(pytesseract, "__file__", None),
            pytesseract_version=getattr(pytesseract, "__version__", None),
            tesseract_cmd=getattr(
                getattr(pytesseract, "pytesseract", None),
                "tesseract_cmd",
                None,
            ),
            target_session_id=getattr(target_snapshot, "session_id", None),
            target_generation=getattr(target_snapshot, "generation", None),
            target_root_hwnd=target_hwnd,
            compact_hwnd=int(compact_hwnd or 0),
            foreground_hwnd=self.adapter.foreground_hwnd(),
            target_window=self._window_snapshot(target_hwnd),
            compact_window=self._window_snapshot(compact_hwnd),
            z_order=self._z_order(),
        )
        try:
            self._baseline_pids = set(psutil.pids())
            self._event_hook_handles = self.adapter.install_win_event_hook(
                self._on_win_event
            ) or ()
            self._sampler_thread = threading.Thread(
                target=self._process_sampler,
                name="ScreenBotOcrProcessSampler",
                daemon=True,
            )
            self._sampler_thread.start()
            self._window_sampler_thread = threading.Thread(
                target=self._window_sampler,
                name="ScreenBotOcrWindowSampler",
                daemon=True,
            )
            self._window_sampler_thread.start()
            # Establish a one-second pre-observation window after both read-only
            # observers are live.
            self._stop_event.wait(1.0)
            with self._trace_popen():
                result = probe_callable()
            self.record(
                "OCR_OBSERVATION_FINISHED",
                result=result,
                variant_count=self._variant_counter,
            )
            return {
                "probe_id": self._probe_id,
                "observation_id": self._observation_id,
                "variant_count": self._variant_counter,
                "result": result,
                **self.log_paths,
            }
        except Exception as exc:
            self.record(
                "PROBE_FAILED",
                exception_type=type(exc).__name__,
                exception=str(exc),
            )
            raise
        finally:
            # Keep the read-only observers alive briefly so a short-lived
            # console host can emit its final hide/destroy/foreground events.
            remaining = (
                max(0.0, min(2.0, self._deadline - time.monotonic()))
                if self._deadline is not None
                else 0.0
            )
            self._stop_event.wait(remaining)
            self._stop_event.set()
            if self._sampler_thread is not None:
                self._sampler_thread.join(timeout=1.0)
            if self._window_sampler_thread is not None:
                self._window_sampler_thread.join(timeout=1.0)
            for watcher in tuple(self._process_watchers):
                watcher.join(timeout=0.25)
            self.adapter.uninstall_win_event_hook(self._event_hook_handles)
            self.record(
                "PROBE_FINISHED",
                foreground_hwnd=self.adapter.foreground_hwnd(),
                target_window=self._window_snapshot(target_hwnd),
                compact_window=self._window_snapshot(compact_hwnd),
                z_order=self._z_order(),
            )
            with self._state_lock:
                self._active = False
                self._deadline = None

    def close(self):
        self._stop_event.set()
