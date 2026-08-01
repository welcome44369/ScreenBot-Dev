"""Focused regression coverage for reversible workflow collapse monitoring UI."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.application import ScreenBotApp
from app.floating_widget import FloatingWidget


APP = QApplication.instance() or QApplication([])


class _WidgetStub:
    def __init__(self):
        self.collapsed = False
        self.collapse_calls = 0
        self.restore_calls = 0
        self.set_expanded_calls = []

    def collapse_for_workflow_execution(self):
        self.collapse_calls += 1
        self.collapsed = True
        return True

    def is_workflow_execution_collapsed(self):
        return self.collapsed

    def restore_after_workflow_execution(self):
        self.restore_calls += 1
        self.collapsed = False
        return True

    def set_workflow_execution_panel_expanded(self, expanded):
        self.set_expanded_calls.append(bool(expanded))
        self.collapsed = not bool(expanded)
        return True


class WorkflowUiToggleTests(unittest.TestCase):
    def _app(self):
        app = ScreenBotApp.__new__(ScreenBotApp)
        app.widget = _WidgetStub()
        app.workflow_execution_ui_state = "RUNNING_COLLAPSED"
        app.workflow_ui_state = "WAIT_TRIGGER"
        app.workflow_data = {"id": "wf_1", "name": "採花"}
        app.workflow_diagnostics = SimpleNamespace(current_run_id="run_1")
        app.target_session = SimpleNamespace(
            get_snapshot=lambda: SimpleNamespace(session_id="target_1")
        )
        app.logger = SimpleNamespace(error=lambda *_a, **_k: None, info=lambda *_a, **_k: None)
        app._recorded = []
        app._record_overlay_diagnostic = lambda event, **data: app._recorded.append((event, data))
        app.lock_or_refresh_target_only_calls = 0
        app.lock_or_refresh_target_only = (
            lambda: setattr(app, "lock_or_refresh_target_only_calls", app.lock_or_refresh_target_only_calls + 1)
        )
        app.unlock_calls = 0
        app.unlock_target = lambda: setattr(app, "unlock_calls", app.unlock_calls + 1)
        return app

    def test_explicit_start_auto_collapses_once(self):
        app = self._app()
        app.workflow_execution_ui_state = "IDLE_EXPANDED"
        self.assertTrue(app._collapse_ui_for_workflow_execution())
        self.assertEqual(app.workflow_execution_ui_state, "COLLAPSED_ARMED")
        self.assertEqual(app.widget.collapse_calls, 1)
        self.assertTrue(app._collapse_ui_for_workflow_execution())
        self.assertEqual(app.widget.collapse_calls, 1)

    def test_collapsed_running_ui_has_expand_control(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            widget = FloatingWidget(Path(temp_dir))
            widget.collapse_for_workflow_execution()
            self.assertFalse(widget.workflow_running_strip.isHidden())
            self.assertEqual(widget.workflow_running_expand_button.text(), "展開")
            self.assertIn("展開", widget.workflow_running_expand_button.toolTip())
            widget.close()

    def test_manual_expand_does_not_restart_workflow(self):
        app = self._app()
        starts = {"count": 0}
        app._start_workflow_from_handoff = lambda *_a, **_k: starts.__setitem__("count", starts["count"] + 1)
        app._on_workflow_execution_panel_visibility_changed(True)
        self.assertEqual(starts["count"], 0)

    def test_manual_expand_preserves_execution_id(self):
        app = self._app()
        before = app.workflow_diagnostics.current_run_id
        app._on_workflow_execution_panel_visibility_changed(True)
        self.assertEqual(app.workflow_diagnostics.current_run_id, before)

    def test_manual_expand_preserves_target_session(self):
        app = self._app()
        before = app.target_session.get_snapshot().session_id
        app._on_workflow_execution_panel_visibility_changed(True)
        after = app.target_session.get_snapshot().session_id
        self.assertEqual(after, before)

    def test_manual_expand_prevents_recollapse_on_status_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            widget = FloatingWidget(Path(temp_dir))
            widget.collapse_for_workflow_execution()
            self.assertTrue(widget.set_workflow_execution_panel_expanded(True))
            widget.set_workflow_runtime_info(
                {"workflow_name": "採花", "status": "WAIT_TRIGGER", "current_cycle": 1, "step_number": 1, "total_steps": 2}
            )
            self.assertFalse(widget.is_workflow_execution_collapsed())
            self.assertFalse(widget.details_widget.isHidden())
            widget.close()

    def test_manual_expand_prevents_recollapse_on_next_cycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            widget = FloatingWidget(Path(temp_dir))
            widget.collapse_for_workflow_execution()
            self.assertTrue(widget.set_workflow_execution_panel_expanded(True))
            widget.set_workflow_runtime_info(
                {"workflow_name": "採花", "status": "WAIT_TRIGGER", "current_cycle": 2, "step_number": 1, "total_steps": 2}
            )
            self.assertFalse(widget.is_workflow_execution_collapsed())
            self.assertFalse(widget.details_widget.isHidden())
            widget.close()

    def test_manual_collapse_returns_to_running_strip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            widget = FloatingWidget(Path(temp_dir))
            widget.collapse_for_workflow_execution()
            self.assertTrue(widget.set_workflow_execution_panel_expanded(True))
            self.assertTrue(widget.toggle_workflow_execution_panel())
            self.assertTrue(widget.is_workflow_execution_collapsed())
            self.assertFalse(widget.workflow_running_strip.isHidden())
            widget.close()

    def test_toggle_does_not_change_workflow_state(self):
        app = self._app()
        before = app.workflow_ui_state
        app._on_workflow_execution_panel_visibility_changed(True)
        self.assertEqual(app.workflow_ui_state, before)
        app._on_workflow_execution_panel_visibility_changed(False)
        self.assertEqual(app.workflow_ui_state, before)

    def test_toggle_does_not_change_f8_state(self):
        app = self._app()
        app._on_workflow_execution_panel_visibility_changed(True)
        app._on_workflow_execution_panel_visibility_changed(False)
        self.assertEqual(app.lock_or_refresh_target_only_calls, 0)

    def test_toggle_does_not_unlock_target(self):
        app = self._app()
        app._on_workflow_execution_panel_visibility_changed(True)
        app._on_workflow_execution_panel_visibility_changed(False)
        self.assertEqual(app.unlock_calls, 0)

    def test_collapsed_and_expanded_views_both_have_stop_control(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            widget = FloatingWidget(Path(temp_dir))
            self.assertEqual(widget.stop_workflow_button.text(), "停止")
            widget.collapse_for_workflow_execution()
            self.assertEqual(widget.workflow_running_stop_button.text(), "停止")
            widget.close()

    def test_terminal_event_restores_ui_once(self):
        app = self._app()
        app.workflow_execution_ui_state = "RUNNING_EXPANDED"
        self.assertTrue(app._restore_ui_after_workflow_execution("terminal"))
        self.assertEqual(app.widget.restore_calls, 1)
        self.assertTrue(app._restore_ui_after_workflow_execution("duplicate_terminal"))
        self.assertEqual(app.widget.restore_calls, 1)

    def test_terminal_event_does_not_collapse_then_restore_when_already_expanded(self):
        app = self._app()
        app.workflow_execution_ui_state = "RUNNING_EXPANDED"
        app._restore_ui_after_workflow_execution("finished")
        self.assertEqual(app.widget.collapse_calls, 0)
        self.assertEqual(app.widget.restore_calls, 1)

    def test_cancel_and_error_restore_ui_once(self):
        for reason in ("start_request_cancelled", "player_error"):
            app = self._app()
            app.workflow_execution_ui_state = "RUNNING_COLLAPSED"
            app._restore_ui_after_workflow_execution(reason)
            app._restore_ui_after_workflow_execution(f"duplicate_{reason}")
            self.assertEqual(app.widget.restore_calls, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
