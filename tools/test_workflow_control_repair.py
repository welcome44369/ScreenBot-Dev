"""Regression checks for the workflow control-plane repair.

These are isolated tests: they use temporary directories and fake targets,
players, and trigger runners.  They never touch a user workflow, macro, or
runtime log directory.
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import logging
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication

from app.application import ScreenBotApp
from app.floating_widget import FloatingWidget
from app.runtime_run_log import _pid_is_alive
from app.state import AppState
from app.workflow_diagnostics import WorkflowDiagnostics
from app.workflow_runner import WorkflowRunner, WorkflowState


class _Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return record


class _TargetTracker:
    def __init__(self):
        self.target = None
        self.info = SimpleNamespace(title="Target", hwnd=123, client_width=1280, client_height=720)

    def lock_foreground_window(self):
        self.target = self.info
        return self.info


class _Detector:
    def __init__(self):
        self.invalidated = 0

    def invalidate_capture_target(self):
        self.invalidated += 1

    def get_capture_runtime_diagnostics(self):
        return {"selected_backend": "fake"}


class _Widget:
    def __init__(self):
        self.target_title = None
        self.running = None
        self.compact_views = []

    def set_target_title(self, title):
        self.target_title = title

    def render_compact_status(self, view_model):
        self.compact_views.append(dict(view_model))
        self.target_title = view_model.get("target_text")

    def set_workflow_running(self, running):
        self.running = running


class _FakePlayer:
    def is_active(self):
        return False

    def stop(self):
        pass


class _FakeTextDetector:
    def invalidate_capture_target(self):
        pass

    def get_capture_runtime_diagnostics(self):
        return {}


class _WorkflowRunnerControlFake:
    def __init__(self, active=False):
        self.active = active
        self.started = 0
        self.stopped = 0

    def is_active(self):
        return self.active

    def start(self):
        self.started += 1
        self.active = True

    def stop(self):
        self.stopped += 1
        self.active = False

    def get_runtime_snapshot(self):
        return {
            "state": "WAIT_TRIGGER" if self.active else "IDLE",
            "workflow_name": None,
            "trigger_text": None,
            "trigger_event": None,
            "macro": None,
            "current_cycle": 0,
            "error": None,
        }


class _DiagnosticsControlFake:
    def __init__(self):
        self.started = 0
        self.stop_requests = 0
        self.stopped = 0

    def mark_workflow_started(self, *args, **kwargs):
        self.started += 1

    def mark_workflow_stop_requested(self):
        self.stop_requests += 1

    def mark_workflow_stopped(self):
        self.stopped += 1


class _FakeTriggerRunner:
    starts = 0

    def __init__(self, detector, *args, **kwargs):
        self.detector = detector
        self.on_status = kwargs["on_status"]
        self.active = False

    def start(self):
        type(self).starts += 1
        self.active = True
        self.on_status({"poll_count": 1, "last_ocr_text": "probe"})

    def stop(self):
        self.active = False

    def is_active(self):
        return self.active

    def get_status_snapshot(self):
        return {"poll_count": 1, "last_ocr_text": "probe", "macro_start_count": 0}


class WorkflowControlRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_f8_locks_only_without_execution(self):
        app = ScreenBotApp.__new__(ScreenBotApp)
        app.logger = logging.getLogger("test.f8")
        app.workflow_runner = SimpleNamespace(
            is_active=lambda: False,
            start=lambda: self.fail("F8 must not start workflow"),
            last_error=None,
        )
        app.window_tracker = _TargetTracker()
        app.text_detector = _Detector()
        app.widget = _Widget()
        app.state = AppState.IDLE
        app.workflow_ui_state = "IDLE"
        app._recorded_event_count = 0
        app.workflow_diagnostics = SimpleNamespace(
            mark_workflow_started=lambda *_args, **_kwargs: self.fail("F8 must not create a run log"),
        )
        app.workflow_runner.get_runtime_snapshot = lambda: {
            "state": "IDLE", "workflow_name": None, "trigger_text": None,
            "trigger_event": None, "macro": None, "current_cycle": 0, "error": None,
        }
        app._show_message = lambda *_: self.fail("unexpected lock error")
        app._start_countdown = lambda: self.fail("F8 must not start countdown")
        app._start_script = lambda: self.fail("F8 must not start script")

        app._handle_f8()

        self.assertEqual(app.widget.compact_views[-1]["visual_state"], "TARGET_READY")
        self.assertIn("Target", app.widget.compact_views[-1]["target_text"])
        self.assertEqual(app.text_detector.invalidated, 1)
        self.assertEqual(app.state, AppState.IDLE)

    def test_step_start_failure_becomes_runner_error(self):
        runner = WorkflowRunner(_FakeTextDetector(), object(), _FakePlayer())
        runner.workflow = {"name": "Test", "steps": [{
            "id": "step_1",
            "trigger": {"text": "needle", "event": "appear", "region": {
                "x_ratio": 0, "y_ratio": 0, "width_ratio": 1, "height_ratio": 1,
            }},
            "macro": "macro.json",
        }]}
        with patch("app.workflow_runner.TriggerRunner", side_effect=RuntimeError("trigger init failed")):
            runner._move_to_next_step()

        snapshot = runner.get_runtime_snapshot()
        self.assertEqual(runner.state, WorkflowState.ERROR)
        self.assertIn("STEP_START_FAILED", snapshot["error"])
        self.assertEqual(snapshot["step_start_error"], "trigger init failed")

    def test_application_start_calls_runner_once_after_validation(self):
        app = ScreenBotApp.__new__(ScreenBotApp)
        app.workflow_data = {"name": "Test"}
        app.workflow_runner = _WorkflowRunnerControlFake()
        app.state = AppState.IDLE
        app.window_tracker = _TargetTracker()
        app.window_tracker.target = app.window_tracker.info
        app.text_detector = _Detector()
        app.workflow_diagnostics = _DiagnosticsControlFake()
        app.widget = _Widget()
        app.logger = logging.getLogger("test.start")
        app.workflow_ui_state = "IDLE"
        app.stop_text_trigger = lambda: None
        app._update_workflow_runtime_ui = lambda: None

        app.start_workflow()

        self.assertEqual(app.workflow_runner.started, 1)
        self.assertEqual(app.workflow_diagnostics.started, 1)
        self.assertEqual(app.workflow_ui_state, "STARTING")

    def test_stop_during_wait_requests_then_stops_without_new_run(self):
        app = ScreenBotApp.__new__(ScreenBotApp)
        app.workflow_runner = _WorkflowRunnerControlFake(active=True)
        app.workflow_diagnostics = _DiagnosticsControlFake()
        app.widget = _Widget()
        app.logger = logging.getLogger("test.stop")
        app.workflow_ui_state = "WAIT_TRIGGER"
        app._update_workflow_runtime_ui = lambda: None

        app.stop_workflow()

        self.assertEqual(app.workflow_diagnostics.stop_requests, 1)
        self.assertEqual(app.workflow_runner.stopped, 1)
        self.assertEqual(app.workflow_diagnostics.stopped, 1)
        self.assertEqual(app.workflow_ui_state, "STOPPED")
        app.stop_workflow()
        self.assertEqual(app.workflow_diagnostics.stop_requests, 1)

    def test_start_reaches_trigger_runner(self):
        _FakeTriggerRunner.starts = 0
        runner = WorkflowRunner(_FakeTextDetector(), object(), _FakePlayer())
        workflow = {"name": "Test", "steps": [{
            "id": "step_1",
            "trigger": {"text": "needle", "event": "appear", "region": {
                "x_ratio": 0, "y_ratio": 0, "width_ratio": 1, "height_ratio": 1,
            }},
            "macro": "macro.json",
        }]}
        runner.load_workflow(workflow)
        with patch("app.workflow_runner.TriggerRunner", _FakeTriggerRunner):
            runner.start()
            for _ in range(40):
                if _FakeTriggerRunner.starts:
                    break
                self.qt_app.processEvents()
                time.sleep(0.01)
            runner.stop()

        self.assertEqual(_FakeTriggerRunner.starts, 1)

    def test_compact_widget_maps_runtime_states(self):
        with tempfile.TemporaryDirectory() as directory:
            widget = FloatingWidget(Path(directory))
            widget.set_workflow_runtime_info({
                "status": "WAIT_TRIGGER", "workflow_name": "Infinite", "trigger_text": "尋找採集物",
                "trigger_event": "disappear", "current_cycle": 1,
            })
            self.assertIn("Infinite", widget.workflow_section_label.text())
            widget.render_compact_status({
                "visual_state": "WAIT_TRIGGER",
                "status_text": "Waiting trigger",
                "target_text": "Target: locked",
                "operation_text": "Current operation: wait",
            })
            self.assertIn("Waiting trigger", widget.state_text_label.text())
            self.assertIn("Current operation", widget.event_count_label.text())
            self.assertIn("#d97706", widget.state_badge.styleSheet())
            widget.set_workflow_running(True)
            self.assertFalse(widget.start_workflow_button.isEnabled())
            self.assertTrue(widget.stop_workflow_button.isEnabled())
            widget.set_workflow_running(False)
            self.assertTrue(widget.start_workflow_button.isEnabled())
            self.assertFalse(widget.stop_workflow_button.isEnabled())
            widget.close()

    def test_compact_presentation_priority_and_target_ready_contract(self):
        app = ScreenBotApp.__new__(ScreenBotApp)
        app.state = AppState.IDLE
        app.window_tracker = _TargetTracker()
        app.window_tracker.target = app.window_tracker.info
        app.workflow_ui_state = "IDLE"
        app._recorded_event_count = 0
        app.current_script = None
        app.countdown_value = 0
        app.workflow_runner = _WorkflowRunnerControlFake(active=False)
        app.widget = _Widget()

        ready = app._refresh_compact_presentation()
        self.assertEqual(ready["visual_state"], "TARGET_READY")
        self.assertIn("尚未執行工作流", ready["operation_text"])

        stopping = app._refresh_compact_presentation(
            {"state": "WAIT_TRIGGER", "error": None}, {"status": "STOPPING"},
        )
        self.assertEqual(stopping["visual_state"], "STOPPING")

        error = app._refresh_compact_presentation(
            {"state": "WAIT_TRIGGER", "error": "boom"}, {"status": "WAIT_TRIGGER", "last_error": "boom"},
        )
        self.assertEqual(error["visual_state"], "ERROR")

    def test_compact_presentation_target_invalid_and_recording(self):
        class _InvalidTarget:
            title = "Closed"
            hwnd = 12

            def is_valid(self):
                return False

        app = ScreenBotApp.__new__(ScreenBotApp)
        app.state = AppState.IDLE
        app.window_tracker = SimpleNamespace(target=_InvalidTarget())
        app.workflow_ui_state = "IDLE"
        app._recorded_event_count = 0
        app.current_script = None
        app.countdown_value = 0
        app.workflow_runner = _WorkflowRunnerControlFake(active=False)
        app.widget = _Widget()
        self.assertEqual(app._refresh_compact_presentation()["visual_state"], "TARGET_INVALID")

        app.state = AppState.RECORDING
        app._recorded_event_count = 4
        recording = app._refresh_compact_presentation()
        self.assertEqual(recording["visual_state"], "RECORDING")
        self.assertIn("4", recording["operation_text"])

    def test_run_log_failures_do_not_escape_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = WorkflowDiagnostics(directory, runtime_log_background_maintenance=False)
            diagnostics.run_log_manager.record = lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("denied"))
            diagnostics.add_event("Test", "continues", event="TEST_EVENT")
            self.assertEqual(diagnostics.get_recent_lines(1)[0].split()[-1], "continues")
            diagnostics.run_log_manager.start = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("open failed"))
            diagnostics.mark_workflow_started({"name": "Test", "steps": []})
            diagnostics.run_log_manager.finalize = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("flush failed"))
            diagnostics.finalize_run("FAILED")

    def test_step_start_error_is_durable_and_finalized(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = WorkflowDiagnostics(directory, runtime_log_background_maintenance=False)
            diagnostics.mark_workflow_started({"id": "wf", "name": "Test", "steps": [{"id": "step_1"}]})
            diagnostics.update_from_snapshot({
                "workflow_name": "Test", "state": "ERROR", "error": "STEP_START_FAILED step=step_1: boom",
                "step_start_error": "boom", "step_id": "step_1", "step_number": 1, "total_steps": 1,
                "trigger_text": "needle", "trigger_event": "appear", "macro": "macro.json",
                "trigger_runtime": {}, "loop_mode": None, "restart_step": None, "max_cycles": None,
                "current_cycle": 1, "completed_cycles": 0, "pending_stop": False,
                "stop_trigger_runtime": {}, "finish_reason": "runtime_error",
            })
            jsonl = next((Path(directory) / "logs" / "runtime").glob("*.jsonl"))
            events = [json.loads(line)["event"] for line in jsonl.read_text(encoding="utf-8").splitlines()]
            self.assertIn("STEP_START_FAILED", events)
            self.assertEqual(events[-1], "RUN_FAILED")

    def test_windows_pid_probe_is_conservative(self):
        self.assertTrue(_pid_is_alive(os.getpid()))
        self.assertIn(_pid_is_alive(0), {False, None})


if __name__ == "__main__":
    unittest.main(verbosity=2)
