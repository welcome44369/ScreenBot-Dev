import json
import logging
import os
import threading
import time
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

from app.text_trigger import TextTrigger
from app.trigger_runner import TriggerRunner
from app.trigger_conditions import label_for_code, normalize_condition, ui_code_from_trigger
from app.player import PlaybackStatus


class WorkflowState(Enum):
    IDLE = auto()
    WAIT_TRIGGER = auto()
    RUNNING_MACRO = auto()
    NEXT_STEP = auto()
    STOPPING = auto()
    FINISHED = auto()
    STOPPED = auto()
    ERROR = auto()


class WorkflowRunner:
    def __init__(
        self,
        text_detector,
        script_store,
        player,
        logger=None,
        input_safety_gate=None,
        workflow_process_diagnostics=None,
    ):
        self.text_detector = text_detector
        self.script_store = script_store
        self.player = player
        self.input_safety_gate = input_safety_gate
        self.workflow_process_diagnostics = workflow_process_diagnostics
        self._expected_target_session = None
        self.logger = logger or logging.getLogger("ScreenBot")

        self.state = WorkflowState.IDLE
        self.workflow = None
        self.current_step_index = -1
        self.current_step = None
        self._active_trigger_runner = None
        self._trigger_runner_generation = 0
        self._thread = None
        self._stop_event = threading.Event()
        self._status_lock = threading.Lock()
        self._stop_lock = threading.Lock()

        self.last_error = None
        self.finish_reason = None
        self.stopped_by_user = False
        self._latest_trigger_status = None
        self._step_macro_started = False
        self._macro_started_monotonic = None
        self._last_macro_duration_ms = 0
        self._trigger_runner_generation = 0
        self.last_step_start_error = None

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
        self.terminal_claimed = False
        self.new_work_blocked = False
        self.stop_details = None
        self.stop_source = None
        self.stop_trigger_id = None
        self.stop_trigger_name = None
        self._stop_trigger_matched = False
        self._stop_macro_cancel_requested = False
        self._condition_memory = {}

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
        self.last_step_start_error = None

        loop = self.workflow.get("loop")
        self.loop_mode = loop.get("mode") if loop else None
        self.max_cycles = loop.get("max_cycles") if loop else None
        self.restart_step = loop.get("restart_step") if loop else None
        self._restart_step_index = self._find_step_index(self.restart_step) if self.restart_step else 0
        self.completed_cycles = 0
        self.current_cycle = 1

        # Global stop is independent from the normal loop policy.  The legacy
        # ``stop_trigger`` mode remains readable but is not an enablement gate.
        self.stop_trigger_config = loop.get("stop_trigger") if loop else None
        self.stop_trigger_enabled = bool(self.stop_trigger_config)
        self._stop_text_trigger = None
        self._stop_last_poll_monotonic = 0.0
        self._stop_poll_count = 0
        self._stop_last_ocr_text = ""
        self._stop_last_error = None
        self._stop_last_trigger_time = None
        self.pending_stop = False
        self.stop_source = None
        self.stop_trigger_id = (self.stop_trigger_config or {}).get("id")
        self.stop_trigger_name = (self.stop_trigger_config or {}).get("name")
        self._stop_trigger_matched = False
        self._stop_macro_cancel_requested = False
        self._condition_memory.clear()
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
        self._expected_target_session = (self.input_safety_gate.expected_current_session() if self.input_safety_gate else None)
        if self.input_safety_gate and self._expected_target_session is None:
            raise RuntimeError("No target session is available")

        self._stop_event.clear()
        self.stopped_by_user = False
        self.last_error = None
        self.finish_reason = None
        self.pending_stop = False
        self.terminal_claimed = False
        self.new_work_blocked = False
        self.stop_details = None
        self.stop_source = None
        self._stop_trigger_matched = False
        self._stop_macro_cancel_requested = False
        self._stop_last_trigger_time = None
        self._stop_last_error = None
        self._stop_last_poll_monotonic = 0.0
        self._stop_poll_count = 0
        self._stop_last_ocr_text = ""
        # Every explicit Start begins a new condition session; cycle restarts
        # deliberately share this dictionary.
        self._condition_memory.clear()
        self.current_cycle = self.completed_cycles + 1
        if self.stop_trigger_enabled:
            cfg = self.stop_trigger_config
            self._stop_text_trigger = TextTrigger(
                target_text=cfg.get("texts") or cfg["text"],
                event=cfg.get("event"),
                condition=cfg.get("condition"),
                condition_memory=self._condition_memory_for(
                    "stop",
                    cfg.get("id") or cfg.get("name") or "inline_stop_trigger",
                    cfg,
                ),
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
        self.last_step_start_error = None

        self._thread = threading.Thread(target=self._run, daemon=True)
        if self.workflow_process_diagnostics is not None:
            self.workflow_process_diagnostics.stage(
                "WORKFLOW_RUNNER_THREAD_STARTING",
                cycle=self.current_cycle,
            )
        self._thread.start()

    def request_immediate_stop(self, source, details=None):
        """Claim one terminal stop and request non-blocking cancellation.

        This method is safe from the workflow worker and Qt UI thread: it
        never joins a thread, waits for cleanup, or accesses a widget.
        """
        with self._stop_lock:
            if self.terminal_claimed or self.state in {WorkflowState.FINISHED, WorkflowState.STOPPED, WorkflowState.ERROR}:
                return False
            self.terminal_claimed = True
            self.pending_stop = True
            self.new_work_blocked = True
            self.stop_source = source
            self.stop_details = dict(details or {})
            self.finish_reason = source
            if source == "manual_stop":
                self.stopped_by_user = True
            self.state = WorkflowState.STOPPING

        self._stop_event.set()
        runner = self._active_trigger_runner
        if runner is not None:
            request_stop = getattr(runner, "request_stop", None)
            if callable(request_stop):
                request_stop()
            else:
                runner.stop()
        if self.player.is_active():
            self._stop_macro_cancel_requested = True
            request_stop = getattr(self.player, "request_stop", None)
            if callable(request_stop):
                request_stop()
            else:
                self.player.stop()
        return True

    def stop(self, manual=True):
        """Bounded UI-facing wait wrapper around ``request_immediate_stop``."""
        self.request_immediate_stop("manual_stop" if manual else "workflow_stop")
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self.player.is_active():
            self._complete_macro_timing()
            self.player.stop()
        if manual and self.state not in {WorkflowState.FINISHED, WorkflowState.ERROR}:
            self.state = WorkflowState.STOPPED
            if self.stop_source in {"manual_stop", "stop_trigger"}:
                self.finish_reason = self.stop_source
        # Run-scoped condition/recovery state is never persisted and must not
        # survive an explicit workflow stop.
        self._condition_memory.clear()

    def is_active(self):
        return self._thread is not None and self._thread.is_alive()

    def clear_condition_memory(self):
        """Forget run-scoped condition latches after a target replacement."""
        if not self.is_active():
            self._condition_memory.clear()

    def _run(self):
        try:
            self.logger.info("CYCLE_STARTED cycle=%s step=%s", self.current_cycle, self._restart_step_label())
            self._move_to_next_step()
            while not self._stop_event.is_set():
                if self.state == WorkflowState.WAIT_TRIGGER:
                    # Global stop trigger has higher priority than step trigger.
                    if self._check_stop_trigger():
                        if self.pending_stop:
                            self._perform_pending_stop()
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
                        if not self._handle_inactive_player_result():
                            break
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
                    # running. A match asks the existing player for its bounded,
                    # input-releasing stop path before another action can begin.
                    self._check_stop_trigger(allow_during_macro=True)
                    if self.pending_stop:
                        self._perform_pending_stop()
                        break
                    if self.player.is_active():
                        time.sleep(0.05)
                    else:
                        if not self._handle_inactive_player_result():
                            break
                        self._complete_macro_timing()
                        self.logger.info("Workflow step %s macro finished", self.current_step.get("id"))
                        self._stop_active_trigger_runner()
                        if self.pending_stop:
                            self._perform_pending_stop()
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
                if self.player.is_active():
                    self._complete_macro_timing()
                    self.player.stop()
                self.state = WorkflowState.STOPPED
                if self.finish_reason is None:
                    self.finish_reason = "manual_stop" if self.stopped_by_user else "workflow_stopped"
            # Run-scoped latches never survive a terminal workflow boundary.
            self._condition_memory.clear()
            if self.workflow_process_diagnostics is not None:
                try:
                    self.workflow_process_diagnostics.workflow_finished(
                        self.get_runtime_snapshot()
                    )
                except Exception:
                    self.logger.exception(
                        "Workflow process diagnostics finalization failed"
                    )

    def _move_to_next_step(self):
        self.current_step_index += 1
        steps = self.workflow["steps"]
        if self.current_step_index >= len(steps):
            self._on_cycle_boundary()
            return

        self.current_step = steps[self.current_step_index]
        self._step_macro_started = False
        try:
            trigger_data = self._build_trigger_data(self.current_step)
            self._trigger_runner_generation += 1
            generation = self._trigger_runner_generation
            self._active_trigger_runner = TriggerRunner(
                self.text_detector,
                self.script_store,
                self.player,
                trigger_data,
                logger=self.logger,
                on_status=lambda status, token=generation: self._on_trigger_status(token, status),
                stop_on_error=True,
                condition_memory=self._condition_memory_for("step", self.current_step.get("id"), trigger_data["trigger"]),
                can_start_macro=self._can_start_step_macro,
                expected_target_session=self._expected_target_session,
                input_safety_gate=self.input_safety_gate,
                on_input_blocked=lambda reason, token=generation: self._on_input_blocked(reason, token),
                workflow_process_diagnostics=self.workflow_process_diagnostics,
            )
            if self.workflow_process_diagnostics is not None:
                self.workflow_process_diagnostics.stage(
                    "TRIGGER_RUNNER_CREATED",
                    step_id=self.current_step.get("id"),
                    trigger_runner_generation=generation,
                )
            self._active_trigger_runner.start()
            self.state = WorkflowState.WAIT_TRIGGER
            self.logger.info("Workflow step active: %s", self.current_step.get("id"))
        except Exception as exc:
            self._active_trigger_runner = None
            self.last_step_start_error = str(exc)
            self._set_runtime_error(f"STEP_START_FAILED step={self.current_step.get('id')}: {exc}")
            # This boundary deliberately records a traceback in the
            # application log; diagnostics will turn the snapshot into the
            # durable STEP_START_FAILED and RUN_FAILED events.
            self.logger.exception("STEP_START_FAILED step=%s", self.current_step.get("id"))

    def _on_cycle_boundary(self):
        # A cycle is completed only when the last step macro has completed and we reach here.
        self.completed_cycles += 1
        self.logger.info("CYCLE_COMPLETED cycle=%s", self.completed_cycles)

        if self.pending_stop:
            self._perform_pending_stop()
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
                    self._perform_pending_stop()
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
        if self.pending_stop or (self.state not in allowed_states and not force):
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
                return self._request_stop_from_trigger(result)
        except Exception as exc:
            self._stop_last_error = str(exc)
            self._set_runtime_error(str(exc))
            return False
        return self._stop_trigger_matched

    def _request_stop_from_trigger(self, result):
        """Atomically accept the first stop source for this workflow run."""
        details = {
            "trigger_id": self.stop_trigger_id,
            "trigger_name": self.stop_trigger_name,
            "target_text": self.stop_trigger_config.get("text") if self.stop_trigger_config else None,
            "condition": self.stop_trigger_config.get("condition") if self.stop_trigger_config else None,
            "event": getattr(result, "event_name", None),
        }
        if not self.request_immediate_stop("stop_trigger", details):
            return False
        self._stop_trigger_matched = True
        self._stop_last_trigger_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.logger.info("STOP_TRIGGER_MATCHED event=%s", result.event_name)
        return True

    def _can_start_step_macro(self):
        """Shared stop gate used immediately before a step runner starts a macro."""
        with self._stop_lock:
            return not self.terminal_claimed and not self.pending_stop and not self.new_work_blocked and self.state not in {WorkflowState.STOPPING, WorkflowState.STOPPED, WorkflowState.FINISHED, WorkflowState.ERROR}

    def _on_input_blocked(self, reason, generation=None):
        if generation is not None and generation != self._trigger_runner_generation:
            return False
        result = getattr(self.player, "get_last_result", lambda: None)()
        if getattr(result, "status", None) is not PlaybackStatus.INPUT_BLOCKED:
            return False
        self.request_immediate_stop(
            "input_blocked",
            {
                "authorization": str(reason),
                "input_block_source": getattr(result, "source", None) or "player_playback",
                "action_index": getattr(result, "action_index", None),
                "player_terminal_status": getattr(getattr(result, "status", None), "value", None),
                "player_finished_at": getattr(result, "finished_at_monotonic", None),
            },
        )
        return True

    def _handle_inactive_player_result(self):
        result = getattr(self.player, "get_last_result", lambda: None)()
        status = getattr(result, "status", None)
        if status is PlaybackStatus.COMPLETED:
            duration_ms = getattr(result, "duration_ms", None)
            if duration_ms is not None:
                self._last_macro_duration_ms = int(duration_ms)
            self._macro_started_monotonic = None
            return True
        if status is PlaybackStatus.INPUT_BLOCKED:
            self._on_input_blocked(getattr(result, "reason", None))
            self._perform_pending_stop()
            return False
        if status is PlaybackStatus.FAILED:
            self._set_runtime_error(getattr(result, "reason", None) or "Macro playback failed")
            return False
        if status is PlaybackStatus.CANCELLED:
            if not self.terminal_claimed:
                self.request_immediate_stop("workflow_stop", {"playback": "cancelled"})
            self._perform_pending_stop()
            return False
        self._set_runtime_error("Macro playback ended without a terminal result")
        return False

    def _perform_pending_stop(self):
        """Stop runtime components at a bounded safe point and finalize once."""
        if not self.pending_stop:
            return
        self.state = WorkflowState.STOPPING
        self._complete_macro_timing()
        self._finish(WorkflowState.STOPPED, self.stop_source or "workflow_stop")

    def _finish(self, state, reason):
        if self.terminal_claimed and state == WorkflowState.FINISHED:
            return
        self.state = state
        self.finish_reason = reason
        if state == WorkflowState.FINISHED:
            self.logger.info("Workflow finished: %s (reason=%s)", self.workflow["name"], reason)
        elif state == WorkflowState.STOPPED:
            self.logger.info("Workflow stopped: %s (reason=%s)", self.workflow["name"], reason)

    def _set_runtime_error(self, message):
        with self._stop_lock:
            if self.terminal_claimed:
                return
            self.terminal_claimed = True
            self.new_work_blocked = True
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
            self._on_trigger_status(self._trigger_runner_generation, runner.get_status_snapshot())
            self._active_trigger_runner = None
            self._trigger_runner_generation += 1

    def _complete_macro_timing(self):
        if self._macro_started_monotonic is not None:
            self._last_macro_duration_ms = int(round((time.monotonic() - self._macro_started_monotonic) * 1000.0))
            self._macro_started_monotonic = None

    def _build_trigger_data(self, step):
        trigger = dict(step["trigger"])
        trigger.setdefault("type", "text")
        if trigger.get("type") == "text":
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

    def _on_trigger_status(self, generation, status):
        if (
            generation != self._trigger_runner_generation
            or self.state in {WorkflowState.FINISHED, WorkflowState.STOPPED, WorkflowState.ERROR}
        ):
            return
        with self._status_lock:
            self._latest_trigger_status = status

    def _stop_trigger_snapshot(self):
        trigger_status = self._stop_text_trigger.get_status_snapshot() if self._stop_text_trigger else {}
        return {
            "enabled": self.stop_trigger_enabled,
            "trigger_text": self.stop_trigger_config.get("text") if self.stop_trigger_config else None,
            "trigger_id": self.stop_trigger_id,
            "trigger_name": self.stop_trigger_name,
            "trigger_event": self.stop_trigger_config.get("event") if self.stop_trigger_config else None,
            "trigger_condition": self.stop_trigger_config.get("condition") if self.stop_trigger_config else None,
            "trigger_condition_label": label_for_code(ui_code_from_trigger(self.stop_trigger_config or {})),
            "trigger_type": self.stop_trigger_config.get("type") if self.stop_trigger_config else None,
            "poll_count": self._stop_poll_count,
            "last_ocr_text": self._stop_last_ocr_text,
            "last_error": self._stop_last_error,
            "pending_stop": self.pending_stop,
            "terminal_claimed": self.terminal_claimed,
            "new_work_blocked": self.new_work_blocked,
            "matched": self._stop_trigger_matched,
            "matched_time": self._stop_last_trigger_time,
            "stop_source": self.stop_source,
            "macro_cancel_requested": self._stop_macro_cancel_requested,
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
        player_result = getattr(self.player, "get_last_result", lambda: None)()
        return {
            "workflow_name": self.workflow.get("name") if self.workflow else None,
            "state": self.state.name,
            "stopped_by_user": self.stopped_by_user,
            "error": self.last_error,
            "step_start_error": self.last_step_start_error,
            "finish_reason": self.finish_reason,
            "step_number": step_number,
            "total_steps": total_steps,
            "step_id": step.get("id"),
            "trigger_text": trigger.get("text"),
            "trigger_event": trigger.get("event"),
            "macro": step.get("macro"),
            "trigger_runtime": trigger_status,
            "playback_status": getattr(getattr(player_result, "status", None), "value", None),
            "loop_mode": loop_mode,
            "restart_step": self.restart_step,
            "max_cycles": self.max_cycles,
            "current_cycle": self.current_cycle,
            "completed_cycles": self.completed_cycles,
            "pending_stop": self.pending_stop,
            "terminal_claimed": self.terminal_claimed,
            "new_work_blocked": self.new_work_blocked,
            "stop_source": self.stop_source,
            "input_safety": self._input_safety_snapshot(trigger_status),
            "stop_trigger_runtime": self._stop_trigger_snapshot(),
            "capture_runtime": self.text_detector.get_capture_runtime_diagnostics(),
        }

    def _condition_memory_for(self, scope, identity, trigger):
        signature = json.dumps({"condition": trigger.get("condition"), "event": trigger.get("event"), "text": trigger.get("text")}, ensure_ascii=False, sort_keys=True)
        return self._condition_memory.setdefault((scope, str(identity), signature), {})

    def _input_safety_snapshot(self, trigger_status):
        authorization = trigger_status.get("input_authorization") or {}
        player_authorization = getattr(self.player, "get_last_authorization", lambda: None)()
        player_snapshot = getattr(player_authorization, "snapshot", None)
        if player_snapshot is not None:
            authorization = {
                "code": getattr(getattr(player_authorization, "code", None), "value", None),
                "foreground_hwnd": getattr(player_authorization, "foreground_hwnd", 0),
                "foreground_root_hwnd": getattr(player_authorization, "foreground_root_hwnd", 0),
                "locked_root_hwnd": getattr(player_snapshot, "root_hwnd", 0),
                "current_session_id": getattr(player_snapshot, "session_id", None),
                "current_generation": getattr(player_snapshot, "generation", None),
                "target_pid": getattr(player_snapshot, "pid", None),
                "target_visibility": getattr(
                    getattr(player_snapshot, "visibility_state", None), "value", None
                ),
            }
        foreground_info = None
        foreground_hwnd = authorization.get("foreground_hwnd")
        if self.input_safety_gate is not None and foreground_hwnd:
            try:
                foreground_info = self.input_safety_gate.window_tracker.query_window(
                    foreground_hwnd
                )
            except (OSError, RuntimeError, TypeError):
                foreground_info = None
        locked_root = authorization.get("locked_root_hwnd")
        foreground_root = authorization.get("foreground_root_hwnd")
        player_result = getattr(self.player, "get_last_result", lambda: None)()
        stop_details = self.stop_details or {}
        return {
            "blocked": self.stop_source == "input_blocked",
            "reason": (self.stop_details or {}).get("authorization"),
            "suspended": bool(trigger_status.get("input_suspended")),
            "foreground_restored": bool(trigger_status.get("foreground_restored")),
            "expected_session_id": getattr(self._expected_target_session, "session_id", None),
            "expected_generation": getattr(self._expected_target_session, "generation", None),
            "current_session_id": authorization.get("current_session_id"),
            "current_generation": authorization.get("current_generation"),
            "authorization_code": authorization.get("code"),
            "locked_root_hwnd": authorization.get("locked_root_hwnd"),
            "foreground_hwnd": authorization.get("foreground_hwnd"),
            "foreground_root_hwnd": authorization.get("foreground_root_hwnd"),
            "target_pid": authorization.get("target_pid"),
            "target_visibility": authorization.get("target_visibility"),
            "cached_target_visibility": authorization.get("target_visibility"),
            "live_foreground_match": bool(
                locked_root and foreground_root and int(locked_root) == int(foreground_root)
            ),
            "foreground_pid": getattr(foreground_info, "pid", None),
            "foreground_process_name": getattr(foreground_info, "process_name", None),
            "foreground_executable": getattr(foreground_info, "executable_path", None),
            "foreground_title": getattr(foreground_info, "title", None),
            "foreground_class": getattr(foreground_info, "window_class", None),
            "locked_title": getattr(player_snapshot, "window_title", None),
            "locked_class": getattr(player_snapshot, "window_class", None),
            "locked_pid": getattr(player_snapshot, "pid", None),
            "screenbot_pid": os.getpid(),
            "screenbot_root_hwnd": (
                foreground_root
                if getattr(foreground_info, "pid", None) == os.getpid()
                else None
            ),
            "screenbot_is_foreground": getattr(foreground_info, "pid", None) == os.getpid(),
            "input_block_source": stop_details.get("input_block_source"),
            "action_index": stop_details.get("action_index"),
            "player_active": self.player.is_active(),
            "player_terminal_status": getattr(
                getattr(player_result, "status", None), "value", None
            ),
            "player_finished_at": getattr(player_result, "finished_at_monotonic", None),
            "workflow_state": self.state.name,
            "trigger_runner_state": {
                "active": bool(trigger_status.get("active")),
                "trigger_fired": bool(trigger_status.get("trigger_fired")),
            },
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
            trigger_type = trigger.get("type", "text")
            if trigger_type not in {"text", "workflow_start"}:
                raise ValueError(f"Workflow step {step_id} has unsupported trigger type")
            if trigger_type == "text":
                normalize_condition(trigger.get("condition"), trigger.get("event"))
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
        stop_trigger = loop.get("stop_trigger")
        if loop.get("stop_trigger_ref") and stop_trigger is None:
            raise ValueError("loop.stop_trigger_ref was not resolved")
        if mode is None and not isinstance(stop_trigger, dict):
            raise ValueError("loop without mode requires a resolved stop_trigger")
        if mode is not None and mode not in {"manual_stop", "max_cycles", "stop_trigger"}:
            raise ValueError(f"Unsupported loop mode: {mode}")

        loop_norm = {}
        if mode is not None:
            restart_step = loop.get("restart_step")
            if not isinstance(restart_step, str) or not restart_step.strip():
                raise ValueError("Loop restart_step is required")
            if restart_step not in seen_ids:
                raise ValueError(f"Loop restart step does not exist: {restart_step}")
            loop_norm.update({"mode": mode, "restart_step": restart_step})
        if mode == "max_cycles":
            max_cycles = loop.get("max_cycles")
            if not isinstance(max_cycles, int) or max_cycles < 1:
                raise ValueError("loop.max_cycles must be an integer >= 1")
            loop_norm["max_cycles"] = max_cycles

        if stop_trigger is not None:
            if loop.get("stop_trigger_ref") and not loop.get("stop_trigger"):
                raise ValueError("loop.stop_trigger_ref was not resolved")
            if not isinstance(stop_trigger, dict):
                raise ValueError("loop.stop_trigger must be an object")
            if stop_trigger.get("type", "text") != "text":
                raise ValueError("loop.stop_trigger supports only text type")
            normalize_condition(stop_trigger.get("condition"), stop_trigger.get("event"))
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
                **({"id": stop_trigger["id"]} if stop_trigger.get("id") else {}),
                **({"name": stop_trigger["name"]} if stop_trigger.get("name") else {}),
                "type": "text",
                **({"condition": dict(stop_trigger["condition"])} if stop_trigger.get("condition") else {"event": stop_trigger["event"]}),
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
