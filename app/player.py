import logging
import math
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum

import keyboard
import mouse

from app.input_safety import InputAuthorizationCode


class PlaybackStartStatus(str, Enum):
    STARTED = "STARTED"
    INPUT_BLOCKED = "INPUT_BLOCKED"
    FAILED = "FAILED"
    ALREADY_ACTIVE = "ALREADY_ACTIVE"


class PlaybackStatus(str, Enum):
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    INPUT_BLOCKED = "INPUT_BLOCKED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class PlaybackResult:
    status: PlaybackStatus
    reason: str | None = None
    source: str | None = None
    action_index: int | None = None
    finished_at_monotonic: float | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class PlaybackStartResult:
    status: PlaybackStartStatus
    reason: str | None = None


class ScriptPlayer:
    """Legacy global-input player with a final fail-closed authorization gate."""

    def __init__(self, window_tracker, input_safety_gate=None):
        self.window_tracker = window_tracker
        self.input_safety_gate = input_safety_gate
        self.logger = logging.getLogger("ScreenBot")
        self._thread = None; self._stop_event = threading.Event(); self._pause_event = threading.Event(); self._pause_event.set()
        self._input_lock = threading.RLock(); self._result_lock = threading.Lock()
        self._pressed_keys = set(); self._pressed_mouse_buttons = set()
        self._expected_target = None; self._last_full_auth = 0.0; self._last_result = None; self._last_authorization = None
        self._current_action_index = None; self._started_at_monotonic = None
        self.script = None; self.on_error = None; self.on_finished = None

    def get_last_result(self):
        with self._result_lock: return self._last_result

    def get_last_authorization(self):
        with self._result_lock:
            return self._last_authorization

    def start(self, script, expected_target=None):
        if self.is_active():
            return PlaybackStartResult(PlaybackStartStatus.ALREADY_ACTIVE, "playback_active")
        with self._result_lock:
            self._last_result = None
            self._last_authorization = None
        try:
            self.script = self._normalize_script(script)
        except Exception as exc:
            result = PlaybackResult(PlaybackStatus.FAILED, str(exc))
            self._set_terminal_result(result)
            return PlaybackStartResult(PlaybackStartStatus.FAILED, str(exc))
        self._expected_target = expected_target or (self.input_safety_gate.expected_current_session() if self.input_safety_gate else None)
        if self.input_safety_gate:
            auth = self.input_safety_gate.authorize_foreground_input(self._expected_target)
            if not auth.allowed:
                self._remember_authorization(auth)
                result = PlaybackResult(
                    PlaybackStatus.INPUT_BLOCKED,
                    auth.code.value,
                    source="player_start_gate",
                )
                self._set_terminal_result(result)
                return PlaybackStartResult(PlaybackStartStatus.INPUT_BLOCKED, auth.code.value)
        self._stop_event.clear(); self._pause_event.set(); self._pressed_keys.clear(); self._pressed_mouse_buttons.clear()
        self._started_at_monotonic = time.monotonic()
        self._current_action_index = None
        self._last_full_auth = self._started_at_monotonic
        self._thread = threading.Thread(target=self._run, daemon=True); self._thread.start()
        return PlaybackStartResult(PlaybackStartStatus.STARTED)

    def request_stop(self): self._stop_event.set(); self._pause_event.set()
    def stop(self):
        self.request_stop()
        if self._thread and self._thread is not threading.current_thread(): self._thread.join(timeout=5)
        self.release_all_inputs()
    def pause(self): self._pause_event.clear()
    def resume(self): self._pause_event.set()
    def is_active(self): return self._thread is not None and self._thread.is_alive()

    def _run(self):
        try:
            for index, action in enumerate(self.script.get("actions", [])):
                self._current_action_index = index
                if self._stop_event.is_set() or not self._sleep(action.get("delay", 0.0)): break
                if not self._execute_action(action): break
            if self.get_last_result() is None:
                self._set_terminal_result(
                    PlaybackResult(PlaybackStatus.CANCELLED if self._stop_event.is_set() else PlaybackStatus.COMPLETED)
                )
        except Exception as exc:
            self.logger.exception("Playback failed")
            reason = "coordinate_invalid" if str(exc) in {"coordinate_invalid", "invalid_client_size"} else str(exc)
            self._set_terminal_result(PlaybackResult(PlaybackStatus.FAILED, reason))
            if self.on_error: self.on_error(str(exc))
        finally:
            self.release_all_inputs()
            if self.on_finished: self.on_finished()

    def _authorize(self):
        if not self.input_safety_gate: return True
        if time.monotonic() - self._last_full_auth >= .25:
            result = self.input_safety_gate.authorize_foreground_input(self._expected_target); self._last_full_auth = time.monotonic()
        else: result = self.input_safety_gate.authorize_foreground_input_fast(self._expected_target)
        if result.allowed: return True
        self._remember_authorization(result)
        self._set_terminal_result(
            PlaybackResult(
                PlaybackStatus.INPUT_BLOCKED,
                result.code.value,
                source="player_per_emission",
                action_index=self._current_action_index,
            )
        )
        self.request_stop()
        return False

    def _sleep(self, seconds):
        end = time.monotonic() + max(0.0, float(seconds or 0))
        while time.monotonic() < end:
            if self._stop_event.is_set() or not self._authorize(): return False
            self._pause_event.wait(.05); time.sleep(min(.05, max(0, end-time.monotonic())))
        return not self._stop_event.is_set()

    def _emit(self, func):
        with self._input_lock:
            if self._stop_event.is_set() or not self._authorize(): return False
            func(); return True

    def _point(self, x, y):
        if not all(math.isfinite(float(v)) and 0 <= float(v) <= 1 for v in (x, y)):
            raise ValueError("coordinate_invalid")
        snapshot = self.input_safety_gate.target_session.get_snapshot() if self.input_safety_gate else None
        if snapshot is not None:
            w, h = snapshot.current_client_size; ox, oy = snapshot.client_screen_origin
            if w <= 0 or h <= 0:
                raise ValueError("coordinate_invalid")
            point = ox + round(float(x) * w), oy + round(float(y) * h)
            if not all(math.isfinite(value) for value in point):
                raise ValueError("coordinate_invalid")
            return point
        target = self.window_tracker.target
        if target is None: raise RuntimeError("target_missing")
        return target.from_client_ratio(float(x), float(y))

    def _execute_action(self, action):
        kind = action.get("type")
        if kind in {"mouse_click","mouse_double","mouse_wheel","mouse_move","mouse_down","mouse_up","scroll","drag"}: return self._mouse(action)
        if kind in {"key","key_down","key_up"}:
            action = {**action, "event": "down" if kind == "key_down" else "up" if kind == "key_up" else action.get("event")}; return self._key(action)
        return True

    def _mouse(self, a):
        kind=a["type"]
        if kind == "drag":
            sx,sy=self._point(a.get("start_x_ratio",.5),a.get("start_y_ratio",.5)); ex,ey=self._point(a.get("end_x_ratio",.5),a.get("end_y_ratio",.5)); button=a.get("button",mouse.LEFT)
            if not self._emit(lambda: mouse.move(sx,sy)) or not self._emit(lambda: mouse.press(button)): return False
            self._pressed_mouse_buttons.add(button); steps=max(1,min(30,int(max(0,float(a.get("duration_ms",0)))/20)))
            segment_delay = max(0.0, float(a.get("duration_ms", 0)) / 1000.0 / steps)
            for i in range(1,steps+1):
                if not self._emit(lambda i=i: mouse.move(round(sx+(ex-sx)*i/steps),round(sy+(ey-sy)*i/steps))): return False
                if not self._sleep(segment_delay): return False
            if not self._emit(lambda: mouse.release(button)): return False
            self._pressed_mouse_buttons.discard(button); return True
        x,y=self._point(a.get("ratio_x",.5),a.get("ratio_y",.5))
        if not self._emit(lambda: mouse.move(x,y)): return False
        if kind=="mouse_move": return True
        if kind=="mouse_down":
            b=a.get("button",mouse.LEFT); ok=self._emit(lambda: mouse.press(b)); self._pressed_mouse_buttons.add(b) if ok else None; return ok
        if kind=="mouse_up":
            b=a.get("button",mouse.LEFT); ok=self._emit(lambda: mouse.release(b)); self._pressed_mouse_buttons.discard(b) if ok else None; return ok
        if kind in {"scroll","mouse_wheel"}: return self._emit(lambda: mouse.wheel(a.get("dy",a.get("delta",0))))
        return self._emit(lambda: mouse.double_click(a.get("button",mouse.LEFT)) if kind=="mouse_double" else mouse.click(a.get("button",mouse.LEFT)))

    def _key(self,a):
        key=a.get("key") or a.get("scan_code")
        if not key:return True
        if a.get("event")=="down":
            ok=self._emit(lambda: keyboard.press(key)); self._pressed_keys.add(key) if ok else None; return ok
        ok=self._emit(lambda: keyboard.release(key)); self._pressed_keys.discard(key) if ok else None; return ok

    def _normalize_script(self, script):
        if not isinstance(script,dict): raise ValueError("Macro script must be an object")
        if isinstance(script.get("actions"),list): return script
        events=script.get("events")
        if not isinstance(events,list): raise ValueError("Macro script must contain actions or events")
        mapping={"click":"mouse_click","double_click":"mouse_double","scroll":"scroll"}; actions=[]
        for event in events:
            action=dict(event); kind=action.get("type"); action["delay"]=max(0,float(action.get("delay_ms",0))/1000)
            action["type"]=mapping.get(kind,kind)
            if kind=="mouse_move": action["ratio_x"]=action.pop("x_ratio",.5); action["ratio_y"]=action.pop("y_ratio",.5)
            if kind in {"key_down","key_up"}: action["type"]="key"; action["event"]="down" if kind=="key_down" else "up"
            if "x_ratio" in action: action.setdefault("ratio_x",action.pop("x_ratio"))
            if "y_ratio" in action: action.setdefault("ratio_y",action.pop("y_ratio"))
            actions.append(action)
        return {**script,"actions":actions}
    def _set_terminal_result(self, result):
        with self._result_lock:
            if self._last_result is None:
                finished_at = result.finished_at_monotonic or time.monotonic()
                duration_ms = result.duration_ms
                if duration_ms is None and self._started_at_monotonic is not None:
                    duration_ms = int(round((finished_at - self._started_at_monotonic) * 1000.0))
                result = replace(
                    result,
                    finished_at_monotonic=finished_at,
                    duration_ms=duration_ms,
                )
                self._last_result = result
                return True
            return False
    def _remember_authorization(self, result):
        with self._result_lock:
            self._last_authorization = result
    def release_all_inputs(self):
        with self._input_lock:
            for key in list(self._pressed_keys):
                try:
                    keyboard.release(key)
                except Exception:
                    self.logger.exception("Held keyboard cleanup failed")
            for button in list(self._pressed_mouse_buttons):
                try:
                    mouse.release(button)
                except Exception:
                    self.logger.exception("Held mouse cleanup failed")
            self._pressed_keys.clear(); self._pressed_mouse_buttons.clear()
