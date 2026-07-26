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

    def __init__(self, window_tracker, settings, on_event=None, on_click=None):
        self.window_tracker = window_tracker
        self.settings = settings
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
        self._start_time = None
        self._last_action_end = None
        self._pressed_keys = {}
        self._mouse_down = {}
        self._last_scroll = None
        self._focus_paused = False
        self.on_event = on_event
        self.on_click = on_click
        self._diagnostics = {}
        self._reset_diagnostics()

    def _reset_diagnostics(self):
        self._diagnostics = {
            "backend": "pynput-win32",
            "keyboard_listener_running": False,
            "mouse_listener_running": False,
            "suppress_enabled": False,
            "target_hwnd": None,
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
        target = self.window_tracker.target
        if target is None:
            raise RuntimeError("Please lock a target window before recording.")
        self.actions = []
        self._queue = Queue()
        self._pressed_keys.clear()
        self._mouse_down.clear()
        self._last_scroll = None
        self._reset_diagnostics()
        self._target_hwnd = int(target.hwnd)
        self._diagnostics["target_hwnd"] = self._target_hwnd
        self._start_time = time.monotonic()
        self._last_action_end = self._start_time
        self._recording = True
        self._accepting_events = True
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
            ctypes.windll.user32.SetForegroundWindow(self._target_hwnd)
            self.logger.info(
                "[MacroRecorder] Backend=pynput-win32 suppress=false Target HWND=0x%08X Passive recording started",
                self._target_hwnd,
            )
        except Exception as exc:
            self._diagnostics["listener_error"] = str(exc)
            self._recording = False
            self._accepting_events = False
            self._stop_listeners()
            self._queue.put(None)
            if self._worker is not None:
                self._worker.join(timeout=2)
            raise RuntimeError(f"Unable to start passive input recorder: {exc}") from exc

    def stop(self):
        if not self._recording:
            return None
        self._accepting_events = False
        self._recording = False
        self._stop_listeners()
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=3)
            self._worker = None
        self._close_open_keys()
        self._mouse_down.clear()
        self._diagnostics["open_mouse_button_count"] = 0
        duration_ms = int(round((time.monotonic() - self._start_time) * 1000)) if self._start_time else 0
        self.logger.info(
            "[MacroRecorder] Recording stopped raw_events=%s normalized_events=%s ignored_moves=%s duration_ms=%s",
            self._diagnostics["raw_event_count"], self._diagnostics["normalized_event_count"],
            self._diagnostics["ignored_move_count"], duration_ms,
        )
        return self._build_script()

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
        if self._accepting_events:
            with self._lock:
                self._diagnostics["ignored_move_count"] += 1

    def _on_mouse_click(self, x, y, button, pressed):
        self._enqueue("mouse_down" if pressed else "mouse_up", {
            "x": int(x), "y": int(y), "button": _button_name(button),
        })

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
        elif raw.kind == "scroll":
            self._normalize_scroll(raw)

    def _normalize_key(self, raw):
        name, scan_code = _key_name(raw.payload["key"])
        if not name:
            return
        key_id = (name, scan_code)
        if name in HOTKEY_BLOCKLIST:
            with self._lock:
                self._diagnostics["ignored_hotkey_event_count"] += 1
            return
        is_down = raw.kind == "key_down"
        if is_down:
            if not self._is_target_foreground() or key_id in self._pressed_keys:
                return
            self._pressed_keys[key_id] = (name, scan_code)
            self._record_action({"type": "key", "event": "down", "key": name, "scan_code": scan_code}, raw.timestamp)
        elif key_id in self._pressed_keys:
            name, scan_code = self._pressed_keys.pop(key_id)
            self._record_action({"type": "key", "event": "up", "key": name, "scan_code": scan_code}, raw.timestamp)
        with self._lock:
            self._diagnostics["open_key_count"] = len(self._pressed_keys)

    def _normalize_mouse_button(self, raw):
        button = raw.payload["button"]
        point = self._to_target_ratio(raw.payload["x"], raw.payload["y"])
        if raw.kind == "mouse_down":
            if point is None or not self._is_target_foreground():
                return
            self._mouse_down[button] = (raw.timestamp, raw.payload["x"], raw.payload["y"], point)
            return
        pending = self._mouse_down.pop(button, None)
        if pending is None:
            return
        started, start_x, start_y, start_ratio = pending
        if point is None:
            return
        distance = ((raw.payload["x"] - start_x) ** 2 + (raw.payload["y"] - start_y) ** 2) ** 0.5
        duration_ms = max(0, int(round((raw.timestamp - started) * 1000)))
        if distance <= CLICK_DISTANCE_PX:
            action = {
                "type": "click", "button": button,
                "x_ratio": start_ratio[0], "y_ratio": start_ratio[1],
                "duration_ms": duration_ms,
            }
            self._record_action(action, started, completes_at=raw.timestamp)
            self._notify_click(action, start_x, start_y)
        else:
            self._record_action({
                "type": "drag", "button": button,
                "start_x_ratio": start_ratio[0], "start_y_ratio": start_ratio[1],
                "end_x_ratio": point[0], "end_y_ratio": point[1],
                "duration_ms": duration_ms,
            }, started, completes_at=raw.timestamp)
        with self._lock:
            self._diagnostics["open_mouse_button_count"] = len(self._mouse_down)

    def _normalize_scroll(self, raw):
        point = self._to_target_ratio(raw.payload["x"], raw.payload["y"])
        if point is None or not self._is_target_foreground():
            return
        if self._last_scroll is not None:
            last_action, last_time, last_point = self._last_scroll
            if raw.timestamp - last_time <= SCROLL_COALESCE_MS / 1000 and last_point == point:
                last_action["dx"] += raw.payload["dx"]
                last_action["dy"] += raw.payload["dy"]
                last_action["delta"] += raw.payload["dy"]
                self._last_scroll = (last_action, raw.timestamp, point)
                self._last_action_end = raw.timestamp
                return
        action = {
            "type": "scroll", "x_ratio": point[0], "y_ratio": point[1],
            "dx": raw.payload["dx"], "dy": raw.payload["dy"], "delta": raw.payload["dy"],
        }
        self._record_action(action, raw.timestamp)
        self._last_scroll = (action, raw.timestamp, point)

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

    def _to_target_ratio(self, screen_x, screen_y):
        if self._target_hwnd is None:
            return None
        try:
            import win32gui
            left, top, right, bottom = win32gui.GetClientRect(self._target_hwnd)
            client_x, client_y = win32gui.ScreenToClient(self._target_hwnd, (screen_x, screen_y))
            width, height = right - left, bottom - top
            if width <= 0 or height <= 0 or not (0 <= client_x < width and 0 <= client_y < height):
                return None
            return round(client_x / width, 4), round(client_y / height, 4)
        except Exception:
            return None

    def _is_target_foreground(self):
        try:
            is_foreground = bool(self._target_hwnd and ctypes.windll.user32.GetForegroundWindow() == self._target_hwnd)
            with self._lock:
                self._diagnostics["target_foreground"] = is_foreground
            return is_foreground
        except Exception:
            return False

    def _close_open_keys(self):
        now = time.monotonic()
        for name, scan_code in list(self._pressed_keys.values()):
            self._record_action({
                "type": "key", "event": "up", "key": name,
                "scan_code": scan_code, "synthetic": True,
            }, now)
        self._pressed_keys.clear()
        with self._lock:
            self._diagnostics["open_key_count"] = 0

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
