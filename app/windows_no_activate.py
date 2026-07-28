"""Windows-only no-activate support for the compact floating control surface."""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes


GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020
WM_MOUSEACTIVATE = 0x0021
MA_NOACTIVATE = 3


class WindowsNoActivateAdapter:
    """Small injectable Win32 adapter; it never changes foreground windows."""

    def __init__(self, observer=None):
        self._observer = observer

    def is_supported(self):
        return sys.platform == "win32"

    def _observe(self, event, **data):
        observer = self._observer
        if observer is None:
            return
        try:
            observer.observe_noactivate_event(event, **data)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return

    def _foreground_hwnd(self):
        observer = self._observer
        adapter = getattr(observer, "adapter", None)
        foreground = getattr(adapter, "foreground_hwnd", None)
        if not callable(foreground):
            return 0
        try:
            return int(foreground() or 0)
        except (OSError, RuntimeError, TypeError):
            return 0

    def apply(self, hwnd):
        if not self.is_supported() or not hwnd:
            return False
        started_at = time.perf_counter_ns()
        foreground_before = self._foreground_hwnd()
        self._observe(
            "NOACTIVATE_APPLY_BEGIN",
            hwnd=int(hwnd),
            foreground_before=foreground_before,
        )
        user32 = ctypes.windll.user32
        get_long = user32.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user32.GetWindowLongW
        set_long = user32.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user32.SetWindowLongW
        pointer_type = ctypes.c_ssize_t if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
        get_long.argtypes = (wintypes.HWND, ctypes.c_int)
        get_long.restype = pointer_type
        set_long.argtypes = (wintypes.HWND, ctypes.c_int, pointer_type)
        set_long.restype = pointer_type
        current = int(get_long(int(hwnd), GWL_EXSTYLE))
        requested = current | WS_EX_NOACTIVATE
        self._observe(
            "NOACTIVATE_STYLE_READ",
            hwnd=int(hwnd),
            foreground_before=foreground_before,
            style_before=current,
            style_requested=requested,
        )
        if current & WS_EX_NOACTIVATE:
            self._observe(
                "NOACTIVATE_APPLY_END",
                hwnd=int(hwnd),
                foreground_before=foreground_before,
                foreground_after=self._foreground_hwnd(),
                style_before=current,
                style_after=current,
                return_value=True,
                duration_us=(time.perf_counter_ns() - started_at) // 1000,
            )
            return True
        previous_style = int(set_long(int(hwnd), GWL_EXSTYLE, requested))
        self._observe(
            "NOACTIVATE_STYLE_WRITE_REQUESTED",
            hwnd=int(hwnd),
            foreground_before=foreground_before,
            style_before=current,
            style_requested=requested,
            previous_style=previous_style,
        )
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED
        self._observe(
            "NOACTIVATE_SETWINDOWPOS_BEGIN",
            hwnd=int(hwnd),
            foreground_before=self._foreground_hwnd(),
            style_before=current,
            style_requested=requested,
            setwindowpos_flags=flags,
            contains_swp_noactivate=bool(flags & 0x0010),
            contains_swp_nozorder=bool(flags & SWP_NOZORDER),
            contains_swp_framechanged=bool(flags & SWP_FRAMECHANGED),
        )
        result = user32.SetWindowPos(
            int(hwnd),
            0,
            0,
            0,
            0,
            0,
            flags,
        )
        last_error = ctypes.get_last_error() if not result else None
        self._observe(
            "NOACTIVATE_SETWINDOWPOS_END",
            hwnd=int(hwnd),
            foreground_before=foreground_before,
            foreground_after=self._foreground_hwnd(),
            style_before=current,
            style_requested=requested,
            setwindowpos_flags=flags,
            contains_swp_noactivate=bool(flags & 0x0010),
            contains_swp_nozorder=bool(flags & SWP_NOZORDER),
            contains_swp_framechanged=bool(flags & SWP_FRAMECHANGED),
            return_value=bool(result),
            last_error=last_error,
            duration_us=(time.perf_counter_ns() - started_at) // 1000,
        )
        applied = self.has_no_activate(hwnd)
        self._observe(
            "NOACTIVATE_APPLY_END",
            hwnd=int(hwnd),
            foreground_before=foreground_before,
            foreground_after=self._foreground_hwnd(),
            style_before=current,
            style_after=(current | WS_EX_NOACTIVATE) if applied else self._read_style(hwnd),
            return_value=applied,
            duration_us=(time.perf_counter_ns() - started_at) // 1000,
        )
        return applied

    def _read_style(self, hwnd):
        if not self.is_supported() or not hwnd:
            return 0
        user32 = ctypes.windll.user32
        get_long = user32.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user32.GetWindowLongW
        get_long.argtypes = (wintypes.HWND, ctypes.c_int)
        get_long.restype = ctypes.c_ssize_t if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
        return int(get_long(int(hwnd), GWL_EXSTYLE))

    def has_no_activate(self, hwnd):
        if not self.is_supported() or not hwnd:
            return False
        return bool(self._read_style(hwnd) & WS_EX_NOACTIVATE)

    def mouse_activate_result(self, message):
        if not self.is_supported() or not message:
            return None
        native_message = wintypes.MSG.from_address(int(message))
        if native_message.message == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        return None
