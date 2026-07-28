"""Win32 target-window query adapter and legacy compatibility projection."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import logging
from pathlib import Path
import threading

import psutil


user32 = ctypes.windll.user32
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
if hasattr(user32, "GetDpiForWindow"):
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_FileTime),
    ctypes.POINTER(_FileTime),
    ctypes.POINTER(_FileTime),
    ctypes.POINTER(_FileTime),
]
kernel32.GetProcessTimes.restype = wintypes.BOOL


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    process_name: str
    process_creation_time: int | float | None
    executable_path: str | None
    degraded_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    process_name: str
    pid: int
    window_left: int
    window_top: int
    window_right: int
    window_bottom: int
    client_left: int
    client_top: int
    client_width: int
    client_height: int
    capture_mode: str = "client"
    root_hwnd: int = 0
    client_hwnd: int = 0
    process_creation_time: int | float | None = None
    executable_path: str | None = None
    window_class: str = ""
    dpi: int = 0
    visible: bool = True
    minimized: bool = False
    foreground: bool = False
    connection_state: str | None = None
    visibility_state: str | None = None
    identity_valid: bool = True
    identity_degraded_reasons: tuple[str, ...] = ()

    @property
    def window_width(self):
        return self.window_right - self.window_left

    @property
    def window_height(self):
        return self.window_bottom - self.window_top

    def is_minimized(self):
        return bool(self.minimized or user32.IsIconic(self.hwnd))

    def is_valid(self):
        if self.connection_state is not None:
            return bool(
                self.identity_valid
                and self.connection_state == "ATTACHED"
                and user32.IsWindow(self.hwnd)
            )
        return bool(user32.IsWindow(self.hwnd)) and not self.is_minimized()

    def contains_point(self, x, y):
        return (
            self.client_left <= x <= self.client_left + self.client_width
            and self.client_top <= y <= self.client_top + self.client_height
        )

    def to_client_ratio(self, x, y):
        if self.client_width <= 0 or self.client_height <= 0:
            raise ValueError("Invalid client dimensions")
        rel_x = (x - self.client_left) / self.client_width
        rel_y = (y - self.client_top) / self.client_height
        return max(0.0, min(1.0, rel_x)), max(0.0, min(1.0, rel_y))

    def from_client_ratio(self, ratio_x, ratio_y):
        x = self.client_left + int(round(ratio_x * self.client_width))
        y = self.client_top + int(round(ratio_y * self.client_height))
        return x, y


class WindowTracker:
    """Win32 adapter.

    Once a TargetSessionService is attached, ``target`` becomes a read-only
    WindowInfo projection of the authoritative immutable TargetSnapshot.
    """

    def __init__(self):
        self._legacy_target = None
        self._target_session = None
        self._lock = threading.RLock()
        self.logger = logging.getLogger("ScreenBot")

    @property
    def target(self):
        with self._lock:
            service = self._target_session
            legacy = self._legacy_target
        if service is None:
            return legacy
        snapshot = service.get_snapshot()
        return self.window_info_from_snapshot(snapshot) if snapshot is not None else None

    @target.setter
    def target(self, value):
        with self._lock:
            if self._target_session is not None:
                raise AttributeError(
                    "WindowTracker.target is read-only while TargetSessionService is attached"
                )
            self._legacy_target = value

    def attach_target_session(self, service):
        with self._lock:
            if self._target_session is not None and self._target_session is not service:
                raise RuntimeError("A TargetSessionService is already attached")
            self._target_session = service

    def get_foreground_hwnd(self):
        return int(user32.GetForegroundWindow() or 0)

    def validate_window(self, hwnd):
        return bool(hwnd and user32.IsWindow(wintypes.HWND(int(hwnd))))

    def _get_window_text(self, hwnd):
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    @staticmethod
    def _native_process_identity(pid):
        reasons = []
        executable_path = None
        creation_time = None
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            wintypes.DWORD(int(pid)),
        )
        if handle:
            try:
                size = wintypes.DWORD(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(
                    handle, 0, buffer, ctypes.byref(size)
                ):
                    executable_path = buffer.value
                else:
                    reasons.append("executable_path_unavailable")

                created = _FileTime()
                exited = _FileTime()
                kernel_time = _FileTime()
                user_time = _FileTime()
                if kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                ):
                    creation_time = (
                        int(created.dwHighDateTime) << 32
                    ) | int(created.dwLowDateTime)
                else:
                    reasons.append("process_creation_time_unavailable")
            finally:
                kernel32.CloseHandle(handle)
        else:
            reasons.extend(
                ("executable_path_unavailable", "process_creation_time_unavailable")
            )
        return executable_path, creation_time, reasons

    def query_process_identity(self, pid):
        executable_path, creation_time, reasons = self._native_process_identity(pid)
        process_name = Path(executable_path).name if executable_path else "unknown"
        if executable_path is None or creation_time is None:
            try:
                proc = psutil.Process(int(pid))
                process_name = proc.name()
                if executable_path is None:
                    executable_path = proc.exe()
                    if executable_path:
                        reasons = [
                            reason
                            for reason in reasons
                            if reason != "executable_path_unavailable"
                        ]
                if creation_time is None:
                    creation_time = proc.create_time()
                    if creation_time is not None:
                        reasons = [
                            reason
                            for reason in reasons
                            if reason != "process_creation_time_unavailable"
                        ]
            except Exception:
                if executable_path:
                    process_name = Path(executable_path).name
        return ProcessIdentity(
            pid=int(pid),
            process_name=process_name,
            process_creation_time=creation_time,
            executable_path=executable_path,
            degraded_reasons=tuple(dict.fromkeys(reasons)),
        )

    def query_window(self, hwnd):
        hwnd = int(hwnd)
        if not self.validate_window(hwnd):
            raise RuntimeError("Target window is closed or invalid")

        pid = wintypes.DWORD()
        thread_id = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not thread_id or not pid.value:
            raise ctypes.WinError()
        process = self.query_process_identity(pid.value)
        title = self._get_window_text(hwnd)

        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise ctypes.WinError()
        client_rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(client_rect)):
            raise ctypes.WinError()
        point = wintypes.POINT(client_rect.left, client_rect.top)
        if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
            raise ctypes.WinError()

        class_buffer = ctypes.create_unicode_buffer(256)
        if not user32.GetClassNameW(hwnd, class_buffer, len(class_buffer)):
            raise ctypes.WinError()
        root = int(user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT) or hwnd)
        get_dpi = getattr(user32, "GetDpiForWindow", None)
        dpi = int(get_dpi(wintypes.HWND(hwnd))) if get_dpi else 0
        minimized = bool(user32.IsIconic(wintypes.HWND(hwnd)))
        visible = bool(user32.IsWindowVisible(wintypes.HWND(hwnd)))

        return WindowInfo(
            hwnd=hwnd,
            title=title,
            process_name=process.process_name,
            pid=int(pid.value),
            window_left=int(rect.left),
            window_top=int(rect.top),
            window_right=int(rect.right),
            window_bottom=int(rect.bottom),
            client_left=int(point.x),
            client_top=int(point.y),
            client_width=int(client_rect.right - client_rect.left),
            client_height=int(client_rect.bottom - client_rect.top),
            root_hwnd=root,
            client_hwnd=hwnd,
            process_creation_time=process.process_creation_time,
            executable_path=process.executable_path,
            window_class=class_buffer.value,
            dpi=dpi,
            visible=visible,
            minimized=minimized,
            foreground=self.get_foreground_hwnd() == root,
            identity_degraded_reasons=process.degraded_reasons,
        )

    def _get_window_info(self, hwnd):
        """Compatibility alias for existing read-only callers."""
        return self.query_window(hwnd)

    @staticmethod
    def window_info_from_snapshot(snapshot):
        if snapshot is None:
            return None
        left, top, right, bottom = snapshot.window_rect
        client_left, client_top = snapshot.client_screen_origin
        client_width, client_height = snapshot.current_client_size
        return WindowInfo(
            hwnd=snapshot.hwnd,
            title=snapshot.window_title,
            process_name=snapshot.process_name,
            pid=snapshot.pid,
            window_left=left,
            window_top=top,
            window_right=right,
            window_bottom=bottom,
            client_left=client_left,
            client_top=client_top,
            client_width=client_width,
            client_height=client_height,
            root_hwnd=snapshot.root_hwnd,
            client_hwnd=snapshot.client_hwnd,
            process_creation_time=snapshot.process_creation_time,
            executable_path=snapshot.executable_path,
            window_class=snapshot.window_class,
            dpi=snapshot.dpi,
            visible=snapshot.visibility_state.value not in {"HIDDEN", "UNKNOWN"},
            minimized=snapshot.visibility_state.value == "MINIMIZED",
            foreground=snapshot.visibility_state.value == "FOREGROUND",
            connection_state=snapshot.connection_state.value,
            visibility_state=snapshot.visibility_state.value,
            identity_valid=snapshot.identity_valid,
            identity_degraded_reasons=(
                (snapshot.identity_degraded_reason,)
                if snapshot.identity_degraded_reason
                else ()
            ),
        )

    def lock_foreground_window(self):
        with self._lock:
            service = self._target_session
        if service is not None:
            snapshot = service.lock_foreground_target()
            info = self.window_info_from_snapshot(snapshot)
            self.logger.info(
                "Locked target session hwnd=%s title=%s session_id=%s generation=%s",
                info.hwnd,
                info.title,
                snapshot.session_id,
                snapshot.generation,
            )
            return info

        hwnd = self.get_foreground_hwnd()
        if not hwnd:
            raise RuntimeError("No foreground target window is available")
        info = self.query_window(hwnd)
        if info.is_minimized():
            raise RuntimeError("Target window is minimized")
        self._legacy_target = info
        self.logger.info("Locked target window hwnd=%s title=%s", hwnd, info.title)
        return info

    def refresh(self):
        with self._lock:
            service = self._target_session
        if service is not None:
            snapshot = service.refresh()
            if snapshot is None:
                raise RuntimeError("No target window is locked")
            if not snapshot.identity_valid:
                raise RuntimeError(
                    f"Target window is disconnected: {snapshot.disconnect_reason}"
                )
            # Phase A keeps current Capture/Player behavior. TargetSession
            # remains ATTACHED while minimized, but compatibility consumers
            # still reject minimized operation until Phase B evaluates it.
            if snapshot.visibility_state.value == "MINIMIZED":
                raise RuntimeError("Target window is minimized")
            return self.window_info_from_snapshot(snapshot)

        target = self._legacy_target
        if target is None:
            raise RuntimeError("No target window is locked")
        if not self.validate_window(target.hwnd):
            raise RuntimeError("Target window is closed or invalid")
        if target.is_minimized():
            raise RuntimeError("Target window is minimized")
        self._legacy_target = self.query_window(target.hwnd)
        return self._legacy_target
