import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
import psutil
import logging

user32 = ctypes.windll.user32


@dataclass
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

    @property
    def window_width(self):
        return self.window_right - self.window_left

    @property
    def window_height(self):
        return self.window_bottom - self.window_top

    def is_minimized(self):
        return bool(user32.IsIconic(self.hwnd))

    def is_valid(self):
        return bool(user32.IsWindow(self.hwnd)) and not self.is_minimized()

    def contains_point(self, x, y):
        return self.client_left <= x <= self.client_left + self.client_width and self.client_top <= y <= self.client_top + self.client_height

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
    def __init__(self):
        self.target = None
        self.logger = logging.getLogger("ScreenBot")

    def _get_window_text(self, hwnd):
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    def _get_window_info(self, hwnd):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = self._get_window_text(hwnd)
        process_name = "unknown"
        try:
            proc = psutil.Process(pid.value)
            process_name = proc.name()
        except Exception:
            pass

        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise ctypes.WinError()

        client_rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(client_rect)):
            raise ctypes.WinError()

        point = wintypes.POINT(client_rect.left, client_rect.top)
        if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
            raise ctypes.WinError()

        width = client_rect.right - client_rect.left
        height = client_rect.bottom - client_rect.top

        return WindowInfo(
            hwnd=hwnd,
            title=title,
            process_name=process_name,
            pid=pid.value,
            window_left=rect.left,
            window_top=rect.top,
            window_right=rect.right,
            window_bottom=rect.bottom,
            client_left=point.x,
            client_top=point.y,
            client_width=width,
            client_height=height,
        )

    def lock_foreground_window(self):
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            raise RuntimeError("無法取得前景視窗")
        info = self._get_window_info(hwnd)
        if info.is_minimized():
            raise RuntimeError("目標視窗已最小化")
        self.target = info
        self.logger.info(f"Locked target window hwnd={hwnd} title={info.title}")
        return info

    def refresh(self):
        if self.target is None:
            raise RuntimeError("沒有鎖定目標視窗")
        if not user32.IsWindow(self.target.hwnd):
            raise RuntimeError("目標視窗已關閉")
        if self.target.is_minimized():
            raise RuntimeError("目標視窗已最小化")
        self.target = self._get_window_info(self.target.hwnd)
        return self.target
