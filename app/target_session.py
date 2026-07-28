"""Authoritative target identity and lifecycle state for ScreenBot.

Phase A deliberately stops at target identity. Capture freshness, input
capabilities, and automatic HWND rebinding belong to later phases.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import os
import threading
import time
from typing import Callable
from uuid import uuid4


class TargetConnectionState(str, Enum):
    UNLOCKED = "UNLOCKED"
    ATTACHED = "ATTACHED"
    SUSPENDED = "SUSPENDED"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"


class TargetVisibilityState(str, Enum):
    FOREGROUND = "FOREGROUND"
    BACKGROUND = "BACKGROUND"
    MINIMIZED = "MINIMIZED"
    HIDDEN = "HIDDEN"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TargetSnapshot:
    session_id: str
    generation: int
    hwnd: int
    root_hwnd: int
    client_hwnd: int
    pid: int
    process_creation_time: int | float | None
    executable_path: str | None
    process_name: str
    window_class: str
    window_title: str
    window_rect: tuple[int, int, int, int]
    client_screen_origin: tuple[int, int]
    initial_client_size: tuple[int, int]
    current_client_size: tuple[int, int]
    dpi: int
    locked_at: float
    last_validated_at: float
    connection_state: TargetConnectionState
    visibility_state: TargetVisibilityState
    identity_valid: bool
    disconnect_reason: str | None
    identity_strength: str
    identity_degraded_reason: str | None


TargetListener = Callable[[str, TargetSnapshot | None, dict], None]


class TargetSessionService:
    """Thread-safe owner of the single current target session."""

    def __init__(self, window_adapter, logger=None):
        self._window_adapter = window_adapter
        self.logger = logger or logging.getLogger("ScreenBot")
        self._lock = threading.RLock()
        self._current: TargetSnapshot | None = None
        self._generation = 0
        self._listeners: list[TargetListener] = []

    def get_snapshot(self) -> TargetSnapshot | None:
        with self._lock:
            return self._current

    def has_target(self) -> bool:
        with self._lock:
            return self._current is not None

    def subscribe(self, listener: TargetListener) -> None:
        if not callable(listener):
            raise TypeError("Target listener must be callable")
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def unsubscribe(self, listener: TargetListener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def lock_foreground_target(self) -> TargetSnapshot:
        """Atomically replace the target after all identity queries succeed."""
        hwnd = int(self._window_adapter.get_foreground_hwnd() or 0)
        if not hwnd:
            raise RuntimeError("No foreground target window is available")
        info = self._window_adapter.query_window(hwnd)
        if int(info.pid) == os.getpid():
            raise RuntimeError("A ScreenBot window cannot be used as the target")

        now = time.monotonic()
        degraded = tuple(getattr(info, "identity_degraded_reasons", ()) or ())
        snapshot = TargetSnapshot(
            session_id=uuid4().hex,
            generation=0,
            hwnd=int(info.hwnd),
            root_hwnd=int(info.root_hwnd),
            client_hwnd=int(info.client_hwnd),
            pid=int(info.pid),
            process_creation_time=info.process_creation_time,
            executable_path=info.executable_path,
            process_name=info.process_name,
            window_class=info.window_class,
            window_title=info.title,
            window_rect=(
                int(info.window_left),
                int(info.window_top),
                int(info.window_right),
                int(info.window_bottom),
            ),
            client_screen_origin=(int(info.client_left), int(info.client_top)),
            initial_client_size=(int(info.client_width), int(info.client_height)),
            current_client_size=(int(info.client_width), int(info.client_height)),
            dpi=int(info.dpi or 0),
            locked_at=now,
            last_validated_at=now,
            connection_state=TargetConnectionState.ATTACHED,
            visibility_state=self._visibility_for(info),
            identity_valid=True,
            disconnect_reason=None,
            identity_strength="degraded" if degraded else "strong",
            identity_degraded_reason="; ".join(degraded) or None,
        )
        with self._lock:
            self._generation += 1
            snapshot = replace(snapshot, generation=self._generation)
            self._current = snapshot
        self._emit("TARGET_SESSION_CREATED", snapshot, reason="explicit_f8_lock")
        return snapshot

    def refresh(self) -> TargetSnapshot | None:
        """Refresh dynamic fields while preserving session and generation."""
        with self._lock:
            expected = self._current
        if expected is None:
            return None
        if expected.connection_state == TargetConnectionState.DISCONNECTED:
            return expected
        if not self._window_adapter.validate_window(expected.hwnd):
            return self.disconnect("INVALID_HWND", expected_session_id=expected.session_id)

        try:
            info = self._window_adapter.query_window(expected.hwnd)
        except Exception as exc:
            if not self._window_adapter.validate_window(expected.hwnd):
                return self.disconnect(
                    "INVALID_HWND", expected_session_id=expected.session_id
                )
            return self.disconnect(
                f"WINDOW_QUERY_FAILED:{type(exc).__name__}",
                expected_session_id=expected.session_id,
            )

        mismatch = self._identity_mismatch(expected, info)
        if mismatch:
            self._emit(
                "TARGET_IDENTITY_MISMATCH",
                expected,
                reason=mismatch,
                observed_pid=int(info.pid),
                observed_process_creation_time=info.process_creation_time,
                observed_executable_path=info.executable_path,
                observed_window_class=info.window_class,
                observed_root_hwnd=int(info.root_hwnd),
                observed_client_hwnd=int(info.client_hwnd),
            )
            return self.disconnect(mismatch, expected_session_id=expected.session_id)

        degraded = list(getattr(info, "identity_degraded_reasons", ()) or ())
        if expected.identity_strength == "degraded":
            degraded.extend(
                reason.strip()
                for reason in (expected.identity_degraded_reason or "").split(";")
                if reason.strip()
            )
        degraded = tuple(dict.fromkeys(degraded))
        refreshed = replace(
            expected,
            window_title=info.title,
            window_rect=(
                int(info.window_left),
                int(info.window_top),
                int(info.window_right),
                int(info.window_bottom),
            ),
            client_screen_origin=(int(info.client_left), int(info.client_top)),
            current_client_size=(int(info.client_width), int(info.client_height)),
            dpi=int(info.dpi or 0),
            last_validated_at=time.monotonic(),
            visibility_state=self._visibility_for(info),
            connection_state=TargetConnectionState.ATTACHED,
            identity_valid=True,
            disconnect_reason=None,
            identity_strength="degraded" if degraded else "strong",
            identity_degraded_reason="; ".join(degraded) or None,
        )
        with self._lock:
            if (
                self._current is None
                or self._current.session_id != expected.session_id
                or self._current.generation != expected.generation
            ):
                return self._current
            previous = self._current
            self._current = refreshed

        if previous.visibility_state != refreshed.visibility_state:
            self._emit(
                "TARGET_STATE_CHANGED",
                refreshed,
                reason="visibility_changed",
                previous_visibility=previous.visibility_state.value,
            )
        else:
            self._notify_listeners("TARGET_SNAPSHOT_UPDATED", refreshed, {})
        return refreshed

    def disconnect(
        self, reason: str, *, expected_session_id: str | None = None
    ) -> TargetSnapshot | None:
        with self._lock:
            current = self._current
            if current is None:
                return None
            if expected_session_id and current.session_id != expected_session_id:
                return current
            if current.connection_state == TargetConnectionState.DISCONNECTED:
                return current
            disconnected = replace(
                current,
                last_validated_at=time.monotonic(),
                connection_state=TargetConnectionState.DISCONNECTED,
                visibility_state=TargetVisibilityState.UNKNOWN,
                identity_valid=False,
                disconnect_reason=str(reason),
            )
            self._current = disconnected
        self._emit("TARGET_DISCONNECTED", disconnected, reason=str(reason))
        return disconnected

    def clear(self, reason="explicit_clear") -> None:
        with self._lock:
            previous = self._current
            self._current = None
        if previous is not None:
            self._emit("TARGET_SESSION_CLEARED", previous, reason=str(reason))

    @staticmethod
    def _visibility_for(info) -> TargetVisibilityState:
        if bool(getattr(info, "minimized", False)):
            return TargetVisibilityState.MINIMIZED
        if not bool(getattr(info, "visible", True)):
            return TargetVisibilityState.HIDDEN
        if bool(getattr(info, "foreground", False)):
            return TargetVisibilityState.FOREGROUND
        return TargetVisibilityState.BACKGROUND

    @staticmethod
    def _same_path(left: str | None, right: str | None) -> bool:
        if left is None or right is None:
            return True
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(right)
        )

    @classmethod
    def _identity_mismatch(cls, expected: TargetSnapshot, info) -> str | None:
        if int(info.pid) != expected.pid:
            return "PID_MISMATCH"
        if (
            expected.process_creation_time is not None
            and info.process_creation_time is not None
            and info.process_creation_time != expected.process_creation_time
        ):
            return "PROCESS_INSTANCE_MISMATCH"
        if not cls._same_path(expected.executable_path, info.executable_path):
            return "EXECUTABLE_MISMATCH"
        if info.window_class != expected.window_class:
            return "WINDOW_CLASS_MISMATCH"
        if int(info.root_hwnd) != expected.root_hwnd:
            return "ROOT_HWND_MISMATCH"
        if int(info.client_hwnd) != expected.client_hwnd:
            return "CLIENT_HWND_MISMATCH"
        return None

    def _base_payload(self, snapshot: TargetSnapshot | None) -> dict:
        if snapshot is None:
            return {"timestamp": datetime.now(timezone.utc).astimezone().isoformat()}
        return {
            "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
            "session_id": snapshot.session_id,
            "generation": snapshot.generation,
            "hwnd": snapshot.hwnd,
            "root_hwnd": snapshot.root_hwnd,
            "client_hwnd": snapshot.client_hwnd,
            "pid": snapshot.pid,
            "process_name": snapshot.process_name,
            "executable_path": snapshot.executable_path,
            "process_creation_time": snapshot.process_creation_time,
            "window_class": snapshot.window_class,
            "window_title": snapshot.window_title,
            "client_size": list(snapshot.current_client_size),
            "dpi": snapshot.dpi,
            "connection_state": snapshot.connection_state.value,
            "visibility_state": snapshot.visibility_state.value,
            "identity_valid": snapshot.identity_valid,
            "identity_strength": snapshot.identity_strength,
        }

    def _emit(self, event: str, snapshot: TargetSnapshot | None, **data) -> None:
        payload = self._base_payload(snapshot)
        payload.update(data)
        log = (
            self.logger.warning
            if event in {"TARGET_IDENTITY_MISMATCH", "TARGET_DISCONNECTED"}
            else self.logger.info
        )
        log("%s %s", event, json.dumps(payload, ensure_ascii=False, sort_keys=True))
        self._notify_listeners(event, snapshot, payload)

    def _notify_listeners(
        self, event: str, snapshot: TargetSnapshot | None, payload: dict
    ) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(event, snapshot, dict(payload))
            except Exception:
                self.logger.exception("TargetSession listener failed event=%s", event)
