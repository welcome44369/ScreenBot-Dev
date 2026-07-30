"""Test-only mouse/keyboard-shaped backend that never emits OS input."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RecordingInputBackend:
    """Drop-in replacement for the ``mouse`` module used by ScriptPlayer tests."""

    LEFT: str = "left"
    context: dict[str, Any] = field(default_factory=dict)
    scheduled_timestamp: float | None = None
    records: list[dict[str, Any]] = field(default_factory=list)

    @property
    def keyboard(self) -> "RecordingKeyboardBackend":
        return RecordingKeyboardBackend(self)

    def set_context(self, **context: Any) -> None:
        self.context.update(context)

    def record_authorization(self, allowed: bool, reason: str | None = None) -> None:
        record = self._record("authorization", accepted=allowed, rejection_reason=reason)
        self.context["authorization_timestamp"] = record["authorization_timestamp"]

    def _record(self, action_type: str, *, accepted: bool = True, rejection_reason: str | None = None, **data: Any) -> dict[str, Any]:
        now = time.monotonic()
        record = {
            "action_type": action_type,
            "target_identity": self.context.get("target_identity"),
            "target_hwnd": self.context.get("target_hwnd"),
            "coordinates": data.get("coordinates"),
            "button_or_key": data.get("button_or_key"),
            "scheduled_timestamp": self.scheduled_timestamp,
            "authorization_timestamp": now if action_type == "authorization" else self.context.get("authorization_timestamp"),
            "emission_attempt_timestamp": now if action_type != "authorization" else None,
            "accepted": accepted,
            "rejection_reason": rejection_reason,
            "execution_id": self.context.get("execution_id"),
            "workflow_id": self.context.get("workflow_id"),
            "script_id": self.context.get("script_id"),
        }
        self.records.append(record)
        return record

    def move(self, x: int, y: int) -> None:
        self._record("mouse.move", coordinates={"screen_x": x, "screen_y": y})

    def click(self, button: str = LEFT) -> None:
        self._record("mouse.click", button_or_key=button)

    def double_click(self, button: str = LEFT) -> None:
        self._record("mouse.double_click", button_or_key=button)

    def press(self, button: str = LEFT) -> None:
        self._record("mouse.press", button_or_key=button)

    def release(self, button: str = LEFT) -> None:
        self._record("mouse.release", button_or_key=button)

    def wheel(self, delta: int) -> None:
        self._record("mouse.wheel", button_or_key=str(delta))


@dataclass
class RecordingKeyboardBackend:
    """Keyboard companion for the same record list; it never calls ``keyboard``."""

    parent: RecordingInputBackend

    def press(self, key: str) -> None:
        self.parent._record("keyboard.press", button_or_key=str(key))

    def release(self, key: str) -> None:
        self.parent._record("keyboard.release", button_or_key=str(key))
