"""Non-input regression coverage for the workflow UI collapse boundary."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.application import ScreenBotApp
from app.input_safety import ExpectedTargetSession
from app.start_handoff import StartHandoffService, StartHandoffState, StartRequestKind
from app.state import AppState


class _Widget:
    def __init__(self, collapse_result=True):
        self.collapse_result = collapse_result
        self.collapsed = False
        self.collapse_calls = 0
        self.restore_calls = 0

    def collapse_for_workflow_execution(self):
        self.collapse_calls += 1
        self.collapsed = bool(self.collapse_result)
        return self.collapsed

    def is_workflow_execution_collapsed(self):
        return self.collapsed

    def restore_after_workflow_execution(self):
        self.restore_calls += 1
        self.collapsed = False
        return True


class _Timer:
    def __init__(self): self.starts = 0; self.stops = 0
    def start(self): self.starts += 1
    def stop(self): self.stops += 1


class WorkflowUiCollapseTests(unittest.TestCase):
    def _app(self, widget=None):
        app = ScreenBotApp.__new__(ScreenBotApp)
        app.widget = widget or _Widget()
        app.workflow_execution_ui_state = "IDLE_EXPANDED"
        app.logger = SimpleNamespace(error=lambda *_args, **_kwargs: None, info=lambda *_args, **_kwargs: None)
        app._record_overlay_diagnostic = lambda *_args, **_kwargs: None
        app._refresh_handoff_presentation = lambda: None
        app._record_start_handoff_event = lambda *_args, **_kwargs: None
        app.set_state = lambda *_args, **_kwargs: None
        app.start_handoff_timer = _Timer()
        return app

    @patch("app.application.QApplication.processEvents")
    def test_collapse_acknowledgement_precedes_running_state_and_restores_once(self, _events):
        app = self._app()
        self.assertTrue(app._collapse_ui_for_workflow_execution())
        self.assertEqual(app.workflow_execution_ui_state, "COLLAPSED_ARMED")
        self.assertEqual(app.widget.collapse_calls, 1)
        self.assertTrue(app._collapse_ui_for_workflow_execution())
        self.assertEqual(app.widget.collapse_calls, 1)
        self.assertTrue(app._restore_ui_after_workflow_execution("terminal"))
        self.assertEqual(app.widget.restore_calls, 1)
        self.assertTrue(app._restore_ui_after_workflow_execution("duplicate_terminal"))
        self.assertEqual(app.widget.restore_calls, 1)

    @patch("app.application.QApplication.processEvents")
    def test_collapse_failure_cancels_handoff_before_timer_or_execution(self, _events):
        app = self._app(_Widget(collapse_result=False))
        app.start_handoff = StartHandoffService()
        app.input_safety_gate = SimpleNamespace(
            expected_current_session=lambda: ExpectedTargetSession("target", 1)
        )
        app.workflow_process_diagnostics = None
        app._attempt_start_handoff = lambda: self.fail("collapse failure must not hand off")

        self.assertFalse(app._arm_start_request(StartRequestKind.WORKFLOW, {"name": "safe"}, "safe"))
        self.assertEqual(app.start_handoff.get_snapshot().state, StartHandoffState.CANCELLED)
        self.assertEqual(app.start_handoff_timer.starts, 0)
        self.assertEqual(app.workflow_execution_ui_state, "IDLE_EXPANDED")

    @patch("app.application.QApplication.processEvents")
    def test_start_handoff_is_not_attempted_until_collapse_is_acknowledged(self, _events):
        app = self._app()
        app.start_handoff = StartHandoffService()
        app.input_safety_gate = SimpleNamespace(
            expected_current_session=lambda: ExpectedTargetSession("target", 1)
        )
        app.workflow_process_diagnostics = None
        observed_states = []
        app._attempt_start_handoff = lambda: observed_states.append(app.workflow_execution_ui_state)

        self.assertTrue(app._arm_start_request(StartRequestKind.WORKFLOW, {"name": "safe"}, "safe"))
        self.assertEqual(observed_states, ["COLLAPSED_ARMED"])
        self.assertEqual(app.widget.collapse_calls, 1)
        self.assertEqual(app.start_handoff_timer.starts, 1)

    @patch("app.application.QApplication.processEvents")
    def test_cancelled_armed_request_restores_collapsed_ui(self, _events):
        app = self._app()
        app.start_handoff = StartHandoffService()
        request = app.start_handoff.create_request(
            kind=StartRequestKind.WORKFLOW, target_session_id="target", target_generation=1,
            display_name="safe", frozen_payload={"name": "safe"},
        )
        app.start_handoff.arm(request)
        self.assertTrue(app._collapse_ui_for_workflow_execution())
        self.assertTrue(app._cancel_armed_start("cancelled"))
        self.assertEqual(app.widget.restore_calls, 1)
        self.assertEqual(app.workflow_execution_ui_state, "IDLE_EXPANDED")

    @patch("app.application.QApplication.processEvents")
    def test_terminal_player_completion_restores_collapsed_ui(self, _events):
        app = self._app()
        app.state = AppState.RUNNING
        app.set_state = lambda state: setattr(app, "state", state)
        self.assertTrue(app._collapse_ui_for_workflow_execution())
        app.workflow_execution_ui_state = "RUNNING_COLLAPSED"
        app._on_player_finished()
        self.assertEqual(app.widget.restore_calls, 1)
        self.assertEqual(app.workflow_execution_ui_state, "IDLE_EXPANDED")

    @patch("app.application.QApplication.processEvents")
    def test_player_error_restores_collapsed_ui(self, _events):
        app = self._app()
        app.state = AppState.RUNNING
        app.set_state = lambda state: setattr(app, "state", state)
        app._show_message = lambda *_args: None
        self.assertTrue(app._collapse_ui_for_workflow_execution())
        app.workflow_execution_ui_state = "RUNNING_COLLAPSED"
        app._on_player_error("test error")
        self.assertEqual(app.widget.restore_calls, 1)
        self.assertEqual(app.workflow_execution_ui_state, "IDLE_EXPANDED")

    @patch("app.application.QApplication.processEvents")
    def test_target_relock_invalidates_armed_request_and_restores_ui(self, _events):
        app = self._app()
        app.start_handoff = StartHandoffService()
        request = app.start_handoff.create_request(
            kind=StartRequestKind.WORKFLOW, target_session_id="target", target_generation=1,
            display_name="safe", frozen_payload={"name": "safe"},
        )
        app.start_handoff.arm(request)
        self.assertTrue(app._collapse_ui_for_workflow_execution())
        self.assertTrue(app._invalidate_armed_start("target_relocked", unavailable=False))
        self.assertEqual(app.widget.restore_calls, 1)
        self.assertEqual(app.workflow_execution_ui_state, "IDLE_EXPANDED")

    def test_f8_lock_only_does_not_collapse_or_start_a_workflow(self):
        app = self._app()
        app.state = SimpleNamespace(name="IDLE")
        app.workflow_runner = SimpleNamespace(is_active=lambda: False)
        calls = {"lock": 0}
        app.lock_or_refresh_target_only = lambda: calls.__setitem__("lock", calls["lock"] + 1)
        app._handle_f8()
        self.assertEqual(calls["lock"], 1)
        self.assertEqual(app.widget.collapse_calls, 0)
        self.assertEqual(app.workflow_execution_ui_state, "IDLE_EXPANDED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
