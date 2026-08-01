"""Passive, semantic macro recorder backed by pynput on Windows.

Input callbacks do not suppress events or touch Qt.  They only enqueue small
records; a worker normalizes those records into compact Macro JSON.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
import logging
from queue import Empty, Queue
import threading
import time

from pynput import keyboard as pynput_keyboard
from pynput import mouse as pynput_mouse

from app.target_boundary import FrozenTargetBoundary, TargetBoundaryFilter


HOTKEY_BLOCKLIST = {"f8", "f9", "esc"}
CLICK_DISTANCE_PX = 6
SCROLL_COALESCE_MS = 120


@dataclass(frozen=True)
class _RawInputEvent:
    kind: str
    timestamp: float
    payload: dict


def _key_name(key):
    """Return a stable player-facing key name and its virtual-key code."""
    char = getattr(key, "char", None)
    vk = getattr(key, "vk", None)
    if isinstance(char, str) and char:
        return char.lower(), vk
    name = getattr(key, "name", None)
    if not name:
        text = str(key).removeprefix("Key.")
        name = text if text else None
    if not name:
        return (f"vk_{vk}" if vk is not None else None), vk
    aliases = {
        "return": "enter", "esc": "esc", "space": "space",
        "shift_l": "shift_l", "shift_r": "shift_r",
        "ctrl_l": "ctrl_l", "ctrl_r": "ctrl_r",
        "alt_l": "alt_l", "alt_r": "alt_r",
        "cmd_l": "win_l", "cmd_r": "win_r",
    }
    value = aliases.get(name, name)
    return value.lower(), vk


def _button_name(button):
    return str(button).removeprefix("Button.").lower()


class ActionRecorder:
    """Observe target input without becoming an input intermediary.

    The recorder writes version 2 semantic events.  Existing version 1 event
    files remain readable by ``ScriptPlayer`` unchanged.
    """

    def __init__(
        self,
        window_tracker,
        settings,
        on_event=None,
        on_click=None,
        *,
        target_session=None,
        boundary_adapter=None,
        on_invalidated=None,
    ):
        self.window_tracker = window_tracker
        self.settings = settings
        self.target_session = target_session
        self._boundary_adapter = boundary_adapter
        self.logger = logging.getLogger("ScreenBot")
        self.actions = []
        self._lock = threading.RLock()
        self._queue: Queue[_RawInputEvent | None] = Queue()
        self._worker = None
        self._keyboard_listener = None
        self._mouse_listener = None
        self._recording = False
        self._accepting_events = False
        self._target_hwnd = None
        self._frozen_boundary = None
        self._boundary_filter = None
        self._target_listener_registered = False
        self._invalidation_notified = False
        self._stop_reason = None
        self._last_script = None
        self._start_time = None
        self._last_action_end = None
        self._pressed_keys = {}
        self._mouse_down = {}
        self._raw_mouse_buttons = set()
        self._last_scroll = None
        self._focus_paused = False
        self.on_event = on_event
        self.on_click = on_click
        self.on_invalidated = on_invalidated
        self._diagnostics = {}
        self._reset_diagnostics()

    def _reset_diagnostics(self):
        self._diagnostics = {
            "backend": "pynput-win32",
            "keyboard_listener_running": False,
            "mouse_listener_running": False,
            "suppress_enabled": False,
            "target_hwnd": None,
            "session_id": None,
            "generation": None,
            "stop_reason": None,
            "target_foreground": False,
            "raw_event_count": 0,
            "normalized_event_count": 0,
            "ignored_move_count": 0,
            "ignored_hotkey_event_count": 0,
            "open_key_count": 0,
            "open_mouse_button_count": 0,
            "queue_depth": 0,
            "listener_error": None,
        }

    def start(self):
        if self._recording:
            return
        if self.target_session is None:
            raise RuntimeError("Recording requires the authoritative TargetSession.")
        snapshot = self.target_session.get_snapshot()
        frozen = FrozenTargetBoundary.from_snapshot(snapshot)
        self.actions = []
        self._queue = Queue()
        self._pressed_keys.clear()
        self._mouse_down.clear()
        self._raw_mouse_buttons.clear()
        self._last_scroll = None
        self._reset_diagnostics()
        self._frozen_boundary = frozen
        self._boundary_filter = TargetBoundaryFilter(
            self.target_session,
            frozen,
            adapter=self._boundary_adapter,
        )
        self._target_hwnd = frozen.client_hwnd
        self._diagnostics["target_hwnd"] = self._target_hwnd
        self._diagnostics["session_id"] = frozen.session_id
        self._diagnostics["generation"] = frozen.generation
        self._stop_reason = None
        self._last_script = None
        self._invalidation_notified = False
        self._start_time = time.monotonic()
        self._last_action_end = self._start_time
        self._recording = True
        self._accepting_events = True
        self.target_session.subscribe(self._on_target_session_event)
        self._target_listener_registered = True
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="ScreenBot-MacroNormalizer",
            daemon=True,
        )
        self._worker.start()
        try:
            # Explicit passive contract: neither listener is allowed to
            # suppress, filter, or reinject any physical input event.
            self._keyboard_listener = pynput_keyboard.Listener(
                on_press=self._on_key_press,
                on_release=self._on_key_release,
                suppress=False,
            )
            self._mouse_listener = pynput_mouse.Listener(
                on_move=self._on_mouse_move,
                on_click=self._on_mouse_click,
                on_scroll=self._on_mouse_scroll,
                suppress=False,
            )
            self._keyboard_listener.start()
            self._mouse_listener.start()
            self._diagnostics["keyboard_listener_running"] = True
            self._diagnostics["mouse_listener_running"] = True
            # Macro Manager may have received the start click.  Return focus
            # once at start only; no input callback ever changes foreground.
            ctypes.windll.user32.SetForegroundWindow(frozen.root_hwnd)
            self.logger.info(
                "[MacroRecorder] Backend=pynput-win32 suppress=false Target HWND=0x%08X Passive recording started",
                self._target_hwnd,
            )
        except Exception as exc:
            self._diagnostics["listener_error"] = str(exc)
            self._recording = False
            self._accepting_events = False
            self._stop_listeners()
            self._unsubscribe_target_session()
            self._queue.put(None)
            if self._worker is not None:
                self._worker.join(timeout=2)
            raise RuntimeError(f"Unable to start passive input recorder: {exc}") from exc

    def stop(self, reason="normal_stop"):
        if not self._recording:
            return self._last_script
        self._accepting_events = False
        self._recording = False
        self._stop_reason = self._stop_reason or str(reason)
        self._diagnostics["stop_reason"] = self._stop_reason
        self._unsubscribe_target_session()
        self._stop_listeners()
        self._queue.put(None)
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=3)
            self._worker = None
        # Incomplete transactions are discarded. Recorder never fabricates a
        # release event that was not observed inside the frozen boundary.
        self._pressed_keys.clear()
        self._mouse_down.clear()
        self._raw_mouse_buttons.clear()
        self._last_scroll = None
        self._diagnostics["open_key_count"] = 0
        self._diagnostics["open_mouse_button_count"] = 0
        duration_ms = int(round((time.monotonic() - self._start_time) * 1000)) if self._start_time else 0
        self.logger.info(
            "[MacroRecorder] Recording stopped raw_events=%s normalized_events=%s ignored_moves=%s duration_ms=%s",
            self._diagnostics["raw_event_count"], self._diagnostics["normalized_event_count"],
            self._diagnostics["ignored_move_count"], duration_ms,
        )
        self._last_script = self._build_script()
        return self._last_script

    def is_recording(self):
        return self._recording

    def get_diagnostics(self):
        with self._lock:
            result = dict(self._diagnostics)
            result["queue_depth"] = self._queue.qsize()
            result["open_key_count"] = len(self._pressed_keys)
            result["open_mouse_button_count"] = len(self._mouse_down)
            result["target_foreground"] = self._is_target_foreground()
            return result

    # pynput listener callbacks: intentionally tiny and non-blocking.
    def _on_key_press(self, key):
        self._enqueue("key_down", {"key": key})

    def _on_key_release(self, key):
        self._enqueue("key_up", {"key": key})

    def _on_mouse_move(self, _x, _y):
        with self._lock:
            gesture_open = bool(self._raw_mouse_buttons)
        if gesture_open:
            self._enqueue("mouse_move", {"x": int(_x), "y": int(_y)})

    def _on_mouse_click(self, x, y, button, pressed):
        button_name = _button_name(button)
        with self._lock:
            if pressed:
                self._raw_mouse_buttons.add(button_name)
        self._enqueue("mouse_down" if pressed else "mouse_up", {
            "x": int(x), "y": int(y), "button": button_name,
        })
        if not pressed:
            with self._lock:
                self._raw_mouse_buttons.discard(button_name)

    def _on_mouse_scroll(self, x, y, dx, dy):
        self._enqueue("scroll", {
            "x": int(x), "y": int(y), "dx": int(dx), "dy": int(dy),
        })

    def _enqueue(self, kind, payload):
        if not self._accepting_events:
            return
        try:
            self._queue.put_nowait(_RawInputEvent(kind, time.monotonic(), payload))
            with self._lock:
                self._diagnostics["raw_event_count"] += 1
                self._diagnostics["queue_depth"] = self._queue.qsize()
        except Exception as exc:
            with self._lock:
                self._diagnostics["listener_error"] = str(exc)
            self.logger.exception("[MacroRecorder] queue enqueue failed")

    def _worker_loop(self):
        while True:
            try:
                raw = self._queue.get(timeout=0.1)
            except Empty:
                if not self._recording and not self._accepting_events:
                    continue
                continue
            if raw is None:
                return
            try:
                self._normalize(raw)
            except Exception as exc:
                with self._lock:
                    self._diagnostics["listener_error"] = str(exc)
                self.logger.exception("[MacroRecorder] event normalizer failed")

    def _normalize(self, raw):
        if raw.kind.startswith("key_"):
            self._normalize_key(raw)
        elif raw.kind in {"mouse_down", "mouse_up"}:
            self._normalize_mouse_button(raw)
        elif raw.kind == "mouse_move":
            self._normalize_mouse_move(raw)
        elif raw.kind == "scroll":
            self._normalize_scroll(raw)

    def _normalize_key(self, raw):
        name, scan_code = _key_name(raw.payload["key"])
        if not name:
            return
        key_id = (name, scan_code)
        decision = self._boundary_filter.authorize_keyboard()
        if not decision.session_valid:
            self._invalidate_target(decision.reason)
            return
        if name in HOTKEY_BLOCKLIST:
            with self._lock:
                self._diagnostics["ignored_hotkey_event_count"] += 1
            return
        is_down = raw.kind == "key_down"
        if is_down:
            if not decision.accepted or key_id in self._pressed_keys:
                return
            self._pressed_keys[key_id] = (name, scan_code, raw.timestamp)
        elif key_id in self._pressed_keys:
            name, scan_code, started = self._pressed_keys.pop(key_id)
            if decision.accepted:
                self._record_action(
                    {
                        "type": "key",
                        "event": "down",
                        "key": name,
                        "scan_code": scan_code,
                    },
                    started,
                )
                self._record_action(
                    {
                        "type": "key",
                        "event": "up",
                        "key": name,
                        "scan_code": scan_code,
                    },
                    raw.timestamp,
                )
        with self._lock:
            self._diagnostics["open_key_count"] = len(self._pressed_keys)

    def _normalize_mouse_button(self, raw):
        button = raw.payload["button"]
        point = self._boundary_filter.authorize_mouse(
            raw.payload["x"], raw.payload["y"]
        )
        if not point.session_valid:
            self._invalidate_target(point.reason)
            return
        if raw.kind == "mouse_down":
            if not point.accepted:
                return
            self._mouse_down[button] = (raw.timestamp, point)
            return
        pending = self._mouse_down.pop(button, None)
        if pending is None:
            return
        started, start_point = pending
        foreground = self._boundary_filter.authorize_keyboard()
        if not foreground.session_valid:
            self._invalidate_target(foreground.reason)
            return
        if not point.accepted or not foreground.accepted:
            return
        distance = (
            (point.client_x - start_point.client_x) ** 2
            + (point.client_y - start_point.client_y) ** 2
        ) ** 0.5
        duration_ms = max(0, int(round((raw.timestamp - started) * 1000)))
        if distance <= CLICK_DISTANCE_PX:
            action = {
                "type": "click", "button": button,
                "client_x": start_point.client_x,
                "client_y": start_point.client_y,
                "x_ratio": start_point.x_ratio,
                "y_ratio": start_point.y_ratio,
                "duration_ms": duration_ms,
            }
            self._record_action(action, started, completes_at=raw.timestamp)
            self._notify_click(action, raw.payload["x"], raw.payload["y"])
        else:
            self._record_action({
                "type": "drag", "button": button,
                "start_client_x": start_point.client_x,
                "start_client_y": start_point.client_y,
                "end_client_x": point.client_x,
                "end_client_y": point.client_y,
                "start_x_ratio": start_point.x_ratio,
                "start_y_ratio": start_point.y_ratio,
                "end_x_ratio": point.x_ratio,
                "end_y_ratio": point.y_ratio,
                "duration_ms": duration_ms,
            }, started, completes_at=raw.timestamp)
        with self._lock:
            self._diagnostics["open_mouse_button_count"] = len(self._mouse_down)

    def _normalize_mouse_move(self, raw):
        if not self._mouse_down:
            with self._lock:
                self._diagnostics["ignored_move_count"] += 1
            return
        point = self._boundary_filter.authorize_mouse(
            raw.payload["x"], raw.payload["y"]
        )
        if not point.session_valid:
            self._invalidate_target(point.reason)
            return
        foreground = self._boundary_filter.authorize_keyboard()
        if not foreground.session_valid:
            self._invalidate_target(foreground.reason)
            return
        if not point.accepted or not foreground.accepted:
            self._mouse_down.clear()
            with self._lock:
                self._diagnostics["open_mouse_button_count"] = 0
        with self._lock:
            self._diagnostics["ignored_move_count"] += 1

    def _normalize_scroll(self, raw):
        point = self._boundary_filter.authorize_mouse(
            raw.payload["x"], raw.payload["y"]
        )
        if not point.session_valid:
            self._invalidate_target(point.reason)
            return
        foreground = self._boundary_filter.authorize_keyboard()
        if not foreground.session_valid:
            self._invalidate_target(foreground.reason)
            return
        if not point.accepted or not foreground.accepted:
            return
        if self._last_scroll is not None:
            last_action, last_time, last_point = self._last_scroll
            point_key = (point.client_x, point.client_y)
            if raw.timestamp - last_time <= SCROLL_COALESCE_MS / 1000 and last_point == point_key:
                last_action["dx"] += raw.payload["dx"]
                last_action["dy"] += raw.payload["dy"]
                last_action["delta"] += raw.payload["dy"]
                self._last_scroll = (last_action, raw.timestamp, point_key)
                self._last_action_end = raw.timestamp
                return
        action = {
            "type": "scroll",
            "client_x": point.client_x,
            "client_y": point.client_y,
            "x_ratio": point.x_ratio,
            "y_ratio": point.y_ratio,
            "dx": raw.payload["dx"], "dy": raw.payload["dy"], "delta": raw.payload["dy"],
        }
        self._record_action(action, raw.timestamp)
        self._last_scroll = (
            action,
            raw.timestamp,
            (point.client_x, point.client_y),
        )

    def _record_action(self, action, timestamp, *, completes_at=None):
        with self._lock:
            anchor = self._last_action_end if self._last_action_end is not None else timestamp
            action["delay_ms"] = max(0, int(round((timestamp - anchor) * 1000)))
            action["timestamp_ms"] = max(0, int(round((timestamp - self._start_time) * 1000)))
            self.actions.append(action)
            self._last_action_end = completes_at if completes_at is not None else timestamp
            self._last_scroll = None if action.get("type") != "scroll" else self._last_scroll
            self._diagnostics["normalized_event_count"] = len(self.actions)
            count = len(self.actions)
        if callable(self.on_event):
            try:
                self.on_event(count)
            except Exception:
                self.logger.exception("[MacroRecorder] event-count callback failed")

    def _notify_click(self, action, screen_x, screen_y):
        if callable(self.on_click):
            payload = dict(action)
            payload["screen_x"] = int(screen_x)
            payload["screen_y"] = int(screen_y)
            try:
                self.on_click(payload)
            except Exception:
                self.logger.exception("[MacroRecorder] click callback failed")

    def _is_target_foreground(self):
        if self._boundary_filter is None:
            return False
        try:
            is_foreground = self._boundary_filter.authorize_keyboard().accepted
            with self._lock:
                self._diagnostics["target_foreground"] = is_foreground
            return is_foreground
        except Exception:
            return False

    def _on_target_session_event(self, event, _snapshot, _payload):
        if event in {"TARGET_DISCONNECTED", "TARGET_SESSION_CLEARED"}:
            self._invalidate_target("target_invalidated")

    def _invalidate_target(self, reason):
        with self._lock:
            if not self._recording or self._invalidation_notified:
                return
            self._accepting_events = False
            self._stop_reason = "target_invalidated"
            self._diagnostics["stop_reason"] = self._stop_reason
            self._pressed_keys.clear()
            self._mouse_down.clear()
            self._raw_mouse_buttons.clear()
            self._last_scroll = None
            self._diagnostics["open_key_count"] = 0
            self._diagnostics["open_mouse_button_count"] = 0
            self._invalidation_notified = True
        self.logger.warning(
            "[MacroRecorder] Target invalidated; recording stop requested reason=%s",
            reason,
        )
        if callable(self.on_invalidated):
            try:
                self.on_invalidated("target_invalidated")
            except Exception:
                self.logger.exception("[MacroRecorder] invalidation callback failed")

    def _unsubscribe_target_session(self):
        if self._target_listener_registered and self.target_session is not None:
            self.target_session.unsubscribe(self._on_target_session_event)
            self._target_listener_registered = False

    def _stop_listeners(self):
        for listener_name in ("_keyboard_listener", "_mouse_listener"):
            listener = getattr(self, listener_name)
            if listener is not None:
                try:
                    listener.stop()
                    listener.join(timeout=1.0)
                except Exception:
                    self.logger.exception("[MacroRecorder] listener shutdown failed")
                setattr(self, listener_name, None)
        self._diagnostics["keyboard_listener_running"] = False
        self._diagnostics["mouse_listener_running"] = False

    def _build_script(self):
        return {
            "version": 2,
            "name": "New Script",
            "coordinate_space": "target_client_ratio",
            "events": list(self.actions),
        }
