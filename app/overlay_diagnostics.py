"""Environment-gated, observation-only compact overlay diagnostics."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import ctypes
from ctypes import wintypes
import json
import logging
import math
import os
from pathlib import Path
import platform
import sys
import time
from typing import Callable

import psutil
from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QObject, Signal, qVersion


GWL_STYLE = -16
GWL_EXSTYLE = -20
GA_ROOT = 2
GA_ROOTOWNER = 3
GW_HWNDPREV = 3
GW_HWNDNEXT = 2
GW_OWNER = 4
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
DWMWA_CLOAKED = 14
WINEVENT_OUTOFCONTEXT = 0x0000
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003
EVENT_OBJECT_REORDER = 0x8004
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
OBJID_WINDOW = 0

MESSAGE_NAMES = {
    0x0081: "WM_NCCREATE",
    0x0001: "WM_CREATE",
    0x0018: "WM_SHOWWINDOW",
    0x0046: "WM_WINDOWPOSCHANGING",
    0x0047: "WM_WINDOWPOSCHANGED",
    0x007C: "WM_STYLECHANGING",
    0x007D: "WM_STYLECHANGED",
    0x0006: "WM_ACTIVATE",
    0x001C: "WM_ACTIVATEAPP",
    0x0021: "WM_MOUSEACTIVATE",
    0x0007: "WM_SETFOCUS",
    0x0008: "WM_KILLFOCUS",
    0x0086: "WM_NCACTIVATE",
    0x02E0: "WM_DPICHANGED",
    0x0002: "WM_DESTROY",
    0x0082: "WM_NCDESTROY",
}

EVENT_NAMES = {
    EVENT_SYSTEM_FOREGROUND: "EVENT_SYSTEM_FOREGROUND",
    EVENT_SYSTEM_MINIMIZESTART: "EVENT_SYSTEM_MINIMIZESTART",
    EVENT_SYSTEM_MINIMIZEEND: "EVENT_SYSTEM_MINIMIZEEND",
    EVENT_OBJECT_DESTROY: "EVENT_OBJECT_DESTROY",
    EVENT_OBJECT_SHOW: "EVENT_OBJECT_SHOW",
    EVENT_OBJECT_HIDE: "EVENT_OBJECT_HIDE",
    EVENT_OBJECT_REORDER: "EVENT_OBJECT_REORDER",
    EVENT_OBJECT_LOCATIONCHANGE: "EVENT_OBJECT_LOCATIONCHANGE",
}


def parse_window_styles(window_style, extended_style):
    """Decode the style bits required by the diagnostic schema."""
    return {
        "is_topmost": bool(extended_style & WS_EX_TOPMOST),
        "is_noactivate": bool(extended_style & WS_EX_NOACTIVATE),
        "is_toolwindow": bool(extended_style & WS_EX_TOOLWINDOW),
        "is_appwindow": bool(extended_style & WS_EX_APPWINDOW),
        "is_child": bool(window_style & WS_CHILD),
        "is_visible": bool(window_style & WS_VISIBLE),
    }


@dataclass(frozen=True)
class WindowIdentitySnapshot:
    timestamp_monotonic: float
    timestamp_wall: str
    hwnd: int
    is_window: bool
    pid: int | None
    thread_id: int | None
    process_name: str | None
    executable: str | None
    title: str | None
    class_name: str | None
    root_hwnd: int
    root_owner_hwnd: int
    parent_hwnd: int
    owner_hwnd: int
    window_style_raw: int
    extended_style_raw: int
    is_topmost: bool
    is_noactivate: bool
    is_toolwindow: bool
    is_appwindow: bool
    is_child: bool
    is_visible: bool
    is_enabled: bool
    is_iconic: bool
    is_cloaked: bool | None
    window_rect: tuple[int, int, int, int] | None
    client_rect_screen: tuple[int, int, int, int] | None
    dpi: int
    z_prev_hwnd: int
    z_next_hwnd: int
    foreground_hwnd: int
    foreground_root_hwnd: int
    is_foreground: bool

    def to_dict(self):
        return asdict(self)


class OverlayDiagnosticWin32Adapter:
    """Read-only Win32 observation adapter; it intentionally has no mutators."""

    def __init__(self):
        self._hook_callback = None
        self._dwmapi = None
        if sys.platform != "win32":
            self.user32 = None
            return
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._configure_apis()

    @property
    def is_supported(self):
        return self.user32 is not None

    def _configure_apis(self):
        pointer = ctypes.c_ssize_t
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.GetParent.argtypes = (wintypes.HWND,)
        self.user32.GetParent.restype = wintypes.HWND
        self.user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
        self.user32.GetWindow.restype = wintypes.HWND
        self.user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
        self.user32.GetWindowLongPtrW.restype = pointer
        self.user32.IsWindow.argtypes = (wintypes.HWND,)
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.IsWindowEnabled.argtypes = (wintypes.HWND,)
        self.user32.IsWindowEnabled.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = (wintypes.HWND,)
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = (
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        )
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.GetClassNameW.argtypes = (
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        )
        self.user32.GetClassNameW.restype = ctypes.c_int
        self.user32.GetWindowRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        self.user32.GetWindowRect.restype = wintypes.BOOL
        self.user32.GetClientRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        self.user32.GetClientRect.restype = wintypes.BOOL
        self.user32.ClientToScreen.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.POINT),
        )
        self.user32.ClientToScreen.restype = wintypes.BOOL
        if hasattr(self.user32, "GetDpiForWindow"):
            self.user32.GetDpiForWindow.argtypes = (wintypes.HWND,)
            self.user32.GetDpiForWindow.restype = wintypes.UINT

    def foreground_hwnd(self):
        return int(self.user32.GetForegroundWindow() or 0) if self.user32 else 0

    def root_hwnd(self, hwnd):
        if not self.user32 or not hwnd:
            return 0
        return int(self.user32.GetAncestor(wintypes.HWND(int(hwnd)), GA_ROOT) or hwnd)

    def root_owner_hwnd(self, hwnd):
        if not self.user32 or not hwnd:
            return 0
        return int(self.user32.GetAncestor(wintypes.HWND(int(hwnd)), GA_ROOTOWNER) or hwnd)

    def _text(self, hwnd):
        length = self.user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
        buffer = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(wintypes.HWND(hwnd), buffer, length + 1)
        return buffer.value

    def _class_name(self, hwnd):
        buffer = ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(wintypes.HWND(hwnd), buffer, len(buffer))
        return buffer.value

    def _rect(self, hwnd):
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))

    def _client_rect_screen(self, hwnd):
        rect = wintypes.RECT()
        if not self.user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
        top_left = wintypes.POINT(rect.left, rect.top)
        bottom_right = wintypes.POINT(rect.right, rect.bottom)
        if not self.user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(top_left)):
            return None
        if not self.user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(bottom_right)):
            return None
        return (
            int(top_left.x),
            int(top_left.y),
            int(bottom_right.x),
            int(bottom_right.y),
        )

    def _is_cloaked(self, hwnd):
        if not self.user32:
            return None
        try:
            if self._dwmapi is None:
                self._dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
                self._dwmapi.DwmGetWindowAttribute.argtypes = (
                    wintypes.HWND,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                )
                self._dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
            cloaked = wintypes.DWORD()
            result = self._dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd),
                DWMWA_CLOAKED,
                ctypes.byref(cloaked),
                ctypes.sizeof(cloaked),
            )
            return bool(cloaked.value) if result == 0 else None
        except OSError:
            return None

    def snapshot_window(self, hwnd):
        now_monotonic = time.monotonic()
        now_wall = datetime.now(timezone.utc).astimezone().isoformat()
        hwnd = int(hwnd or 0)
        if not self.user32 or not hwnd:
            return WindowIdentitySnapshot(
                timestamp_monotonic=now_monotonic,
                timestamp_wall=now_wall,
                hwnd=hwnd,
                is_window=False,
                pid=None,
                thread_id=None,
                process_name=None,
                executable=None,
                title=None,
                class_name=None,
                root_hwnd=0,
                root_owner_hwnd=0,
                parent_hwnd=0,
                owner_hwnd=0,
                window_style_raw=0,
                extended_style_raw=0,
                is_topmost=False,
                is_noactivate=False,
                is_toolwindow=False,
                is_appwindow=False,
                is_child=False,
                is_visible=False,
                is_enabled=False,
                is_iconic=False,
                is_cloaked=None,
                window_rect=None,
                client_rect_screen=None,
                dpi=0,
                z_prev_hwnd=0,
                z_next_hwnd=0,
                foreground_hwnd=0,
                foreground_root_hwnd=0,
                is_foreground=False,
            )

        exists = bool(self.user32.IsWindow(wintypes.HWND(hwnd)))
        foreground = self.foreground_hwnd()
        if not exists:
            return WindowIdentitySnapshot(
                timestamp_monotonic=now_monotonic,
                timestamp_wall=now_wall,
                hwnd=hwnd,
                is_window=False,
                pid=None,
                thread_id=None,
                process_name=None,
                executable=None,
                title=None,
                class_name=None,
                root_hwnd=0,
                root_owner_hwnd=0,
                parent_hwnd=0,
                owner_hwnd=0,
                window_style_raw=0,
                extended_style_raw=0,
                is_topmost=False,
                is_noactivate=False,
                is_toolwindow=False,
                is_appwindow=False,
                is_child=False,
                is_visible=False,
                is_enabled=False,
                is_iconic=False,
                is_cloaked=None,
                window_rect=None,
                client_rect_screen=None,
                dpi=0,
                z_prev_hwnd=0,
                z_next_hwnd=0,
                foreground_hwnd=foreground,
                foreground_root_hwnd=self.root_hwnd(foreground),
                is_foreground=False,
            )

        pid = wintypes.DWORD()
        thread_id = int(
            self.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
            or 0
        )
        process_name = executable = None
        try:
            process = psutil.Process(int(pid.value))
            process_name = process.name()
            executable = process.exe()
        except (psutil.Error, OSError):
            pass
        style = int(self.user32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_STYLE))
        exstyle = int(self.user32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE))
        style_flags = parse_window_styles(style, exstyle)
        root = self.root_hwnd(hwnd)
        foreground_root = self.root_hwnd(foreground)
        dpi_function = getattr(self.user32, "GetDpiForWindow", None)
        dpi = int(dpi_function(wintypes.HWND(hwnd))) if dpi_function else 0
        return WindowIdentitySnapshot(
            timestamp_monotonic=now_monotonic,
            timestamp_wall=now_wall,
            hwnd=hwnd,
            is_window=True,
            pid=int(pid.value),
            thread_id=thread_id or None,
            process_name=process_name,
            executable=executable,
            title=self._text(hwnd),
            class_name=self._class_name(hwnd),
            root_hwnd=root,
            root_owner_hwnd=self.root_owner_hwnd(hwnd),
            parent_hwnd=int(self.user32.GetParent(wintypes.HWND(hwnd)) or 0),
            owner_hwnd=int(self.user32.GetWindow(wintypes.HWND(hwnd), GW_OWNER) or 0),
            window_style_raw=style,
            extended_style_raw=exstyle,
            is_topmost=style_flags["is_topmost"],
            is_noactivate=style_flags["is_noactivate"],
            is_toolwindow=style_flags["is_toolwindow"],
            is_appwindow=style_flags["is_appwindow"],
            is_child=style_flags["is_child"],
            is_visible=bool(
                style_flags["is_visible"]
                and self.user32.IsWindowVisible(wintypes.HWND(hwnd))
            ),
            is_enabled=bool(self.user32.IsWindowEnabled(wintypes.HWND(hwnd))),
            is_iconic=bool(self.user32.IsIconic(wintypes.HWND(hwnd))),
            is_cloaked=self._is_cloaked(hwnd),
            window_rect=self._rect(hwnd),
            client_rect_screen=self._client_rect_screen(hwnd),
            dpi=dpi,
            z_prev_hwnd=int(self.user32.GetWindow(wintypes.HWND(hwnd), GW_HWNDPREV) or 0),
            z_next_hwnd=int(self.user32.GetWindow(wintypes.HWND(hwnd), GW_HWNDNEXT) or 0),
            foreground_hwnd=foreground,
            foreground_root_hwnd=foreground_root,
            is_foreground=root != 0 and root == foreground_root,
        )

    def enumerate_windows(self):
        if not self.user32:
            return []
        windows = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def callback(hwnd, _lparam):
            windows.append(int(hwnd))
            return True

        self.user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.EnumWindows(callback, 0)
        return windows

    def install_win_event_hook(self, callback):
        if not self.user32:
            return None
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
        handles = []
        for event in EVENT_NAMES:
            handle = self.user32.SetWinEventHook(
                event,
                event,
                None,
                hook,
                0,
                0,
                WINEVENT_OUTOFCONTEXT,
            )
            if handle:
                handles.append(handle)
        return tuple(handles)

    def uninstall_win_event_hook(self, handles):
        if not self.user32:
            return
        self.user32.UnhookWinEvent.argtypes = (wintypes.HANDLE,)
        self.user32.UnhookWinEvent.restype = wintypes.BOOL
        for handle in handles or ():
            self.user32.UnhookWinEvent(handle)
        self._hook_callback = None


class OverlayDiagnostics(QObject):
    """Writes observation records only when explicitly enabled by the environment."""

    win_event_received = Signal(dict)

    def __init__(self, root_path, logger=None, adapter=None, enabled=None):
        super().__init__()
        self.root_path = Path(root_path)
        self.logger = logger or logging.getLogger("ScreenBot")
        self.adapter = adapter or OverlayDiagnosticWin32Adapter()
        self.enabled = (
            os.environ.get("SCREENBOT_OVERLAY_DIAGNOSTIC") == "1"
            if enabled is None
            else bool(enabled)
        )
        self._compact_hwnd = 0
        self._locked_target_root = 0
        self._popup_roles = {}
        self._hook_handles = ()
        self._interaction_id = None
        self._interaction_counter = 0
        self._message_counter = 0
        self._location_samples = {}
        self._json_handle = None
        self._text_handle = None
        self.log_path = None
        self.text_log_path = None
        self.win_event_received.connect(self._record_win_event)
        if self.enabled:
            self._open_logs()
            self.record_event(
                "DIAGNOSTIC_STARTED",
                diagnostic_schema_version=1,
                application_pid=os.getpid(),
                application_start_time=datetime.now(timezone.utc).astimezone().isoformat(),
                qt_version=qVersion(),
                pyside_version=pyside_version,
                python_version=sys.version,
                windows_version=platform.platform(),
                diagnostic_enabled=True,
            )

    def _open_logs(self):
        directory = self.root_path / "logs" / "overlay_diagnostics"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"overlay_diag_{stamp}_{os.getpid()}"
        self.log_path = directory / f"{stem}.jsonl"
        self.text_log_path = directory / f"{stem}.log"
        self._json_handle = self.log_path.open("a", encoding="utf-8")
        self._text_handle = self.text_log_path.open("a", encoding="utf-8")

    def start(self):
        if not self.enabled:
            return False
        try:
            self._hook_handles = self.adapter.install_win_event_hook(self._queue_win_event) or ()
            self.record_event("WINEVENT_HOOK_STARTED", hook_count=len(self._hook_handles))
            return True
        except Exception as exc:
            self._failure("WINEVENT_HOOK_START_FAILED", exc)
            return False

    def close(self):
        if not self.enabled:
            return
        try:
            if self._hook_handles:
                self.adapter.uninstall_win_event_hook(self._hook_handles)
                self.record_event("WINEVENT_HOOK_STOPPED", hook_count=len(self._hook_handles))
                self._hook_handles = ()
            self.record_event("DIAGNOSTIC_STOPPED")
        except Exception as exc:
            self._failure("WINEVENT_HOOK_STOP_FAILED", exc)
        finally:
            if self._json_handle is not None:
                self._json_handle.close()
                self._json_handle = None
            if self._text_handle is not None:
                self._text_handle.close()
                self._text_handle = None

    def _failure(self, event, exc):
        self.logger.exception("Overlay diagnostic failure event=%s", event)
        self.record_event(event, exception_type=type(exc).__name__, message=str(exc))

    def _queue_win_event(self, payload):
        if self.enabled:
            self.win_event_received.emit(dict(payload))

    def _record_win_event(self, payload):
        event = int(payload.get("event", 0))
        hwnd = int(payload.get("hwnd", 0))
        object_id = int(payload.get("object_id", OBJID_WINDOW))
        if object_id not in {OBJID_WINDOW, 0}:
            return
        root = self.adapter.root_hwnd(hwnd)
        foreground_root = self.adapter.root_hwnd(
            self.adapter.foreground_hwnd()
        )
        relevant_roots = {
            int(self._compact_hwnd or 0),
            int(self._locked_target_root or 0),
            int(foreground_root or 0),
        }
        if (
            event != EVENT_SYSTEM_FOREGROUND
            and root not in relevant_roots
        ):
            return
        if event == EVENT_OBJECT_LOCATIONCHANGE:
            key = (event, root)
            now = time.monotonic()
            if now - self._location_samples.get(key, 0.0) < 0.25:
                return
            self._location_samples[key] = now
        snapshot = self.capture_window(hwnd)
        self.record_event(
            "WINEVENT",
            event_name=EVENT_NAMES.get(event, f"EVENT_{event:#x}"),
            win_event=payload,
            role=self.classify_window(snapshot),
            window=snapshot,
        )
        if event in {EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_REORDER}:
            self.capture_z_order_snapshot(EVENT_NAMES.get(event, "WINEVENT"))

    def record_event(self, event, **data):
        if not self.enabled or self._json_handle is None:
            return
        try:
            record = {
                "event": event,
                "timestamp_monotonic": time.monotonic(),
                "timestamp_wall": datetime.now(timezone.utc).astimezone().isoformat(),
                **data,
            }
            encoded_record = json.dumps(
                self._json_safe(record),
                ensure_ascii=False,
                default=str,
                allow_nan=False,
            )
            self._json_handle.write(encoded_record + "\n")
            self._json_handle.flush()
            self._text_handle.write(f"{record['timestamp_wall']} {event} {encoded_record}\n")
            self._text_handle.flush()
        except (OSError, TypeError, ValueError) as exc:
            self.logger.exception("Overlay diagnostic log write failed: %s", exc)

    @classmethod
    def _json_safe(cls, value):
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, dict):
            return {str(key): cls._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._json_safe(item) for item in value]
        return value

    def capture_window(self, hwnd):
        try:
            snapshot = self.adapter.snapshot_window(int(hwnd or 0))
            data = (
                snapshot.to_dict()
                if hasattr(snapshot, "to_dict")
                else dict(snapshot)
            )
            data["is_layered"] = bool(
                int(data.get("extended_style_raw", 0)) & WS_EX_LAYERED
            )
            return data
        except Exception as exc:
            self._failure("WINDOW_SNAPSHOT_FAILED", exc)
            return {"hwnd": int(hwnd or 0), "snapshot_error": type(exc).__name__}

    def classify_window(self, snapshot):
        hwnd = int(snapshot.get("hwnd", 0))
        if hwnd == self._compact_hwnd:
            return "SCREENBOT_COMPACT_ROOT"
        if hwnd in self._popup_roles:
            return self._popup_roles[hwnd]
        if self._compact_hwnd and snapshot.get("root_hwnd") == self._compact_hwnd:
            return "SCREENBOT_CHILD_CONTROL"
        if (
            self._compact_hwnd
            and snapshot.get("owner_hwnd") == self._compact_hwnd
            and snapshot.get("class_name") in {"tooltips_class32", "QTipLabel"}
        ):
            return "SCREENBOT_TOOLTIP"
        if snapshot.get("pid") == os.getpid():
            return "SCREENBOT_OTHER_TOPLEVEL"
        if hwnd == self._locked_target_root:
            return "LOCKED_TARGET_ROOT"
        if self._locked_target_root and snapshot.get("root_hwnd") == self._locked_target_root:
            return "LOCKED_TARGET_CLIENT"
        if snapshot.get("is_foreground"):
            return "THIRD_PARTY_FOREGROUND"
        return "UNKNOWN"

    def observe_compact_lifecycle(self, event, old_hwnd=0, new_hwnd=0, **data):
        if not self.enabled:
            return
        old_hwnd = int(old_hwnd or self._compact_hwnd or 0)
        new_hwnd = int(new_hwnd or old_hwnd or 0)
        if new_hwnd:
            self._compact_hwnd = new_hwnd
        self.record_event(
            event,
            old_hwnd=old_hwnd,
            new_hwnd=new_hwnd,
            hwnd_changed=bool(old_hwnd and new_hwnd and old_hwnd != new_hwnd),
            compact_window=self.capture_window(new_hwnd),
            **data,
        )
        self.capture_z_order_snapshot(event)

    def observe_noactivate_event(self, event, **data):
        if self.enabled:
            self.record_event(event, **data)

    def begin_interaction(self, interaction_kind):
        if not self.enabled:
            return None
        self._interaction_counter += 1
        self._interaction_id = f"{interaction_kind.lower()}-{self._interaction_counter}-{time.monotonic_ns()}"
        self.record_event(
            "INTERACTION_BEGIN",
            interaction_id=self._interaction_id,
            interaction_kind=interaction_kind,
            compact_root_hwnd=self._compact_hwnd,
            compact_window=self.capture_window(self._compact_hwnd),
        )
        return self._interaction_id

    def current_interaction_id(self):
        return self._interaction_id if self.enabled else None

    def observe_ui_event(self, event, receiver=None, **data):
        if not self.enabled:
            return
        snapshot = self.capture_window(self._compact_hwnd)
        locked_root = self._locked_target_root
        self.record_event(
            event,
            interaction_id=self._interaction_id,
            compact_root_hwnd=self._compact_hwnd,
            event_receiver=receiver,
            foreground_hwnd=snapshot.get("foreground_hwnd"),
            foreground_root_hwnd=snapshot.get("foreground_root_hwnd"),
            locked_root_hwnd=locked_root,
            foreground_matches_locked=bool(
                locked_root and snapshot.get("foreground_root_hwnd") == locked_root
            ),
            compact_is_foreground=snapshot.get("is_foreground"),
            **data,
        )
        if event in {"START_UI_MOUSE_PRESS", "START_UI_MOUSE_RELEASE", "START_SIGNAL_EMITTED"}:
            self.capture_z_order_snapshot(event)

    def observe_popup(self, role, widget, event):
        if not self.enabled:
            return
        try:
            hwnd = int(widget.winId())
        except (RuntimeError, TypeError):
            hwnd = 0
        if hwnd:
            self._popup_roles[hwnd] = role
        self.record_event(
            event,
            popup_role=role,
            popup_hwnd=hwnd,
            popup_window=self.capture_window(hwnd),
            interaction_id=self._interaction_id,
        )
        self.capture_z_order_snapshot(event)

    def observe_native_message(self, event_type, message, handler_result=None):
        if not self.enabled or not self.adapter.is_supported:
            return None
        try:
            native_message = wintypes.MSG.from_address(int(message))
            message_id = int(native_message.message)
            if message_id not in MESSAGE_NAMES:
                return None
            self._message_counter += 1
            payload = {
                "message_sequence_id": self._message_counter,
                "message_name": MESSAGE_NAMES[message_id],
                "message_id": message_id,
                "receiver_hwnd": int(native_message.hWnd or 0),
                "top_level_root": self.adapter.root_hwnd(int(native_message.hWnd or 0)),
                "event_type": bytes(event_type).decode(errors="replace"),
                "wparam": int(native_message.wParam),
                "lparam": int(native_message.lParam),
                "foreground_before": self.adapter.foreground_hwnd(),
                "handler_result": handler_result,
            }
            if message_id == 0x0021:
                payload["hit_test_code"] = int(native_message.lParam) & 0xFFFF
                payload["mouse_message"] = (int(native_message.lParam) >> 16) & 0xFFFF
            self.record_event("NATIVE_MESSAGE", sample_phase="before_handler", **payload)
            return payload
        except (OSError, TypeError, ValueError) as exc:
            self._failure("NATIVE_MESSAGE_CAPTURE_FAILED", exc)
            return None

    def observe_native_message_after(self, payload, handler_result):
        if not self.enabled or payload is None:
            return
        queued = dict(payload)
        queued["handler_result"] = handler_result
        queued["foreground_after"] = self.adapter.foreground_hwnd()
        self.record_event("NATIVE_MESSAGE", sample_phase="queued_after_message", **queued)

    def observe_target_event(self, event, snapshot=None, **data):
        if not self.enabled:
            return
        target = snapshot
        if target is not None:
            self._locked_target_root = int(getattr(target, "root_hwnd", 0) or 0)
            target_data = {
                "session_id": getattr(target, "session_id", None),
                "generation": getattr(target, "generation", None),
                "target_root_hwnd": self._locked_target_root,
                "target_client_hwnd": getattr(target, "client_hwnd", None),
                "target_rect": getattr(target, "window_rect", None),
                "client_rect": getattr(target, "current_client_size", None),
                "visibility": getattr(getattr(target, "visibility_state", None), "value", None),
                "identity_strength": getattr(target, "identity_strength", None),
            }
        else:
            target_data = {}
        self.record_event(
            event,
            **target_data,
            foreground_hwnd=self.adapter.foreground_hwnd(),
            **data,
        )
        self.capture_z_order_snapshot(event)

    def observe_coordinator_reconcile(
        self,
        phase,
        *,
        reason,
        session_id,
        generation,
        overlay_hwnd,
        target_hwnd,
        binding_valid,
        target_suppressed,
        user_visibility_intent,
        effective_visibility,
        computed_predecessor=None,
        already_settled=None,
        set_window_pos_action=None,
        skip_reason=None,
        widget=None,
    ):
        """Record a coordinator decision without changing that decision."""
        if not self.enabled:
            return
        foreground = self.adapter.foreground_hwnd()
        overlay = self.capture_window(overlay_hwnd)
        target = self.capture_window(target_hwnd)
        qt_data = {}
        if widget is not None:
            try:
                qt_data = {
                    "qt_window_flags": int(widget.windowFlags()),
                    "qt_visible": bool(widget.isVisible()),
                    "qt_wa_show_without_activating": bool(
                        widget.testAttribute(98)
                    ),
                }
            except (AttributeError, RuntimeError, TypeError, ValueError):
                qt_data = {"qt_snapshot_error": True}
        self.record_event(
            "COORDINATOR_RECONCILE",
            phase=str(phase),
            reason=str(reason),
            thread_id=int(__import__("threading").get_ident()),
            session_id=session_id,
            generation=generation,
            binding_valid=bool(binding_valid),
            target_suppressed=bool(target_suppressed),
            user_visibility_intent=bool(user_visibility_intent),
            effective_visibility=effective_visibility,
            overlay_hwnd=int(overlay_hwnd or 0),
            overlay_root_hwnd=overlay.get("root_hwnd"),
            target_root_hwnd=target.get("root_hwnd"),
            foreground_hwnd=foreground,
            foreground_root_hwnd=self.adapter.root_hwnd(foreground),
            target_topmost=target.get("is_topmost"),
            overlay_topmost=overlay.get("is_topmost"),
            computed_predecessor=computed_predecessor,
            computed_successor=overlay.get("z_next_hwnd"),
            already_settled=already_settled,
            set_window_pos_action=set_window_pos_action,
            skip_reason=skip_reason,
            **qt_data,
        )
        self.capture_z_order_snapshot(f"COORDINATOR_{phase}_{reason}")

    def observe_coordinator_event(
        self,
        event,
        *,
        overlay_hwnd=0,
        target_hwnd=0,
        widget=None,
        **data,
    ):
        """Record coordinator lifecycle state without changing its decisions."""
        if not self.enabled:
            return
        overlay = self.capture_window(overlay_hwnd)
        target = self.capture_window(target_hwnd)
        qt_data = {}
        if widget is not None:
            try:
                qt_data = {
                    "qt_visible": bool(widget.isVisible()),
                    "qt_visibility_intent": bool(
                        widget.is_compact_visibility_intended()
                    ),
                    "qt_target_suppressed": bool(
                        getattr(widget, "_target_visibility_suppressed", False)
                    ),
                }
            except (AttributeError, RuntimeError, TypeError, ValueError):
                qt_data = {"qt_snapshot_error": True}
        self.record_event(
            str(event),
            thread_id=int(__import__("threading").get_ident()),
            overlay_hwnd=int(overlay_hwnd or 0),
            overlay_valid=bool(overlay.get("is_window")),
            overlay_native_visible=overlay.get("is_visible"),
            target_hwnd=int(target_hwnd or 0),
            target_root_hwnd=target.get("root_hwnd"),
            target_valid=bool(target.get("is_window")),
            target_native_visible=target.get("is_visible"),
            target_iconic=target.get("is_iconic"),
            **qt_data,
            **data,
        )

    def observe_set_window_pos(
        self,
        phase,
        *,
        reason,
        hwnd,
        insert_after,
        x,
        y,
        width,
        height,
        flags,
        result=None,
        last_error=None,
    ):
        """Capture exact native mutation arguments and surrounding state."""
        if not self.enabled:
            return
        foreground = self.adapter.foreground_hwnd()
        self.record_event(
            "SET_WINDOW_POS",
            phase=str(phase),
            reason=str(reason),
            thread_id=int(__import__("threading").get_ident()),
            overlay_hwnd=int(hwnd or 0),
            insert_after_raw=int(insert_after),
            insert_after_symbolic=self.decode_hwnd_insert_after(insert_after),
            x=int(x),
            y=int(y),
            width=int(width),
            height=int(height),
            flags_raw=int(flags),
            flags_decoded=self.decode_swp_flags(flags),
            result=result,
            get_last_error=last_error,
            foreground_hwnd=foreground,
            foreground_root_hwnd=self.adapter.root_hwnd(foreground),
            overlay_window=self.capture_window(hwnd),
        )
        self.capture_z_order_snapshot(f"SET_WINDOW_POS_{phase}_{reason}")

    @staticmethod
    def decode_hwnd_insert_after(value):
        return {
            0: "HWND_TOP",
            1: "HWND_BOTTOM",
            -1: "HWND_TOPMOST",
            -2: "HWND_NOTOPMOST",
        }.get(int(value), f"HWND({int(value)})")

    @staticmethod
    def decode_swp_flags(flags):
        flags = int(flags)
        names = (
            (0x0001, "SWP_NOSIZE"),
            (0x0002, "SWP_NOMOVE"),
            (0x0004, "SWP_NOZORDER"),
            (0x0010, "SWP_NOACTIVATE"),
            (0x0040, "SWP_SHOWWINDOW"),
            (0x0080, "SWP_HIDEWINDOW"),
            (0x0200, "SWP_NOOWNERZORDER"),
            (0x0400, "SWP_NOSENDCHANGING"),
        )
        return [name for bit, name in names if flags & bit]

    def capture_z_order_snapshot(self, reason):
        if not self.enabled:
            return
        try:
            windows = self.adapter.enumerate_windows()
            snapshots = [self.capture_window(hwnd) for hwnd in windows]
            compact_index = self._index_for(self._compact_hwnd, windows)
            target_index = self._index_for(self._locked_target_root, windows)
            self.record_event(
                "Z_ORDER_SNAPSHOT",
                reason=reason,
                foreground_root_hwnd=self.adapter.root_hwnd(self.adapter.foreground_hwnd()),
                compact_root_hwnd=self._compact_hwnd,
                locked_target_root_hwnd=self._locked_target_root,
                compact_neighbors=self._neighbors(snapshots, compact_index),
                target_neighbors=self._neighbors(snapshots, target_index),
                top_to_bottom=[
                    {
                        "z_index": index,
                        "hwnd": item.get("hwnd"),
                        "root_hwnd": item.get("root_hwnd"),
                        "role": self.classify_window(item),
                        "pid": item.get("pid"),
                        "process_name": item.get("process_name"),
                        "class_name": item.get("class_name"),
                        "title": item.get("title"),
                        "visible": item.get("is_visible"),
                        "iconic": item.get("is_iconic"),
                        "topmost": item.get("is_topmost"),
                        "owner_hwnd": item.get("owner_hwnd"),
                        "root_owner_hwnd": item.get("root_owner_hwnd"),
                    }
                    for index, item in enumerate(snapshots)
                ],
            )
        except Exception as exc:
            self._failure("Z_ORDER_CAPTURE_FAILED", exc)

    @staticmethod
    def _index_for(hwnd, windows):
        try:
            return windows.index(hwnd) if hwnd else None
        except ValueError:
            return None

    def _neighbors(self, snapshots, index):
        if index is None:
            return {"above": [], "below": []}
        return {
            "above": [
                self._z_item(snapshot)
                for snapshot in snapshots[max(0, index - 5):index]
            ],
            "below": [
                self._z_item(snapshot)
                for snapshot in snapshots[index + 1:index + 6]
            ],
        }

    def _z_item(self, snapshot):
        return {
            "hwnd": snapshot.get("hwnd"),
            "role": self.classify_window(snapshot),
            "process_name": snapshot.get("process_name"),
            "title": snapshot.get("title"),
            "topmost": snapshot.get("is_topmost"),
        }
