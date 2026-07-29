"""Environment-gated Workflow-time process/window attribution.

The diagnostic uses the normal Start/Workflow/Trigger/OCR path, but owns a
fail-closed quarantine boundary before any ScriptPlayer start.  It observes
subprocess arguments without changing them and never emits input.
"""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import psutil
import threading
import time
from uuid import uuid4

from app.ocr_process_diagnostics import OcrProcessDiagnostics


class WorkflowProcessDiagnostics(OcrProcessDiagnostics):
    MAX_TRACE_SECONDS = 15.0

    def __init__(self, root_path, logger=None, enabled=None, adapter=None):
        gate = (
            os.environ.get("SCREENBOT_WORKFLOW_PROCESS_DIAGNOSTIC") == "1"
            if enabled is None
            else bool(enabled)
        )
        super().__init__(root_path, logger=logger, enabled=gate, adapter=adapter)
        self._diagnostic_id = None
        self._request_id = None
        self._run_id = None
        self._workflow_identifier = None
        self._target_session_id = None
        self._target_generation = None
        self._target_root_hwnd = 0
        self._compact_hwnd = 0
        self._trace_context = None
        self._deadline_thread = None
        self._deadline_cancel = threading.Event()
        self._quarantine_callback = None
        self._quarantine_claimed = False
        self._observation_count = 0
        self._ending = False

    def _open_log(self):
        directory = self.root_path / "logs" / "workflow_process_diagnostics"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self._jsonl_path = directory / f"workflow_process_{stamp}.jsonl"
        self._text_path = directory / f"workflow_process_{stamp}.log"

    def record(self, event, **payload):
        correlated = {
            "diagnostic_id": self._diagnostic_id,
            "request_id": self._request_id,
            "run_id": self._run_id,
            "workflow_identifier": self._workflow_identifier,
            "target_session_id": self._target_session_id,
            "target_generation": self._target_generation,
            "target_root_hwnd": self._target_root_hwnd,
            "compact_hwnd": self._compact_hwnd,
            "thread_id": threading.get_native_id(),
        }
        correlated.update(payload)
        super().record(event, **correlated)

    def _window_snapshot(self, hwnd):
        snapshot = super()._window_snapshot(hwnd)
        hwnd_value = int(snapshot.get("hwnd") or 0)
        root = int(snapshot.get("root_hwnd") or 0)
        owner = int(snapshot.get("owner_hwnd") or 0)
        taskbar_eligible = bool(
            snapshot.get("is_visible")
            and hwnd_value
            and root == hwnd_value
            and owner == 0
            and (
                snapshot.get("is_appwindow")
                or not snapshot.get("is_toolwindow")
            )
        )
        snapshot["taskbar_eligible"] = taskbar_eligible
        if hwnd_value == self._compact_hwnd:
            snapshot["diagnostic_role"] = "SCREENBOT_COMPACT"
        elif hwnd_value == self._target_root_hwnd:
            snapshot["diagnostic_role"] = "TARGET"
        elif str(snapshot.get("process_name") or "").casefold() == "tesseract.exe":
            snapshot["diagnostic_role"] = "TESSERACT"
        elif str(snapshot.get("process_name") or "").casefold() in {
            "conhost.exe",
            "openconsole.exe",
            "cmd.exe",
        }:
            snapshot["diagnostic_role"] = "CONSOLE_HOST"
        elif snapshot.get("pid") == os.getpid():
            snapshot["diagnostic_role"] = "SCREENBOT_OTHER"
        else:
            snapshot["diagnostic_role"] = "OTHER"
        return snapshot

    def _process_sampler(self):
        known = set(self._baseline_pids)
        descendants = {os.getpid()}
        while not self._stop_event.wait(0.005):
            try:
                new_pids = set(psutil.pids()) - known
            except (psutil.Error, OSError):
                continue
            known.update(new_pids)
            for pid in new_pids:
                details = self._process_details(pid)
                parent = int(details.get("parent_pid") or 0)
                related = parent in descendants
                if related:
                    descendants.add(int(pid))
                name = str(details.get("process_name") or "").casefold()
                if related or name in {
                    "tesseract.exe",
                    "conhost.exe",
                    "openconsole.exe",
                    "cmd.exe",
                    "powershell.exe",
                    "python.exe",
                    "pythonw.exe",
                }:
                    self.record(
                        "PROCESS_SNAPSHOT_FIRST_SEEN",
                        **details,
                        screenbot_descendant=related,
                    )

    def begin_trace(
        self,
        *,
        workflow_identifier,
        target_snapshot,
        compact_hwnd,
        quarantine_callback,
    ):
        if not self.enabled:
            return False
        with self._state_lock:
            if self._active or self._consumed:
                return False
            self._active = True
            self._consumed = True
            self._ending = False
            self._quarantine_claimed = False
            self._observation_count = 0
            self._stop_event.clear()
            self._deadline_cancel.clear()
            self._diagnostic_id = f"workflow-diagnostic-{uuid4().hex}"
            self._probe_id = self._diagnostic_id
            self._observation_id = None
            self._workflow_identifier = str(workflow_identifier or "")
            self._target_session_id = getattr(target_snapshot, "session_id", None)
            self._target_generation = getattr(target_snapshot, "generation", None)
            self._target_root_hwnd = int(
                getattr(target_snapshot, "root_hwnd", 0) or 0
            )
            self._compact_hwnd = int(compact_hwnd or 0)
            self._quarantine_callback = quarantine_callback
            self._variant_counter = 0
            self._variant_by_token.clear()
            self._process_started.clear()
            self._process_watchers.clear()
            self._deadline = time.monotonic() + self.MAX_TRACE_SECONDS
            self._open_log()

        self.record(
            "WORKFLOW_DIAGNOSTIC_TRACE_STARTED",
            pid=os.getpid(),
            max_duration_seconds=self.MAX_TRACE_SECONDS,
            input_quarantine=True,
            target_window=self._window_snapshot(self._target_root_hwnd),
            compact_window=self._window_snapshot(self._compact_hwnd),
            foreground_hwnd=self.adapter.foreground_hwnd(),
            z_order=self._z_order(),
        )
        self._baseline_pids = set(psutil.pids())
        self._event_hook_handles = (
            self.adapter.install_win_event_hook(self._on_win_event) or ()
        )
        self._sampler_thread = threading.Thread(
            target=self._process_sampler,
            name="ScreenBotWorkflowProcessSampler",
            daemon=True,
        )
        self._window_sampler_thread = threading.Thread(
            target=self._window_sampler,
            name="ScreenBotWorkflowWindowSampler",
            daemon=True,
        )
        self._sampler_thread.start()
        self._window_sampler_thread.start()
        self._trace_context = self._trace_popen()
        self._trace_context.__enter__()
        self._deadline_thread = threading.Thread(
            target=self._deadline_loop,
            name="ScreenBotWorkflowDiagnosticDeadline",
            daemon=True,
        )
        self._deadline_thread.start()
        return True

    def _deadline_loop(self):
        if not self._deadline_cancel.wait(self.MAX_TRACE_SECONDS):
            self.request_quarantine(
                "trace_timeout",
                {"max_duration_seconds": self.MAX_TRACE_SECONDS},
            )

    def bind_request(self, request_id):
        self._request_id = request_id
        self.record("START_REQUEST_CORRELATED")

    def bind_run(self, run_id):
        self._run_id = run_id
        self.record("WORKFLOW_RUN_CORRELATED")

    def stage(self, event, **payload):
        if self.active:
            self.record(event, **payload)

    def variant_started(self, variant_id, **metadata):
        if not self.active:
            return None
        if self._observation_id is None:
            self._observation_id = f"observation-{uuid4().hex}"
            self.record("OCR_OBSERVATION_ENTER")
        # The workflow deadline is enforced by the quarantine thread.
        # Deliberately duplicate the tiny bookkeeping section from the probe
        # observer so this observer can never raise into production OCR.
        with self._state_lock:
            self._variant_counter += 1
            token = f"variant-{self._variant_counter}"
            entry = {
                "variant_id": str(variant_id),
                "variant_index": self._variant_counter,
                "started_monotonic_ns": time.monotonic_ns(),
                **metadata,
            }
            self._variant_by_token[token] = entry
            self._thread_local.variant_token = token
        self.record("OCR_VARIANT_STARTED", variant_token=token, **entry)
        return token

    def observation_completed(self, *, observation, result, step_id=None):
        if not self.active:
            return False
        self._observation_count += 1
        self.record(
            "OCR_OBSERVATION_EXIT",
            observation_count=self._observation_count,
            step_id=step_id,
            observation_state=getattr(
                getattr(observation, "state", None),
                "value",
                str(getattr(observation, "state", None)),
            ),
            recognized_text=getattr(observation, "recognized_text", ""),
            trigger_matched=bool(getattr(result, "triggered", False)),
            trigger_event=getattr(result, "event_name", None),
        )
        if getattr(result, "triggered", False):
            self.record(
                "WOULD_START_MACRO",
                step_id=step_id,
                player_start_blocked=True,
            )
        else:
            self.record("CONTINUE_WAITING", step_id=step_id)
        return self.request_quarantine(
            "first_observation_complete",
            {
                "step_id": step_id,
                "trigger_matched": bool(getattr(result, "triggered", False)),
            },
        )

    def request_quarantine(self, reason, details=None):
        with self._state_lock:
            if not self._active or self._quarantine_claimed:
                return False
            self._quarantine_claimed = True
            callback = self._quarantine_callback
        payload = dict(details or {})
        payload["reason"] = reason
        self.record("DIAGNOSTIC_INPUT_QUARANTINE_STOP", **payload)
        if callable(callback):
            callback(reason, payload)
        return True

    def workflow_finished(self, snapshot=None):
        if not self.active:
            return
        self.record(
            "WORKFLOW_DIAGNOSTIC_RUNTIME_FINISHED",
            runtime_snapshot=snapshot or {},
        )
        self.end_trace("workflow_finished")

    def end_trace(self, reason):
        with self._state_lock:
            if not self._active or self._ending:
                return
            self._ending = True
        self._deadline_cancel.set()
        context = self._trace_context
        self._trace_context = None
        if context is not None:
            context.__exit__(None, None, None)
        self._stop_event.set()
        current = threading.current_thread()
        for thread in (self._sampler_thread, self._window_sampler_thread):
            if thread is not None and thread is not current:
                thread.join(timeout=1.0)
        for watcher in tuple(self._process_watchers):
            if watcher is not current:
                watcher.join(timeout=0.25)
        self.adapter.uninstall_win_event_hook(self._event_hook_handles)
        self.record(
            "DIAGNOSTIC_TRACE_ENDED",
            reason=reason,
            observation_count=self._observation_count,
            foreground_hwnd=self.adapter.foreground_hwnd(),
            target_window=self._window_snapshot(self._target_root_hwnd),
            compact_window=self._window_snapshot(self._compact_hwnd),
            z_order=self._z_order(),
        )
        with self._state_lock:
            self._active = False
            self._ending = False
            self._deadline = None

    def close(self):
        self.end_trace("application_shutdown")
        super().close()
