import json
import logging
import threading
import time
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

from app.text_trigger import TextTrigger
from app.trigger_runner import TriggerRunner


class WorkflowState(Enum):
    IDLE = auto()
    WAIT_TRIGGER = auto()
    RUNNING_MACRO = auto()
    NEXT_STEP = auto()
    FINISHED = auto()
    STOPPED = auto()
    ERROR = auto()


class WorkflowRunner:
    def __init__(self, text_detector, script_store, player, logger=None):
        self.text_detector = text_detector
        self.script_store = script_store
        self.player = player
        self.logger = logger or logging.getLogger("ScreenBot")

        self.state = WorkflowState.IDLE
        self.workflow = None
        self.current_step_index = -1
        self.current_step = None
        self._active_trigger_runner = None
        self._thread = None
        self._stop_event = threading.Event()
        self._status_lock = threading.Lock()

        self.last_error = None
        self.finish_reason = None
        self.stopped_by_user = False
        self._latest_trigger_status = None
        self._step_macro_started = False
        self._macro_started_monotonic = None
        self._last_macro_duration_ms = 0

        # Loop runtime state
        self.loop_mode = None
        self.max_cycles = None
        self.restart_step = None
        self._restart_step_index = 0
        self.completed_cycles = 0
        self.current_cycle = 1

        # Global stop trigger state (Phase B)
        self.stop_trigger_enabled = False
        self.stop_trigger_config = None
        self._stop_text_trigger = None
        self._stop_last_poll_monotonic = 0.0
        self._stop_poll_count = 0
        self._stop_last_ocr_text = ""
        self._stop_last_error = None
        self._stop_last_trigger_time = None
        self.pending_stop = False
        self._stop_trigger_matched = False

    def load_workflow(self, workflow_data):
        self.stop(manual=False)
        self.workflow = self._validate_workflow(workflow_data)
        self.current_step_index = -1
        self.current_step = None
        self.state = WorkflowState.IDLE
        self.last_error = None
        self.finish_reason = None
        self.stopped_by_user = False
        self._latest_trigger_status = None
        self._step_macro_started = False
        self._macro_started_monotonic = None
        self._last_macro_duration_ms = 0

        loop = self.workflow.get("loop")
        self.loop_mode = loop.get("mode") if loop else None
        self.max_cycles = loop.get("max_cycles") if loop else None
        self.restart_step = loop.get("restart_step") if loop else None
        self._restart_step_index = self._find_step_index(self.restart_step) if self.restart_step else 0
        self.completed_cycles = 0
        self.current_cycle = 1

        self.stop_trigger_enabled = bool(loop and loop.get("mode") == "stop_trigger")
        self.stop_trigger_config = loop.get("stop_trigger") if self.stop_trigger_enabled else None
        self._stop_text_trigger = None
        self._stop_last_poll_monotonic = 0.0
        self._stop_poll_count = 0
        self._stop_last_ocr_text = ""
        self._stop_last_error = None
        self._stop_last_trigger_time = None
        self.pending_stop = False
        self._stop_trigger_matched = False
        return self.workflow

    def load_workflow_file(self, file_path):
        path = Path(file_path)
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return self.load_workflow(data)

    def start(self):
        if self.workflow is None:
            raise RuntimeError("Workflow is not loaded")
        if self._thread is not None and self._thread.is_alive():
            return
        if self.player.is_active():
            raise RuntimeError("Cannot start workflow while player is active")

        self._stop_event.clear()
        self.stopped_by_user = False
        self.last_error = None
        self.finish_reason = None
        self.pending_stop = False
        self._stop_trigger_matched = False
        self.current_cycle = self.completed_cycles + 1
        if self.stop_trigger_enabled:
            cfg = self.stop_trigger_config
            self._stop_text_trigger = TextTrigger(
                target_text=cfg.get("texts") or cfg["text"],
                event=cfg["event"],
                confirm_frames=cfg.get("confirm_frames", 2),
                cooldown_ms=cfg.get("cooldown_ms", 0),
                min_absent_duration_ms=cfg.get("min_absent_duration_ms", 5000),
                observation_config=cfg.get("observation"),
            )
        else:
            self._stop_text_trigger = None

        self.current_step_index = self._restart_step_index - 1 if self.loop_mode else -1
        self.current_step = None
        self._macro_started_monotonic = None
        self._last_macro_duration_ms = 0

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, manual=True):
        self._stop_event.set()
        self.stopped_by_user = self.stopped_by_user or manual
        self._stop_active_trigger_runner()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self.player.is_active():
            self._complete_macro_timing()
            self.player.stop()
        if manual and self.state not in {WorkflowState.FINISHED, WorkflowState.ERROR}:
            self.state = WorkflowState.STOPPED
            self.finish_reason = "manual_stop"

    def is_active(self):
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        try:
            self.logger.info("CYCLE_STARTED cycle=%s step=%s", self.current_cycle, self._restart_step_label())
            self._move_to_next_step()
            while not self._stop_event.is_set():
                if self.state == WorkflowState.WAIT_TRIGGER:
                    # Global stop trigger has higher priority than step trigger.
                    if self._check_stop_trigger():
                        if self.state == WorkflowState.FINISHED:
                            break

                    trigger_status = self._active_trigger_runner.get_status_snapshot() if self._active_trigger_runner else {}
                    if trigger_status.get("macro_start_count", 0) > 0:
                        self._step_macro_started = True

                    if self.player.is_active():
                        if self._macro_started_monotonic is None:
                            self._macro_started_monotonic = time.monotonic()
                        self._stop_active_trigger_runner()
                        self.state = WorkflowState.RUNNING_MACRO
                    elif self._step_macro_started:
                        self._last_macro_duration_ms = int(trigger_status.get("last_macro_duration_ms", 0) or 0)
                        self.logger.info("Workflow step %s macro finished (fast completion)", self.current_step.get("id"))
                        self._stop_active_trigger_runner()
                        self.state = WorkflowState.NEXT_STEP
                    elif self._active_trigger_runner is not None and not self._active_trigger_runner.is_active():
                        err = trigger_status.get("last_error") or "Trigger runner stopped unexpectedly"
                        self._set_runtime_error(err)
                        break
                    else:
                        time.sleep(0.05)

                elif self.state == WorkflowState.RUNNING_MACRO:
                    # The global stop condition remains observable while a macro is
                    # running.  A match is deliberately deferred until the macro
                    # reaches its normal completion boundary; it must never cancel
                    # the player's current input event.
                    self._check_stop_trigger(allow_during_macro=True)
                    if self.player.is_active():
                        time.sleep(0.05)
                    else:
                        self._complete_macro_timing()
                        self.logger.info("Workflow step %s macro finished", self.current_step.get("id"))
                        self._stop_active_trigger_runner()
                        if self.pending_stop:
                            # A stop match observed during the macro takes effect at
                            # the first safe boundary: after this macro, before any
                            # following step can begin.
                            self._finish(WorkflowState.FINISHED, "stop_trigger_matched")
                        else:
                            self.state = WorkflowState.NEXT_STEP

                elif self.state == WorkflowState.NEXT_STEP:
                    self._move_to_next_step()

                elif self.state in {WorkflowState.FINISHED, WorkflowState.STOPPED, WorkflowState.ERROR}:
                    break
                else:
                    time.sleep(0.05)

        except Exception as exc:
            self._set_runtime_error(str(exc))
            self.logger.exception("Workflow runner failed")
        finally:
            self._stop_active_trigger_runner()
            # A WGC session is a workflow-lifetime resource.  Releasing it at
            # the boundary prevents a stopped workflow from continuing to
            # subscribe to game frames or retain GPU/COM resources.
            try:
                self.text_detector.invalidate_capture_target()
                self.logger.info("CAPTURE_RUNTIME_SESSION_RELEASED reason=workflow_end")
            except Exception:
                self.logger.exception("CAPTURE_RUNTIME_SESSION_RELEASE_FAILED")
            if self._stop_event.is_set() and self.state not in {WorkflowState.FINISHED, WorkflowState.ERROR}:
                self.state = WorkflowState.STOPPED
                if self.finish_reason is None:
                    self.finish_reason = "manual_stop" if self.stopped_by_user else "workflow_stopped"

    def _move_to_next_step(self):
        self.current_step_index += 1
        steps = self.workflow["steps"]
        if self.current_step_index >= len(steps):
            self._on_cycle_boundary()
            return

        self.current_step = steps[self.current_step_index]
        self._step_macro_started = False
        trigger_data = self._build_trigger_data(self.current_step)
        self._active_trigger_runner = TriggerRunner(
            self.text_detector,
            self.script_store,
            self.player,
            trigger_data,
            logger=self.logger,
            on_status=self._on_trigger_status,
            stop_on_error=True,
        )
        self._active_trigger_runner.start()
        self.state = WorkflowState.WAIT_TRIGGER
        self.logger.info("Workflow step active: %s", self.current_step.get("id"))

    def _on_cycle_boundary(self):
        # A cycle is completed only when the last step macro has completed and we reach here.
        self.completed_cycles += 1
        self.logger.info("CYCLE_COMPLETED cycle=%s", self.completed_cycles)

        if self.pending_stop:
            self._finish(WorkflowState.FINISHED, "stop_trigger_matched")
            return

        if not self.loop_mode:
            self._finish(WorkflowState.FINISHED, "workflow_completed")
            return

        if self.loop_mode == "max_cycles":
            if self.completed_cycles >= self.max_cycles:
                self.logger.info("MAX_CYCLES_REACHED completed=%s max=%s", self.completed_cycles, self.max_cycles)
                self._finish(WorkflowState.FINISHED, "max_cycles_reached")
                return
            self._restart_cycle()
            return

        if self.loop_mode in {"manual_stop", "stop_trigger"}:
            if self.loop_mode == "stop_trigger":
                # Safe stop strategy: check once at cycle boundary before restarting.
                self._check_stop_trigger(force=True)
                if self.pending_stop or self._stop_trigger_matched:
                    self._finish(WorkflowState.FINISHED, "stop_trigger_matched")
                    return
            self._restart_cycle()
            return

        self._finish(WorkflowState.FINISHED, "workflow_completed")

    def _restart_cycle(self):
        self.current_cycle = self.completed_cycles + 1
        self.current_step_index = self._restart_step_index - 1
        self.current_step = None
        self._step_macro_started = False
        self.state = WorkflowState.NEXT_STEP
        self.logger.info("CYCLE_RESTARTED next_cycle=%s step=%s", self.current_cycle, self._restart_step_label())

    def _check_stop_trigger(self, force=False, allow_during_macro=False):
        if not self.stop_trigger_enabled or self._stop_text_trigger is None:
            return False
        allowed_states = {WorkflowState.WAIT_TRIGGER, WorkflowState.NEXT_STEP}
        if allow_during_macro:
            allowed_states.add(WorkflowState.RUNNING_MACRO)
        if self.state not in allowed_states and not force:
            return False

        now = time.monotonic()
        poll_interval_ms = self.stop_trigger_config.get("poll_interval_ms", 500)
        if not force and (now - self._stop_last_poll_monotonic) * 1000.0 < poll_interval_ms:
            return False
        self._stop_last_poll_monotonic = now

        try:
            observation = self.text_detector.observe_text(
                self.stop_trigger_config["region"],
                self._stop_text_trigger.target_texts,
                self.stop_trigger_config.get("observation"),
            )
            self._stop_last_ocr_text = observation.recognized_text
            self._stop_poll_count += 1
            result = self._stop_text_trigger.update(observation)
            self.logger.info(
                "[Observation] source=global_stop state=%s exact=%s similarity=%.2f readability=%.2f presence=%.2f reason=%s",
                observation.state, observation.exact_match, observation.text_similarity,
                observation.readability_score, observation.presence_score, observation.reason,
            )
            if result.triggered:
                self.pending_stop = True
                self._stop_trigger_matched = True
                self._stop_last_trigger_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.logger.info("STOP_TRIGGER_MATCHED event=%s", result.event_name)
                if self.state == WorkflowState.WAIT_TRIGGER and not self.player.is_active():
                    self._stop_active_trigger_runner()
                    self._finish(WorkflowState.FINISHED, "stop_trigger_matched")
                    return True
        except Exception as exc:
            self._stop_last_error = str(exc)
            self._set_runtime_error(str(exc))
            return False
        return self._stop_trigger_matched

    def _finish(self, state, reason):
        self.state = state
        self.finish_reason = reason
        if state == WorkflowState.FINISHED:
            self.logger.info("Workflow finished: %s (reason=%s)", self.workflow["name"], reason)
        elif state == WorkflowState.STOPPED:
            self.logger.info("Workflow stopped: %s (reason=%s)", self.workflow["name"], reason)

    def _set_runtime_error(self, message):
        self.last_error = message
        if self._is_target_window_lost_error(message):
            self.finish_reason = "target_window_lost"
        else:
            self.finish_reason = "runtime_error"
        self.state = WorkflowState.ERROR
        self.logger.error("Workflow runtime error: %s", message)

    def _is_target_window_lost_error(self, message):
        text = (message or "").lower()
        return (
            "目標視窗" in message
            or "target window" in text
            or "window is closed" in text
            or "window is minimized" in text
            or "已關閉" in message
            or "已最小化" in message
        )

    def _restart_step_label(self):
        return self.restart_step or (self.workflow["steps"][0]["id"] if self.workflow and self.workflow.get("steps") else "(none)")

    def _find_step_index(self, step_id):
        for idx, step in enumerate(self.workflow["steps"]):
            if step.get("id") == step_id:
                return idx
        raise ValueError(f"Loop restart step does not exist: {step_id}")

    def _stop_active_trigger_runner(self):
        if self._active_trigger_runner is not None:
            runner = self._active_trigger_runner
            runner.stop()
            # Preserve the completed macro duration/status for the terminal
            # workflow snapshot rather than leaving a stale macro_running=True
            # status in diagnostics.
            self._on_trigger_status(runner.get_status_snapshot())
            self._active_trigger_runner = None

    def _complete_macro_timing(self):
        if self._macro_started_monotonic is not None:
            self._last_macro_duration_ms = int(round((time.monotonic() - self._macro_started_monotonic) * 1000.0))
            self._macro_started_monotonic = None

    def _build_trigger_data(self, step):
        trigger = dict(step["trigger"])
        trigger.setdefault("type", "text")
        trigger.setdefault("region", {"x_ratio": 0.0, "y_ratio": 0.0, "width_ratio": 1.0, "height_ratio": 1.0})
        trigger.setdefault("poll_interval_ms", 500)
        trigger.setdefault("confirm_frames", 2)
        trigger.setdefault("cooldown_ms", 1000)
        trigger.setdefault("min_absent_duration_ms", 5000)
        return {
            "name": f"{self.workflow['name']}::{step['id']}",
            "trigger": trigger,
            "macro": step["macro"],
        }

    def _on_trigger_status(self, status):
        with self._status_lock:
            self._latest_trigger_status = status

    def _stop_trigger_snapshot(self):
        trigger_status = self._stop_text_trigger.get_status_snapshot() if self._stop_text_trigger else {}
        return {
            "enabled": self.stop_trigger_enabled,
            "trigger_text": self.stop_trigger_config.get("text") if self.stop_trigger_config else None,
            "trigger_event": self.stop_trigger_config.get("event") if self.stop_trigger_config else None,
            "trigger_type": self.stop_trigger_config.get("type") if self.stop_trigger_config else None,
            "poll_count": self._stop_poll_count,
            "last_ocr_text": self._stop_last_ocr_text,
            "last_error": self._stop_last_error,
            "pending_stop": self.pending_stop,
            "matched": self._stop_trigger_matched,
            "matched_time": self._stop_last_trigger_time,
            "confirm_progress": (
                f"{trigger_status.get('candidate_count', 0)} / {trigger_status.get('confirm_frames', 0)}"
                if trigger_status
                else "N/A"
            ),
            "cooldown_remaining_ms": trigger_status.get("cooldown_remaining_ms") if trigger_status else None,
            "status": trigger_status,
        }

    def get_runtime_snapshot(self):
        with self._status_lock:
            trigger_status = dict(self._latest_trigger_status or {})
        macro_running = self.state == WorkflowState.RUNNING_MACRO and self.player.is_active()
        trigger_status["macro_running"] = macro_running
        if macro_running and self._macro_started_monotonic is not None:
            trigger_status["last_macro_duration_ms"] = int(round((time.monotonic() - self._macro_started_monotonic) * 1000.0))
        else:
            trigger_status["last_macro_duration_ms"] = self._last_macro_duration_ms or trigger_status.get("last_macro_duration_ms", 0)
        total_steps = len(self.workflow["steps"]) if self.workflow else 0
        step_number = self.current_step_index + 1 if self.current_step is not None else 0
        step = self.current_step or {}
        trigger = step.get("trigger", {})
        loop_mode = self.loop_mode
        return {
            "workflow_name": self.workflow.get("name") if self.workflow else None,
            "state": self.state.name,
            "stopped_by_user": self.stopped_by_user,
            "error": self.last_error,
            "finish_reason": self.finish_reason,
            "step_number": step_number,
            "total_steps": total_steps,
            "step_id": step.get("id"),
            "trigger_text": trigger.get("text"),
            "trigger_event": trigger.get("event"),
            "macro": step.get("macro"),
            "trigger_runtime": trigger_status,
            "loop_mode": loop_mode,
            "restart_step": self.restart_step,
            "max_cycles": self.max_cycles,
            "current_cycle": self.current_cycle,
            "completed_cycles": self.completed_cycles,
            "pending_stop": self.pending_stop,
            "stop_trigger_runtime": self._stop_trigger_snapshot(),
            "capture_runtime": self.text_detector.get_capture_runtime_diagnostics(),
        }

    def _validate_workflow(self, workflow_data):
        if not isinstance(workflow_data, dict):
            raise ValueError("Workflow must be a dict")
        name = workflow_data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Workflow missing name")
        steps = workflow_data.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("Workflow must contain at least one step")

        seen_ids = set()
        normalized_steps = []
        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"Workflow step {idx} must be an object")
            step_id = step.get("id")
            if not isinstance(step_id, str) or not step_id.strip():
                raise ValueError(f"Workflow step {idx} missing id")
            if step_id in seen_ids:
                raise ValueError(f"Duplicate workflow step id: {step_id}")
            seen_ids.add(step_id)

            trigger = step.get("trigger")
            if not isinstance(trigger, dict):
                raise ValueError(f"Workflow step {step_id} missing trigger")
            if trigger.get("type", "text") != "text":
                raise ValueError(f"Workflow step {step_id} supports only text trigger")
            if trigger.get("event") not in {"appear", "disappear"}:
                raise ValueError(f"Workflow step {step_id} event must be appear/disappear")
            if not isinstance(trigger.get("text"), str) or not trigger["text"]:
                raise ValueError(f"Workflow step {step_id} requires non-empty trigger text")
            region = trigger.get("region")
            if not isinstance(region, dict):
                raise ValueError(f"Workflow step {step_id} missing trigger region")
            for k in ("x_ratio", "y_ratio", "width_ratio", "height_ratio"):
                if not isinstance(region.get(k), (int, float)):
                    raise ValueError(f"Workflow step {step_id} region {k} must be numeric")

            macro = step.get("macro")
            if not isinstance(macro, str) or not macro.strip():
                raise ValueError(f"Workflow step {step_id} missing macro filename")

            trigger_norm = dict(trigger)
            min_absent_duration_ms = trigger_norm.get("min_absent_duration_ms", 5000)
            if not isinstance(min_absent_duration_ms, int) or min_absent_duration_ms < 0:
                raise ValueError(f"Workflow step {step_id} min_absent_duration_ms must be integer >= 0")
            trigger_norm["min_absent_duration_ms"] = min_absent_duration_ms
            normalized_steps.append({"id": step_id, "trigger": trigger_norm, "macro": macro})

        normalized = {"name": name, "steps": normalized_steps}
        loop = workflow_data.get("loop")
        if loop is None:
            return normalized
        if not isinstance(loop, dict):
            raise ValueError("loop must be an object")

        mode = loop.get("mode")
        if mode not in {"manual_stop", "max_cycles", "stop_trigger"}:
            raise ValueError(f"Unsupported loop mode: {mode}")

        restart_step = loop.get("restart_step")
        if not isinstance(restart_step, str) or not restart_step.strip():
            raise ValueError("Loop restart_step is required")
        if restart_step not in seen_ids:
            raise ValueError(f"Loop restart step does not exist: {restart_step}")

        loop_norm = {"mode": mode, "restart_step": restart_step}
        if mode == "max_cycles":
            max_cycles = loop.get("max_cycles")
            if not isinstance(max_cycles, int) or max_cycles < 1:
                raise ValueError("loop.max_cycles must be an integer >= 1")
            loop_norm["max_cycles"] = max_cycles

        if mode == "stop_trigger":
            stop_trigger = loop.get("stop_trigger")
            if not isinstance(stop_trigger, dict):
                raise ValueError("loop.stop_trigger is required for stop_trigger mode")
            if stop_trigger.get("type", "text") != "text":
                raise ValueError("loop.stop_trigger supports only text type")
            if stop_trigger.get("event") not in {"appear", "disappear"}:
                raise ValueError("loop.stop_trigger.event must be appear/disappear")
            if not isinstance(stop_trigger.get("text"), str) or not stop_trigger.get("text"):
                raise ValueError("loop.stop_trigger.text must be non-empty")
            region = stop_trigger.get("region")
            if not isinstance(region, dict):
                raise ValueError("loop.stop_trigger.region must be an object")
            for key in ("x_ratio", "y_ratio", "width_ratio", "height_ratio"):
                value = region.get(key)
                if not isinstance(value, (int, float)):
                    raise ValueError(f"loop.stop_trigger.region.{key} must be numeric")
            poll_interval = stop_trigger.get("poll_interval_ms", 500)
            confirm_frames = stop_trigger.get("confirm_frames", 2)
            cooldown_ms = stop_trigger.get("cooldown_ms", 0)
            min_absent_duration_ms = stop_trigger.get("min_absent_duration_ms", 5000)
            if not isinstance(poll_interval, int) or poll_interval <= 0:
                raise ValueError("loop.stop_trigger.poll_interval_ms must be integer > 0")
            if not isinstance(confirm_frames, int) or confirm_frames < 1:
                raise ValueError("loop.stop_trigger.confirm_frames must be integer >= 1")
            if not isinstance(cooldown_ms, int) or cooldown_ms < 0:
                raise ValueError("loop.stop_trigger.cooldown_ms must be integer >= 0")
            if not isinstance(min_absent_duration_ms, int) or min_absent_duration_ms < 0:
                raise ValueError("loop.stop_trigger.min_absent_duration_ms must be integer >= 0")
            loop_norm["stop_trigger"] = {
                "type": "text",
                "event": stop_trigger["event"],
                "text": stop_trigger["text"],
                "region": dict(region),
                "poll_interval_ms": poll_interval,
                "confirm_frames": confirm_frames,
                "cooldown_ms": cooldown_ms,
                "min_absent_duration_ms": min_absent_duration_ms,
                "observation": dict(stop_trigger.get("observation") or {}),
            }

        normalized["loop"] = loop_norm
        return normalized
