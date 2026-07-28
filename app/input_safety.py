"""Fail-closed authorization for ScreenBot's legacy global input backend."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.target_session import TargetConnectionState, TargetVisibilityState


class InputAuthorizationCode(str, Enum):
    ALLOWED = "ALLOWED"
    NO_TARGET = "NO_TARGET"
    TARGET_DISCONNECTED = "TARGET_DISCONNECTED"
    TARGET_CHANGED = "TARGET_CHANGED"
    TARGET_MINIMIZED = "TARGET_MINIMIZED"
    TARGET_HIDDEN = "TARGET_HIDDEN"
    TARGET_NOT_FOREGROUND = "TARGET_NOT_FOREGROUND"
    IDENTITY_INVALID = "IDENTITY_INVALID"
    IDENTITY_DEGRADED = "IDENTITY_DEGRADED"
    REFRESH_FAILED = "REFRESH_FAILED"


@dataclass(frozen=True)
class ExpectedTargetSession:
    session_id: str
    generation: int


@dataclass(frozen=True)
class InputAuthorizationResult:
    code: InputAuthorizationCode
    snapshot: object | None = None
    foreground_hwnd: int = 0
    foreground_root_hwnd: int = 0

    @property
    def allowed(self):
        return self.code is InputAuthorizationCode.ALLOWED


class ForegroundInputSafetyGate:
    """Reads target state; it never emits input or changes window focus."""

    def __init__(self, target_session, window_tracker, logger=None):
        self.target_session = target_session
        self.window_tracker = window_tracker
        self.logger = logger

    def expected_current_session(self):
        snapshot = self.target_session.get_snapshot()
        if snapshot is None:
            return None
        return ExpectedTargetSession(snapshot.session_id, snapshot.generation)

    def authorize_foreground_input(self, expected):
        try:
            snapshot = self.target_session.refresh()
        except Exception:
            return InputAuthorizationResult(InputAuthorizationCode.REFRESH_FAILED)
        return self._authorize(snapshot, expected)

    def authorize_foreground_input_fast(self, expected):
        return self._authorize(self.target_session.get_snapshot(), expected)

    def _authorize(self, snapshot, expected):
        if snapshot is None:
            return InputAuthorizationResult(InputAuthorizationCode.NO_TARGET)
        if expected is None or snapshot.session_id != expected.session_id or snapshot.generation != expected.generation:
            return InputAuthorizationResult(InputAuthorizationCode.TARGET_CHANGED, snapshot)
        if snapshot.connection_state != TargetConnectionState.ATTACHED:
            return InputAuthorizationResult(InputAuthorizationCode.TARGET_DISCONNECTED, snapshot)
        if not snapshot.identity_valid:
            return InputAuthorizationResult(InputAuthorizationCode.IDENTITY_INVALID, snapshot)
        if snapshot.identity_strength != "strong":
            return InputAuthorizationResult(InputAuthorizationCode.IDENTITY_DEGRADED, snapshot)
        if snapshot.visibility_state == TargetVisibilityState.MINIMIZED:
            return InputAuthorizationResult(InputAuthorizationCode.TARGET_MINIMIZED, snapshot)
        if snapshot.visibility_state == TargetVisibilityState.HIDDEN:
            return InputAuthorizationResult(InputAuthorizationCode.TARGET_HIDDEN, snapshot)
        foreground = int(self.window_tracker.get_foreground_hwnd() or 0)
        if not foreground:
            return InputAuthorizationResult(InputAuthorizationCode.TARGET_NOT_FOREGROUND, snapshot)
        try:
            foreground_root = int(self.window_tracker.query_window(foreground).root_hwnd)
        except Exception:
            return InputAuthorizationResult(InputAuthorizationCode.REFRESH_FAILED, snapshot, foreground)
        if foreground_root != int(snapshot.root_hwnd):
            return InputAuthorizationResult(InputAuthorizationCode.TARGET_NOT_FOREGROUND, snapshot, foreground, foreground_root)
        return InputAuthorizationResult(InputAuthorizationCode.ALLOWED, snapshot, foreground, foreground_root)
