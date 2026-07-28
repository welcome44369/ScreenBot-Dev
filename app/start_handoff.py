"""Thread-safe state for deferred, user-driven target foreground handoff."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import copy
import threading
import time
from typing import Any
from uuid import uuid4


class StartRequestKind(str, Enum):
    DIRECT_SCRIPT = "direct_script"
    WORKFLOW = "workflow"


class StartHandoffState(str, Enum):
    IDLE = "idle"
    ARMED = "armed"
    COMMITTING = "committing"
    STARTED = "started"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    TARGET_CHANGED = "target_changed"
    TARGET_UNAVAILABLE = "target_unavailable"
    FAILED = "failed"


class StartHandoffCode(str, Enum):
    ARMED = "armed"
    ALREADY_ARMED = "already_armed"
    CLAIMED = "claimed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    TARGET_CHANGED = "target_changed"
    TARGET_UNAVAILABLE = "target_unavailable"
    STARTED = "started"
    FAILED = "failed"
    NOT_ARMED = "not_armed"
    REQUEST_MISMATCH = "request_mismatch"
    ALREADY_COMPLETED = "already_completed"


@dataclass(frozen=True)
class PendingStartRequest:
    request_id: str
    kind: StartRequestKind
    target_session_id: str
    target_generation: int
    display_name: str
    frozen_payload: Any
    requested_at_monotonic: float
    expires_at_monotonic: float


@dataclass(frozen=True)
class StartHandoffSnapshot:
    state: StartHandoffState
    request: PendingStartRequest | None
    reason: str | None
    display_name: str | None
    remaining_ms: int


@dataclass(frozen=True)
class StartHandoffResult:
    code: StartHandoffCode
    snapshot: StartHandoffSnapshot
    request: PendingStartRequest | None = None


class StartHandoffService:
    """Owns one pending request; callers perform all runtime work outside its lock."""

    def __init__(self, timeout_seconds: float = 15.0):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = float(timeout_seconds)
        self._lock = threading.RLock()
        self._state = StartHandoffState.IDLE
        self._request: PendingStartRequest | None = None
        self._reason: str | None = None
        self._presentation_name: str | None = None

    def create_request(
        self,
        *,
        kind: StartRequestKind,
        target_session_id: str,
        target_generation: int,
        display_name: str,
        frozen_payload: Any,
        now: float | None = None,
    ) -> PendingStartRequest:
        timestamp = time.monotonic() if now is None else float(now)
        return PendingStartRequest(
            request_id=uuid4().hex,
            kind=kind,
            target_session_id=str(target_session_id),
            target_generation=int(target_generation),
            display_name=str(display_name),
            frozen_payload=copy.deepcopy(frozen_payload),
            requested_at_monotonic=timestamp,
            expires_at_monotonic=timestamp + self._timeout_seconds,
        )

    def arm(self, request: PendingStartRequest) -> StartHandoffResult:
        if not isinstance(request, PendingStartRequest):
            raise TypeError("request must be a PendingStartRequest")
        with self._lock:
            if self._state in {StartHandoffState.ARMED, StartHandoffState.COMMITTING}:
                return self._result_locked(StartHandoffCode.ALREADY_ARMED)
            self._state = StartHandoffState.ARMED
            self._request = request
            self._reason = None
            self._presentation_name = request.display_name
            return self._result_locked(StartHandoffCode.ARMED, request)

    def get_snapshot(self, now: float | None = None) -> StartHandoffSnapshot:
        with self._lock:
            return self._snapshot_locked(now)

    def claim_commit(self, request_id: str, now: float | None = None) -> StartHandoffResult:
        with self._lock:
            if self._state != StartHandoffState.ARMED or self._request is None:
                return self._result_locked(StartHandoffCode.NOT_ARMED, now=now)
            if self._request.request_id != request_id:
                return self._result_locked(StartHandoffCode.REQUEST_MISMATCH, now=now)
            if self._is_expired_locked(now):
                return self._transition_locked(
                    StartHandoffState.TIMED_OUT, StartHandoffCode.TIMED_OUT,
                    "target_foreground_timeout", now,
                )
            request = self._request
            self._state = StartHandoffState.COMMITTING
            self._reason = None
            return self._result_locked(StartHandoffCode.CLAIMED, request, now)

    def cancel(self, reason: str = "start_request_cancelled") -> StartHandoffResult:
        with self._lock:
            if self._state != StartHandoffState.ARMED:
                return self._result_locked(StartHandoffCode.NOT_ARMED)
            return self._transition_locked(
                StartHandoffState.CANCELLED, StartHandoffCode.CANCELLED, reason
            )

    def expire_if_due(self, now: float | None = None) -> StartHandoffResult:
        with self._lock:
            if self._state != StartHandoffState.ARMED:
                return self._result_locked(StartHandoffCode.NOT_ARMED, now=now)
            if not self._is_expired_locked(now):
                return self._result_locked(StartHandoffCode.NOT_ARMED, now=now)
            return self._transition_locked(
                StartHandoffState.TIMED_OUT, StartHandoffCode.TIMED_OUT,
                "target_foreground_timeout", now,
            )

    def invalidate_target(self, reason: str, unavailable: bool = False) -> StartHandoffResult:
        with self._lock:
            if self._state != StartHandoffState.ARMED:
                return self._result_locked(StartHandoffCode.NOT_ARMED)
            state = (
                StartHandoffState.TARGET_UNAVAILABLE
                if unavailable else StartHandoffState.TARGET_CHANGED
            )
            code = (
                StartHandoffCode.TARGET_UNAVAILABLE
                if unavailable else StartHandoffCode.TARGET_CHANGED
            )
            return self._transition_locked(state, code, reason)

    def complete_started(self, request_id: str) -> StartHandoffResult:
        with self._lock:
            return self._complete_locked(
                request_id, StartHandoffState.STARTED, StartHandoffCode.STARTED, None
            )

    def complete_failed(self, request_id: str, reason: str) -> StartHandoffResult:
        with self._lock:
            return self._complete_locked(
                request_id, StartHandoffState.FAILED, StartHandoffCode.FAILED, reason
            )

    def clear_terminal_presentation(self) -> None:
        with self._lock:
            if self._state not in {StartHandoffState.ARMED, StartHandoffState.COMMITTING}:
                self._state = StartHandoffState.IDLE
                self._request = None
                self._reason = None
                self._presentation_name = None

    def _complete_locked(
        self,
        request_id: str,
        state: StartHandoffState,
        code: StartHandoffCode,
        reason: str | None,
    ) -> StartHandoffResult:
        if self._state != StartHandoffState.COMMITTING or self._request is None:
            return self._result_locked(StartHandoffCode.ALREADY_COMPLETED)
        if self._request.request_id != request_id:
            return self._result_locked(StartHandoffCode.REQUEST_MISMATCH)
        return self._transition_locked(state, code, reason)

    def _transition_locked(
        self,
        state: StartHandoffState,
        code: StartHandoffCode,
        reason: str | None,
        now: float | None = None,
    ) -> StartHandoffResult:
        request = self._request
        self._state = state
        self._reason = reason
        self._presentation_name = request.display_name if request is not None else self._presentation_name
        self._request = None
        return self._result_locked(code, request, now)

    def _result_locked(
        self,
        code: StartHandoffCode,
        request: PendingStartRequest | None = None,
        now: float | None = None,
    ) -> StartHandoffResult:
        return StartHandoffResult(code, self._snapshot_locked(now), request)

    def _snapshot_locked(self, now: float | None = None) -> StartHandoffSnapshot:
        request = self._request
        timestamp = time.monotonic() if now is None else float(now)
        remaining_ms = 0
        if self._state == StartHandoffState.ARMED and request is not None:
            remaining_ms = max(0, int(round((request.expires_at_monotonic - timestamp) * 1000)))
        return StartHandoffSnapshot(
            state=self._state,
            request=request,
            reason=self._reason,
            display_name=self._presentation_name,
            remaining_ms=remaining_ms,
        )

    def _is_expired_locked(self, now: float | None) -> bool:
        return (
            self._request is not None
            and (time.monotonic() if now is None else float(now))
            >= self._request.expires_at_monotonic
        )
