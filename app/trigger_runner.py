import logging
import threading
import time
from datetime import datetime

from app.text_trigger import TextTrigger


class TriggerRunner:
    def __init__(
        self,
        text_detector,
        script_store,
        player,
        trigger_data,
        logger=None,
        on_status=None,
        stop_on_error=False,
    ):
        self.text_detector = text_detector
        self.script_store = script_store
        self.player = player
        self.logger = logger or logging.getLogger("ScreenBot")
        self.trigger_data = self._validate_trigger_data(trigger_data)
        trigger_config = self.trigger_data["trigger"]
        self.text_trigger = TextTrigger(
            target_text=trigger_config.get("texts") or trigger_config["text"],
            event=trigger_config["event"],
            confirm_frames=trigger_config.get("confirm_frames", 1),
            cooldown_ms=trigger_config.get("cooldown_ms", 0),
            min_absent_duration_ms=trigger_config.get("min_absent_duration_ms", 5000),
            observation_config=trigger_config.get("observation"),
        )
        self._thread = None
        self._stop_event = threading.Event()
        self._poll_interval_ms = trigger_config.get("poll_interval_ms", 500)
        self._macro_running = False
        self._macro_script = None
        self._status_lock = threading.Lock()
        self._on_status = on_status
        self._stop_on_error = stop_on_error
        self._last_ocr_text = ""
        self._last_result = None
        self._last_error = None
        self._poll_count = 0
        self._trigger_fire_count = 0
        self._macro_start_count = 0
        self._last_ocr_success_time = None
        self._consecutive_success = 0
        self._consecutive_failure = 0
        self._macro_started_at = None
        self._last_macro_duration_ms = 0

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._macro_running = False
        self._last_error = None
        self._poll_count = 0
        self._trigger_fire_count = 0
        self._macro_start_count = 0
        self._last_ocr_success_time = None
        self._consecutive_success = 0
        self._consecutive_failure = 0
        self._macro_started_at = None
        self._last_macro_duration_ms = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._macro_running and self._macro_started_at is not None:
            self._last_macro_duration_ms = int(round((time.monotonic() - self._macro_started_at) * 1000.0))
        self._macro_started_at = None
        self._macro_running = False

    def is_active(self):
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        while not self._stop_event.is_set():
            try:
                if self.player.is_active():
                    self._macro_running = True
                    self._publish_status()
                    self._wait_interval()
                    continue
                if self._macro_running:
                    if self._macro_started_at is not None:
                        self._last_macro_duration_ms = int(round((time.monotonic() - self._macro_started_at) * 1000.0))
                    self._macro_started_at = None
                    self._macro_running = False

                observation = self.text_detector.observe_text(
                    self.trigger_data["trigger"]["region"],
                    self.text_trigger.target_texts,
                    self.trigger_data["trigger"].get("observation"),
                )
                ocr_text = observation.recognized_text
                result = self.text_trigger.update(observation)
                self.logger.info(
                    "[Observation] state=%s exact=%s similarity=%.2f readability=%.2f presence=%.2f reason=%s",
                    observation.state, observation.exact_match, observation.text_similarity,
                    observation.readability_score, observation.presence_score, observation.reason,
                )
                with self._status_lock:
                    self._last_ocr_text = ocr_text
                    self._last_result = result
                    self._poll_count += 1
                    self._last_ocr_success_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._consecutive_success += 1
                    self._consecutive_failure = 0
                if result.triggered:
                    macro_script = self._get_macro_script()
                    self.player.start(macro_script)
                    self._macro_running = True
                    self._macro_started_at = time.monotonic()
                    self._trigger_fire_count += 1
                    self._macro_start_count += 1
                    self.logger.info("Text trigger fired: %s -> %s", result.event_name, self.trigger_data["macro"])
                self._publish_status()
                self._wait_interval()
            except Exception as exc:
                with self._status_lock:
                    self._last_error = str(exc)
                    self._consecutive_failure += 1
                    self._consecutive_success = 0
                self.logger.exception("Trigger runner iteration failed")
                self._publish_status()
                if self._stop_on_error:
                    self._stop_event.set()
                    break
                self._wait_interval()

    def _wait_interval(self):
        self._stop_event.wait(self._poll_interval_ms / 1000.0)

    def _get_macro_script(self):
        if self._macro_script is None:
            raw_script = self.script_store.load_script(self.trigger_data["macro"])
            self._macro_script = self._normalize_macro_for_player(raw_script)
        return self._macro_script

    def get_status_snapshot(self):
        with self._status_lock:
            result = self._last_result
            trigger_status = self.text_trigger.get_status_snapshot()
            return {
                "active": self.is_active(),
                "macro_running": self._macro_running,
                "last_ocr_text": self._last_ocr_text,
                "last_error": self._last_error,
                "poll_count": self._poll_count,
                "trigger_fire_count": self._trigger_fire_count,
                "macro_start_count": self._macro_start_count,
                "last_ocr_success_time": self._last_ocr_success_time,
                "consecutive_success": self._consecutive_success,
                "consecutive_failure": self._consecutive_failure,
                "last_macro_duration_ms": self._last_macro_duration_ms,
                "trigger_result": {
                    "triggered": result.triggered if result else False,
                    "event_name": result.event_name if result else None,
                    "present": result.present if result else trigger_status.get("last_present"),
                },
                "trigger_status": trigger_status,
                "trigger_text": self.trigger_data["trigger"]["text"],
                "trigger_event": self.trigger_data["trigger"]["event"],
                "macro": self.trigger_data["macro"],
                "capture_diagnostics": self.text_detector.get_capture_runtime_diagnostics(),
            }

    def _publish_status(self):
        if callable(self._on_status):
            try:
                self._on_status(self.get_status_snapshot())
            except Exception:
                self.logger.exception("Trigger runner status callback failed")

    def _normalize_macro_for_player(self, script):
        if "actions" in script and isinstance(script.get("actions"), list):
            return script
        if "events" not in script or not isinstance(script.get("events"), list):
            raise ValueError("Macro script must contain actions or events")
        actions = []
        for event in script["events"]:
            event_type = event.get("type")
            delay = max(0.0, float(event.get("delay_ms", 0)) / 1000.0)
            if event_type == "click":
                actions.append(
                    {
                        "type": "mouse_click",
                        "button": event.get("button", "left"),
                        "ratio_x": event.get("x_ratio", 0.5),
                        "ratio_y": event.get("y_ratio", 0.5),
                        "delay": delay,
                    }
                )
            elif event_type == "double_click":
                actions.append(
                    {
                        "type": "mouse_double",
                        "button": event.get("button", "left"),
                        "ratio_x": event.get("x_ratio", 0.5),
                        "ratio_y": event.get("y_ratio", 0.5),
                        "delay": delay,
                    }
                )
            elif event_type == "key":
                key_name = event.get("key")
                key_event = event.get("event")
                if key_event in {"down", "up"}:
                    actions.append({"type": "key", "event": key_event, "key": key_name, "scan_code": event.get("scan_code"), "delay": delay})
                else:
                    actions.append({"type": "key", "event": "down", "key": key_name, "scan_code": event.get("scan_code"), "delay": delay})
                    actions.append({"type": "key", "event": "up", "key": key_name, "scan_code": event.get("scan_code"), "delay": 0.0})
            elif event_type in {"key_down", "key_up"}:
                action = dict(event)
                action["type"] = "key"
                action["event"] = "down" if event_type == "key_down" else "up"
                action["delay"] = delay
                actions.append(action)
            elif event_type in {"mouse_move", "mouse_down", "mouse_up", "scroll", "drag"}:
                action = dict(event)
                action["type"] = "mouse_wheel" if event_type == "scroll" else event_type
                action["delay"] = delay
                if "x_ratio" in action:
                    action["ratio_x"] = action.pop("x_ratio", 0.5)
                if "y_ratio" in action:
                    action["ratio_y"] = action.pop("y_ratio", 0.5)
                actions.append(action)
            else:
                raise ValueError(f"Unsupported macro event type: {event_type}")
        normalized = dict(script)
        normalized["actions"] = actions
        return normalized

    def _validate_trigger_data(self, trigger_data):
        if not isinstance(trigger_data, dict):
            raise ValueError("Trigger config must be a dict")
        if not isinstance(trigger_data.get("name"), str) or not trigger_data["name"].strip():
            raise ValueError("Trigger config missing name")
        trigger = trigger_data.get("trigger")
        if not isinstance(trigger, dict):
            raise ValueError("Trigger config missing trigger block")
        if trigger.get("type") != "text":
            raise ValueError("Only text triggers are supported")
        if trigger.get("event") not in {"appear", "disappear"}:
            raise ValueError("Trigger event must be 'appear' or 'disappear'")
        if not isinstance(trigger.get("text"), str) or not trigger["text"]:
            raise ValueError("Trigger text must be non-empty")
        region = trigger.get("region")
        if not isinstance(region, dict):
            raise ValueError("Trigger region must be a dict")
        for key in ("x_ratio", "y_ratio", "width_ratio", "height_ratio"):
            value = region.get(key)
            if not isinstance(value, (int, float)):
                raise ValueError(f"Trigger region missing numeric {key}")
        if trigger.get("poll_interval_ms", 500) <= 0:
            raise ValueError("poll_interval_ms must be > 0")
        if trigger.get("confirm_frames", 1) < 1:
            raise ValueError("confirm_frames must be >= 1")
        if trigger.get("cooldown_ms", 0) < 0:
            raise ValueError("cooldown_ms must be >= 0")
        if trigger.get("min_absent_duration_ms", 5000) < 0:
            raise ValueError("min_absent_duration_ms must be >= 0")
        if not isinstance(trigger_data.get("macro"), str) or not trigger_data["macro"].strip():
            raise ValueError("Trigger config missing macro filename")
        return trigger_data
