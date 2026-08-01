"""Fail-closed TargetSession boundary for passive Recorder events."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path


GA_ROOT = 2
GW_OWNER = 4


@dataclass(frozen=True)
class FrozenTargetBoundary:
    session_id: str
    generation: int
    pid: int
    process_creation_time: int | float | None
    root_hwnd: int
    client_hwnd: int
    executable_path: str | None
    identity_strength: str

    @classmethod
    def from_snapshot(cls, snapshot):
        if snapshot is None or not bool(getattr(snapshot, "identity_valid", False)):
            raise RuntimeError("Recording requires a valid TargetSession.")
        return cls(
            session_id=str(snapshot.session_id),
            generation=int(snapshot.generation),
            pid=int(snapshot.pid),
            process_creation_time=snapshot.process_creation_time,
            root_hwnd=int(snapshot.root_hwnd),
            client_hwnd=int(snapshot.client_hwnd),
            executable_path=snapshot.executable_path,
            identity_strength=str(snapshot.identity_strength),
        )


@dataclass(frozen=True)
class BoundaryDecision:
    accepted: bool
    reason: str
    session_valid: bool = True
    client_x: int | None = None
    client_y: int | None = None
    x_ratio: float | None = None
    y_ratio: float | None = None
    hit_hwnd: int = 0


class Win32BoundaryAdapter:
    """Read-only Win32 hit-test adapter; it never changes window state."""

    def __init__(self, user32=None):
        self.user32 = user32 or ctypes.windll.user32
        signatures = (
            ("WindowFromPoint", (wintypes.POINT,), wintypes.HWND),
            (
                "GetAncestor",
                (wintypes.HWND, wintypes.UINT),
                wintypes.HWND,
            ),
            (
                "GetWindow",
                (wintypes.HWND, wintypes.UINT),
                wintypes.HWND,
            ),
            (
                "GetWindowThreadProcessId",
                (wintypes.HWND, ctypes.POINTER(wintypes.DWORD)),
                wintypes.DWORD,
            ),
            ("GetForegroundWindow", (), wintypes.HWND),
            (
                "ScreenToClient",
                (wintypes.HWND, ctypes.POINTER(wintypes.POINT)),
                wintypes.BOOL,
            ),
            (
                "GetClientRect",
                (wintypes.HWND, ctypes.POINTER(wintypes.RECT)),
                wintypes.BOOL,
            ),
        )
        for name, argtypes, restype in signatures:
            function = getattr(self.user32, name, None)
            if function is not None:
                function.argtypes = list(argtypes)
                function.restype = restype

    def window_from_point(self, x, y):
        point = wintypes.POINT(int(x), int(y))
        return int(self.user32.WindowFromPoint(point) or 0)

    def root_hwnd(self, hwnd):
        if not hwnd:
            return 0
        return int(
            self.user32.GetAncestor(wintypes.HWND(int(hwnd)), GA_ROOT) or hwnd
        )

    def owner_hwnd(self, hwnd):
        if not hwnd:
            return 0
        return int(
            self.user32.GetWindow(wintypes.HWND(int(hwnd)), GW_OWNER) or 0
        )

    def pid_for_window(self, hwnd):
        if not hwnd:
            return 0
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(
            wintypes.HWND(int(hwnd)), ctypes.byref(pid)
        )
        return int(pid.value)

    def foreground_hwnd(self):
        return int(self.user32.GetForegroundWindow() or 0)

    def screen_to_client(self, hwnd, x, y):
        point = wintypes.POINT(int(x), int(y))
        if not self.user32.ScreenToClient(wintypes.HWND(int(hwnd)), ctypes.byref(point)):
            raise ctypes.WinError()
        return int(point.x), int(point.y)

    def client_size(self, hwnd):
        rect = wintypes.RECT()
        if not self.user32.GetClientRect(wintypes.HWND(int(hwnd)), ctypes.byref(rect)):
            raise ctypes.WinError()
        return int(rect.right - rect.left), int(rect.bottom - rect.top)


class TargetBoundaryFilter:
    """Authorize raw observations against one immutable TargetSession token."""

    def __init__(
        self,
        target_session,
        frozen,
        *,
        adapter=None,
        screenbot_pid=None,
    ):
        self.target_session = target_session
        self.frozen = frozen
        self.adapter = adapter or Win32BoundaryAdapter()
        self.screenbot_pid = int(screenbot_pid or os.getpid())

    @staticmethod
    def _same_path(left, right):
        if left is None or right is None:
            return left is right
        return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
            str(Path(right).resolve())
        )

    def validate_session(self):
        try:
            snapshot = self.target_session.refresh()
        except Exception:
            return BoundaryDecision(False, "target_refresh_failed", False)
        if snapshot is None:
            return BoundaryDecision(False, "target_session_cleared", False)
        checks = (
            (str(snapshot.session_id) == self.frozen.session_id, "session_id_changed"),
            (int(snapshot.generation) == self.frozen.generation, "generation_changed"),
            (int(snapshot.pid) == self.frozen.pid, "pid_changed"),
            (
                snapshot.process_creation_time == self.frozen.process_creation_time,
                "process_creation_time_changed",
            ),
            (int(snapshot.root_hwnd) == self.frozen.root_hwnd, "root_hwnd_changed"),
            (int(snapshot.client_hwnd) == self.frozen.client_hwnd, "client_hwnd_changed"),
            (
                self._same_path(snapshot.executable_path, self.frozen.executable_path),
                "executable_path_changed",
            ),
            (bool(snapshot.identity_valid), "identity_invalid"),
        )
        for accepted, reason in checks:
            if not accepted:
                return BoundaryDecision(False, reason, False)
        return BoundaryDecision(True, "session_valid")

    def authorize_keyboard(self):
        session = self.validate_session()
        if not session.accepted:
            return session
        try:
            foreground = self.adapter.foreground_hwnd()
            if not foreground:
                return BoundaryDecision(False, "no_foreground_window")
            root = self.adapter.root_hwnd(foreground)
            if self.adapter.pid_for_window(root or foreground) == self.screenbot_pid:
                return BoundaryDecision(False, "screenbot_window")
            if root != self.frozen.root_hwnd:
                return BoundaryDecision(False, "foreground_outside_target")
            return BoundaryDecision(True, "target_foreground", hit_hwnd=foreground)
        except Exception:
            return BoundaryDecision(False, "foreground_query_failed")

    def authorize_mouse(self, screen_x, screen_y):
        session = self.validate_session()
        if not session.accepted:
            return session
        try:
            hit = self.adapter.window_from_point(screen_x, screen_y)
            if not hit:
                return BoundaryDecision(False, "no_window_at_point")
            if self.adapter.pid_for_window(hit) == self.screenbot_pid:
                return BoundaryDecision(False, "screenbot_window", hit_hwnd=hit)
            if not self._belongs_to_frozen_root(hit):
                return BoundaryDecision(False, "window_outside_target", hit_hwnd=hit)
            client_x, client_y = self.adapter.screen_to_client(
                self.frozen.client_hwnd, screen_x, screen_y
            )
            width, height = self.adapter.client_size(self.frozen.client_hwnd)
            if (
                width <= 0
                or height <= 0
                or client_x < 0
                or client_y < 0
                or client_x >= width
                or client_y >= height
            ):
                return BoundaryDecision(False, "outside_target_client", hit_hwnd=hit)
            return BoundaryDecision(
                True,
                "target_window",
                client_x=client_x,
                client_y=client_y,
                x_ratio=round(client_x / width, 4),
                y_ratio=round(client_y / height, 4),
                hit_hwnd=hit,
            )
        except Exception:
            return BoundaryDecision(False, "mouse_boundary_query_failed")

    def _belongs_to_frozen_root(self, hwnd):
        root = self.adapter.root_hwnd(hwnd)
        if root == self.frozen.root_hwnd:
            return True
        # A modal/tool popup can have its own root. It is accepted only when a
        # concrete owner chain reaches the frozen root; PID equality alone is
        # deliberately insufficient.
        seen = set()
        candidate = root
        while candidate and candidate not in seen:
            seen.add(candidate)
            owner = self.adapter.owner_hwnd(candidate)
            if not owner:
                return False
            owner_root = self.adapter.root_hwnd(owner)
            if owner == self.frozen.root_hwnd or owner_root == self.frozen.root_hwnd:
                return True
            candidate = owner_root or owner
        return False
