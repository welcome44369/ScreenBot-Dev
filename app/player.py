import time
import threading
import logging
import mouse
import keyboard


class ScriptPlayer:
    def __init__(self, window_tracker):
        self.window_tracker = window_tracker
        self.logger = logging.getLogger("ScreenBot")
        self._thread = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._pressed_keys = set()
        self._pressed_mouse_buttons = set()
        self.script = None
        self.on_error = None
        self.on_finished = None

    def start(self, script):
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Playback 已在進行中")
        self.script = self._normalize_script(script)
        self._stop_event.clear()
        self._pause_event.set()
        self._pressed_keys.clear()
        self._pressed_mouse_buttons.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.logger.info("腳本開始播放")

    def stop(self):
        self._stop_event.set()
        self._pause_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._release_all_keys()
        self.release_all_inputs()
        self.logger.info("腳本已停止")

    def pause(self):
        self._pause_event.clear()
        self.logger.info("腳本暫停")

    def resume(self):
        self._pause_event.set()
        self.logger.info("腳本繼續")

    def is_active(self):
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        try:
            target = self.window_tracker.refresh()
            for action in self.script.get("actions", []):
                if self._stop_event.is_set():
                    break
                self._pause_event.wait()
                if not self._sleep(action.get("delay", 0.0)):
                    break
                self._pause_event.wait()
                if self._stop_event.is_set():
                    break
                if not self._check_target():
                    return
                self._execute_action(action)
            self.logger.info("腳本播放完成")
        except Exception as exc:
            self.logger.exception("腳本播放發生例外")
            if self.on_error:
                self.on_error(str(exc))
        finally:
            self.release_all_inputs()
            if self.on_finished:
                self.on_finished()

    def _sleep(self, seconds):
        elapsed = 0.0
        interval = 0.05
        while elapsed < seconds:
            if self._stop_event.is_set():
                return False
            self._pause_event.wait()
            sleep_time = min(interval, seconds - elapsed)
            time.sleep(sleep_time)
            elapsed += sleep_time
        return True

    def _check_target(self):
        try:
            self.window_tracker.refresh()
            return True
        except Exception as exc:
            self.logger.warning(f"目標視窗失效: {exc}")
            if self.on_error:
                self.on_error(str(exc))
            return False

    def _execute_action(self, action):
        action_type = action.get("type")
        if action_type in {"mouse_click", "mouse_double", "mouse_wheel", "mouse_move", "mouse_down", "mouse_up", "scroll", "drag"}:
            self._execute_mouse_action(action)
        elif action_type in {"key", "key_down", "key_up"}:
            if action_type == "key_down":
                action = {**action, "event": "down"}
            elif action_type == "key_up":
                action = {**action, "event": "up"}
            self._execute_key_action(action)
        else:
            self.logger.debug(f"跳過未知動作: {action_type}")

    def _execute_mouse_action(self, action):
        target = self.window_tracker.target
        if target is None:
            raise RuntimeError("沒有目標視窗")
        x, y = target.from_client_ratio(action.get("ratio_x", 0.5), action.get("ratio_y", 0.5))
        if action["type"] == "drag":
            start_x, start_y = target.from_client_ratio(action.get("start_x_ratio", 0.5), action.get("start_y_ratio", 0.5))
            end_x, end_y = target.from_client_ratio(action.get("end_x_ratio", 0.5), action.get("end_y_ratio", 0.5))
            button = action.get("button", mouse.LEFT)
            mouse.move(start_x, start_y)
            mouse.press(button)
            self._pressed_mouse_buttons.add(button)
            duration_s = max(0.0, float(action.get("duration_ms", 0)) / 1000.0)
            steps = max(1, min(30, int(duration_s / 0.02)))
            for index in range(1, steps + 1):
                if self._stop_event.is_set():
                    break
                ratio = index / steps
                mouse.move(round(start_x + (end_x - start_x) * ratio), round(start_y + (end_y - start_y) * ratio))
                if duration_s:
                    time.sleep(duration_s / steps)
            mouse.release(button)
            self._pressed_mouse_buttons.discard(button)
            return
        mouse.move(x, y)
        if action["type"] == "mouse_move":
            return
        if action["type"] == "mouse_down":
            button = action.get("button", mouse.LEFT); mouse.press(button); self._pressed_mouse_buttons.add(button); return
        if action["type"] == "mouse_up":
            button = action.get("button", mouse.LEFT); mouse.release(button); self._pressed_mouse_buttons.discard(button); return
        if action["type"] == "scroll":
            mouse.wheel(action.get("dy", action.get("delta", 0))); return
        if action["type"] == "mouse_click":
            mouse.click(action.get("button", mouse.LEFT))
            self.logger.debug(f"播放滑鼠點擊 {action.get('button')} at {x},{y}")
        elif action["type"] == "mouse_double":
            mouse.double_click(action.get("button", mouse.LEFT))
            self.logger.debug(f"播放滑鼠雙擊 {action.get('button')} at {x},{y}")
        elif action["type"] == "mouse_wheel":
            mouse.wheel(action.get("dy", action.get("delta", 0)))
            self.logger.debug(f"播放滾輪 {action.get('delta')} at {x},{y}")

    def _execute_key_action(self, action):
        key = action.get("key")
        if key in {"ctrl_l", "ctrl_r", "shift_l", "shift_r", "alt_l", "alt_r"}:
            key = {
                "ctrl_l": "left ctrl", "ctrl_r": "right ctrl",
                "shift_l": "left shift", "shift_r": "right shift",
                "alt_l": "left alt", "alt_r": "right alt",
            }[key]
        if isinstance(key, str) and key.startswith("vk_"):
            key = action.get("scan_code")
        key = key if key is not None else action.get("scan_code")
        if not key:
            return
        if action.get("event") == "down":
            keyboard.press(key)
            self._pressed_keys.add(key)
            self.logger.debug(f"播放鍵盤按下 {key}")
        elif action.get("event") == "up":
            keyboard.release(key)
            self._pressed_keys.discard(key)
            self.logger.debug(f"播放鍵盤放開 {key}")

    def _normalize_script(self, script):
        if not isinstance(script, dict):
            raise ValueError("Macro script must be an object")
        if isinstance(script.get("actions"), list):
            return script
        events = script.get("events")
        if not isinstance(events, list):
            raise ValueError("Macro script must contain actions or events")
        actions = []
        for event in events:
            kind = event.get("type")
            action = dict(event)
            action["delay"] = max(0.0, float(event.get("delay_ms", 0)) / 1000.0)
            mapping = {"click": "mouse_click", "double_click": "mouse_double", "scroll": "scroll"}
            action["type"] = mapping.get(kind, kind)
            if kind == "mouse_move":
                action["ratio_x"] = action.pop("x_ratio", 0.5); action["ratio_y"] = action.pop("y_ratio", 0.5)
            elif kind in {"key_down", "key_up"}:
                action["type"] = "key"
                action["event"] = "down" if kind == "key_down" else "up"
            elif kind not in {"click", "double_click", "scroll", "mouse_down", "mouse_up", "key", "mouse_move", "drag"}:
                raise ValueError(f"Unsupported macro event type: {kind}")
            if "x_ratio" in action and "ratio_x" not in action: action["ratio_x"] = action.pop("x_ratio")
            if "y_ratio" in action and "ratio_y" not in action: action["ratio_y"] = action.pop("y_ratio")
            actions.append(action)
        normalized = dict(script); normalized["actions"] = actions
        return normalized

    def release_all_inputs(self):
        """Best-effort, idempotent release for every held keyboard/mouse input."""
        self._release_all_keys()
        for button in list(self._pressed_mouse_buttons):
            try:
                mouse.release(button)
            except Exception:
                self.logger.exception("Failed to release mouse button: %s", button)
        self._pressed_mouse_buttons.clear()

    def _release_all_keys(self):
        for key in list(self._pressed_keys):
            try:
                keyboard.release(key)
            except Exception:
                pass
        self._pressed_keys.clear()
