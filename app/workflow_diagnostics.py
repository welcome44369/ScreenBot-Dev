import logging
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from app.runtime_run_log import RuntimeRunLogManager


class WorkflowDiagnostics:
    def __init__(self, root_path, logger=None, max_events=100, runtime_log_background_maintenance=True):
        self.root_path = Path(root_path)
        self.logger = logger or logging.getLogger("ScreenBot")
        self.max_events = max_events
        self.timeline = deque(maxlen=max_events)
        self.run_log_manager = RuntimeRunLogManager(
            self.root_path,
            self.logger,
            background_maintenance=runtime_log_background_maintenance,
        )
        self.reset()

    def reset(self):
        self.started_at = None
        self.stopped_at = None
        self.workflow_name = None
        self.workflow_state = "IDLE"
        self.current_step = None
        self.step_number = 0
        self.total_steps = 0
        self.current_trigger = None
        self.current_macro = None
        self.poll_count = 0
        self.trigger_count = 0
        self.macro_count = 0
        self.last_ocr_result = ""
        self.last_ocr_success_time = None
        self.consecutive_success = 0
        self.consecutive_failure = 0
        self.last_trigger_time = None
        self.cooldown_remaining_ms = 0
        self.confirm_progress = "0 / 0"
        self.trigger_state = "(未知)"
        self.macro_running = False
        self.last_macro_duration_ms = 0
        self.last_error = None
        self.last_error_source = None
        self.last_error_time = None
        self.finish_reason = None
        self.loop_mode = None
        self.restart_step = None
        self.max_cycles = None
        self.current_cycle = 1
        self.completed_cycles = 0
        self.pending_stop = False
        self.stop_trigger_enabled = False
        self.stop_trigger_text = None
        self.stop_trigger_event = None
        self.stop_trigger_confirm_progress = "N/A"
        self.stop_trigger_last_text = ""
        self.stop_trigger_timestamp = None
        self._last_logged_ocr = None
        self._last_logged_workflow_state = None
        self._last_logged_step_id = None
        self._last_logged_macro_running = False
        self._last_logged_trigger_fire = None
        self._last_poll_count = 0
        self._last_trigger_count = 0
        self._last_macro_count = 0
        self._last_logged_error = None
        self._last_logged_cycle_started = None
        self._last_logged_cycle_completed = 0
        self._last_logged_pending_stop = False
        self._last_logged_finish_reason = None
        self._last_logged_stop_trigger_matched = False
        self._last_logged_stop_trigger_enabled = False
        self._last_logged_stop_confirm_progress = None
        self._last_logged_observation_signature = None

    def add_event(self, category, message, *, event="TIMELINE_EVENT", data=None, force_flush=False):
        now = datetime.now()
        entry = {
            "ts": now.strftime("%H:%M:%S"),
            "category": category,
            "message": message,
            "line": f"{now.strftime('%H:%M:%S')} [{category}] {message}",
        }
        self.timeline.append(entry)
        self.run_log_manager.record(
            category,
            event,
            data=data or {"message": message},
            step_id=self.current_step,
            cycle=self.current_cycle,
            message=message,
            force_flush=force_flush,
        )

    @property
    def current_run_id(self):
        session = self.run_log_manager.active_session
        return session.run_id if session else None

    @property
    def current_run_log_path(self):
        session = self.run_log_manager.active_session
        return session.log_path if session else None

    def _finalize_active_run(self, status):
        summary = self.get_summary()
        summary["error_count"] = 1 if summary.get("last_error") else 0
        self.run_log_manager.finalize(status, summary)

    def finalize_run(self, status):
        self._finalize_active_run(status)

    def update_from_snapshot(self, snapshot):
        now = time.time()
        trigger_runtime = snapshot.get("trigger_runtime") or {}
        trigger_status = trigger_runtime.get("trigger_status") or {}
        trigger_result = trigger_runtime.get("trigger_result") or {}

        self.workflow_name = snapshot.get("workflow_name")
        self.workflow_state = snapshot.get("state", "IDLE")
        self.current_step = snapshot.get("step_id")
        self.step_number = snapshot.get("step_number", 0)
        self.total_steps = snapshot.get("total_steps", 0)
        self.current_trigger = f"{snapshot.get('trigger_event') or '(無)'} {snapshot.get('trigger_text') or '(無)'}"
        self.current_macro = snapshot.get("macro")
        self.last_error = snapshot.get("error")
        self.finish_reason = snapshot.get("finish_reason")
        self.loop_mode = snapshot.get("loop_mode")
        self.restart_step = snapshot.get("restart_step")
        self.max_cycles = snapshot.get("max_cycles")
        self.current_cycle = snapshot.get("current_cycle", 1)
        self.completed_cycles = snapshot.get("completed_cycles", 0)
        self.pending_stop = bool(snapshot.get("pending_stop"))

        # Detect the macro completion before cycle/workflow transitions.  A
        # single UI snapshot can already contain FINISHED and a completed
        # cycle, but the durable run log must never put those before the
        # macro's actual completion boundary.
        current_macro_running = bool(trigger_runtime.get("macro_running", False))
        current_macro_duration_ms = int(trigger_runtime.get("last_macro_duration_ms", 0) or 0)
        if current_macro_running and not self._last_logged_macro_running:
            self.add_event("Macro", f"Start {self.current_macro or '(unknown)'}", event="MACRO_STARTED", force_flush=True)
        if not current_macro_running and self._last_logged_macro_running:
            self.add_event(
                "Macro",
                f"Finished {self.current_macro or '(unknown)'} ({current_macro_duration_ms} ms)",
                event="MACRO_FINISHED",
                data={"duration_ms": current_macro_duration_ms},
                force_flush=True,
            )
        self._last_logged_macro_running = current_macro_running

        stop_runtime = snapshot.get("stop_trigger_runtime") or {}
        self.stop_trigger_enabled = bool(stop_runtime.get("enabled"))
        self.stop_trigger_text = stop_runtime.get("trigger_text")
        self.stop_trigger_event = stop_runtime.get("trigger_event")
        self.stop_trigger_confirm_progress = stop_runtime.get("confirm_progress") or "N/A"
        self.stop_trigger_last_text = stop_runtime.get("last_ocr_text") or ""
        self.stop_trigger_timestamp = stop_runtime.get("matched_time")

        if self.stop_trigger_enabled and not self._last_logged_stop_trigger_enabled:
            self.add_event("StopTrigger", "STOP_TRIGGER_STARTED", event="STOP_TRIGGER_STARTED")
        self._last_logged_stop_trigger_enabled = self.stop_trigger_enabled
        if self.stop_trigger_enabled and self.stop_trigger_confirm_progress != self._last_logged_stop_confirm_progress:
            self.add_event("StopTrigger", f"STOP_TRIGGER_CONFIRM progress={self.stop_trigger_confirm_progress}")
            self._last_logged_stop_confirm_progress = self.stop_trigger_confirm_progress

        if self.current_cycle != self._last_logged_cycle_started:
            self.add_event("Cycle", f"CYCLE_STARTED cycle={self.current_cycle} step={self.current_step or self.restart_step or '(none)'}", event="CYCLE_STARTED")
            self._last_logged_cycle_started = self.current_cycle

        if self.completed_cycles > self._last_logged_cycle_completed:
            self.add_event("Cycle", f"CYCLE_COMPLETED cycle={self.completed_cycles}", event="CYCLE_COMPLETED", force_flush=True)
            self._last_logged_cycle_completed = self.completed_cycles
            if self.loop_mode in {"manual_stop", "max_cycles", "stop_trigger"} and snapshot.get("state") not in {"FINISHED", "STOPPED", "ERROR"}:
                next_cycle = self.completed_cycles + 1
                self.add_event("Cycle", f"CYCLE_RESTARTED next_cycle={next_cycle} step={self.restart_step or '(none)'}")

        if self.pending_stop and not self._last_logged_pending_stop:
            self.add_event("StopTrigger", "PENDING_STOP_SET")
        self._last_logged_pending_stop = self.pending_stop

        if stop_runtime.get("matched") and not self._last_logged_stop_trigger_matched:
            self.add_event("StopTrigger", "STOP_TRIGGER_MATCHED")
        self._last_logged_stop_trigger_matched = bool(stop_runtime.get("matched"))

        if self.workflow_state != self._last_logged_workflow_state:
            self.add_event("Workflow", f"State = {self.workflow_state}", event="WORKFLOW_STATE_CHANGED", data={"state": self.workflow_state})
            self._last_logged_workflow_state = self.workflow_state

        if self.current_step and self.current_step != self._last_logged_step_id:
            self.add_event("Workflow", f"Current Step = {self.current_step} ({self.step_number}/{self.total_steps})", event="STEP_STARTED")
            self.add_event("Trigger", f"Waiting Text = {self.current_trigger}", event="TRIGGER_WAIT_STARTED")
            self._last_logged_step_id = self.current_step

        if trigger_runtime:
            self.poll_count = max(self.poll_count, trigger_runtime.get("poll_count", 0))
            self.trigger_count = max(self.trigger_count, trigger_runtime.get("trigger_fire_count", 0))
            self.macro_count = max(self.macro_count, trigger_runtime.get("macro_start_count", 0))
            self.last_ocr_result = trigger_runtime.get("last_ocr_text", "")
            self.last_ocr_success_time = trigger_runtime.get("last_ocr_success_time")
            self.consecutive_success = trigger_runtime.get("consecutive_success", 0)
            self.consecutive_failure = trigger_runtime.get("consecutive_failure", 0)
            self.macro_running = bool(trigger_runtime.get("macro_running", False))
            self.last_macro_duration_ms = int(trigger_runtime.get("last_macro_duration_ms", 0) or 0)

            candidate_count = trigger_status.get("candidate_count", 0) or 0
            confirm_frames = trigger_status.get("confirm_frames", 1) or 1
            self.confirm_progress = f"{candidate_count} / {confirm_frames}"
            self.cooldown_remaining_ms = int(trigger_status.get("cooldown_remaining_ms", 0) or 0)
            self.last_trigger_time = trigger_status.get("last_trigger_time")
            observation = trigger_status.get("observation") or {}
            observation_signature = (
                observation.get("state"),
                observation.get("exact_match"),
                round(float(observation.get("text_similarity", 0.0) or 0.0), 2),
                round(float(observation.get("readability_score", 0.0) or 0.0), 2),
                round(float(observation.get("presence_score", 0.0) or 0.0), 2),
                observation.get("reason"),
            )
            if observation and observation_signature != self._last_logged_observation_signature:
                self.add_event(
                    "Observation",
                    "state={state} exact={exact} similarity={similarity:.2f} "
                    "readability={readability:.2f} presence={presence:.2f} reason={reason}".format(
                        state=observation.get("state"),
                        exact=observation.get("exact_match"),
                        similarity=observation_signature[2],
                        readability=observation_signature[3],
                        presence=observation_signature[4],
                        reason=observation.get("reason") or "-",
                    ),
                    event="OBSERVATION_RECORDED",
                    data=dict(observation),
                )
                self._last_logged_observation_signature = observation_signature
            stable_present = trigger_status.get("stable_present")
            if stable_present is True:
                self.trigger_state = "存在"
            elif stable_present is False:
                self.trigger_state = "不存在"
            else:
                self.trigger_state = "(未知)"

            if self.last_ocr_result != self._last_logged_ocr:
                detected_text = self.last_ocr_result if self.last_ocr_result else "\"\""
                self.add_event("OCR", f"Detected = {detected_text}")
                self._last_logged_ocr = self.last_ocr_result

            trigger_fire_count = trigger_runtime.get("trigger_fire_count", 0)
            if trigger_fire_count > self._last_trigger_count:
                self.add_event("Trigger", "Trigger Fired", event="TRIGGER_FIRED", force_flush=True)
            self._last_trigger_count = max(self._last_trigger_count, trigger_fire_count)

            if trigger_runtime.get("last_error"):
                err = f"TriggerRunner: {trigger_runtime.get('last_error')}"
                if err != self._last_logged_error:
                    self.add_event("Error", err)
                    self._last_logged_error = err
                self.last_error = trigger_runtime.get("last_error")
                self.last_error_source = "TriggerRunner"
                self.last_error_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        workflow_error = snapshot.get("error")
        if workflow_error:
            err = f"WorkflowRunner: {workflow_error}"
            if err != self._last_logged_error:
                self.add_event("Error", err)
                self._last_logged_error = err
            self.last_error = workflow_error
            self.last_error_source = "WorkflowRunner"
            self.last_error_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if self.finish_reason and self.finish_reason != self._last_logged_finish_reason:
            self.add_event("Workflow", f"Finish Reason = {self.finish_reason}")
            self._last_logged_finish_reason = self.finish_reason
            if self.finish_reason == "manual_stop":
                self.add_event("Workflow", "MANUAL_STOP_REQUESTED")
                self.add_event("Workflow", "WORKFLOW_STOPPED")
            if self.finish_reason == "max_cycles_reached":
                self.add_event("Cycle", f"MAX_CYCLES_REACHED completed={self.completed_cycles} max={self.max_cycles}")
            if self.finish_reason == "stop_trigger_matched":
                self.add_event("StopTrigger", "STOP_TRIGGER_FINISHED_WORKFLOW")

        if snapshot.get("state") in {"FINISHED", "STOPPED", "ERROR"}:
            if self.stopped_at is None:
                self.stopped_at = now
            # The active TriggerRunner has already stopped when WorkflowRunner
            # exposes a terminal snapshot.  Force the final macro boundary to
            # be represented before closing this one-run session.
            if self._last_logged_macro_running:
                self._last_logged_macro_running = False
                self.macro_running = False
                self.add_event(
                    "Macro",
                    f"Finished {self.current_macro or '(unknown)'} ({self.last_macro_duration_ms} ms)",
                    event="MACRO_FINISHED",
                    data={"duration_ms": self.last_macro_duration_ms, "forced_at_terminal": True},
                    force_flush=True,
                )
            terminal_status = {
                "FINISHED": "FINISHED",
                "STOPPED": "STOPPED",
                "ERROR": "FAILED",
            }[snapshot.get("state")]
            self._finalize_active_run(terminal_status)

    def mark_workflow_started(self, workflow, target=None, capture_backend=None):
        self.reset()
        workflow_data = workflow if isinstance(workflow, dict) else {"name": workflow}
        self.workflow_name = workflow_data.get("name") or "(unnamed workflow)"
        self.started_at = time.time()
        steps = workflow_data.get("steps") or []
        loop = workflow_data.get("loop") or {}
        self.total_steps = len(steps)
        self.loop_mode = loop.get("mode")
        self.max_cycles = loop.get("max_cycles")
        self.restart_step = loop.get("restart_step")
        self.run_log_manager.start({
            "workflow_id": workflow_data.get("id"),
            "workflow_name": self.workflow_name,
            "target_hwnd": getattr(target, "hwnd", None),
            "target_title": getattr(target, "title", None),
            "target_client_size": [getattr(target, "client_width", None), getattr(target, "client_height", None)],
            "capture_backend": capture_backend,
            "loop_mode": self.loop_mode,
            "max_cycles": self.max_cycles,
            "restart_step": self.restart_step,
            "steps_total": self.total_steps,
        })
        self.add_event("Workflow", "Workflow Started", event="WORKFLOW_STATE_CHANGED", data={"state": "STARTING"}, force_flush=True)
        self.add_event("Workflow", f"Name = {self.workflow_name}", event="WORKFLOW_METADATA")

    def mark_workflow_stopped(self):
        self.stopped_at = time.time()
        self.add_event("Workflow", "STOP_REQUESTED", event="STOP_REQUESTED", force_flush=True)

    def mark_workflow_error(self, reason):
        self.last_error = reason
        self.last_error_source = "Application"
        self.last_error_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.add_event("Error", reason, event="WORKFLOW_FAILED", force_flush=True)

    def get_recent_lines(self, count=5):
        lines = [entry["line"] for entry in list(self.timeline)[-count:]]
        return lines

    def get_elapsed_seconds(self):
        if not self.started_at:
            return 0.0
        end = self.stopped_at if self.stopped_at else time.time()
        return max(0.0, end - self.started_at)

    def get_poll_rate_hz(self):
        elapsed = self.get_elapsed_seconds()
        if elapsed <= 0:
            return 0.0
        return self.poll_count / elapsed

    def export_runtime_log(self):
        """Return the existing current Run Log; never synthesize a timeline."""
        active_path = self.current_run_log_path
        if active_path is not None:
            return active_path
        candidates = sorted(self.run_log_manager.runtime_dir.glob("runtime_*_run_*.log"), key=lambda path: path.stat().st_mtime)
        if not candidates:
            raise FileNotFoundError("尚無可匯出的 Workflow Run Log")
        return candidates[-1]

    def print_timeline_to_console(self):
        for line in self.get_recent_lines(self.max_events):
            print(line)

    def get_summary(self):
        elapsed = self.get_elapsed_seconds()
        return {
            "workflow_name": self.workflow_name or "(none)",
            "workflow_state": self.workflow_state,
            "step": f"{self.step_number}/{self.total_steps}",
            "current_step": self.current_step or "(none)",
            "current_trigger": self.current_trigger or "(none)",
            "current_macro": self.current_macro or "(none)",
            "elapsed_seconds": round(elapsed, 3),
            "poll_count": self.poll_count,
            "poll_rate_hz": round(self.get_poll_rate_hz(), 3),
            "trigger_count": self.trigger_count,
            "macro_count": self.macro_count,
            "last_ocr_result": self.last_ocr_result,
            "last_ocr_success_time": self.last_ocr_success_time,
            "consecutive_success": self.consecutive_success,
            "consecutive_failure": self.consecutive_failure,
            "confirm_progress": self.confirm_progress,
            "cooldown_remaining_ms": self.cooldown_remaining_ms,
            "last_trigger_time": self.last_trigger_time,
            "macro_running": self.macro_running,
            "last_macro_duration_ms": self.last_macro_duration_ms,
            "last_error": self.last_error,
            "last_error_source": self.last_error_source,
            "last_error_time": self.last_error_time,
            "finish_reason": self.finish_reason,
            "loop_mode": self.loop_mode,
            "restart_step": self.restart_step,
            "max_cycles": self.max_cycles,
            "current_cycle": self.current_cycle,
            "completed_cycles": self.completed_cycles,
            "pending_stop": self.pending_stop,
            "stop_trigger_enabled": self.stop_trigger_enabled,
            "stop_trigger_text": self.stop_trigger_text,
            "stop_trigger_event": self.stop_trigger_event,
            "stop_trigger_confirm_progress": self.stop_trigger_confirm_progress,
            "stop_trigger_last_text": self.stop_trigger_last_text,
            "stop_trigger_timestamp": self.stop_trigger_timestamp,
        }

