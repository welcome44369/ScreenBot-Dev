"""Focused UI regressions for clearing a frozen TargetSession."""
from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from app.application import ScreenBotApp
from app.floating_widget import FloatingWidget
from app.state import AppState
from tools.test_target_relative_overlay import CoordinatorHarness


APP = QApplication.instance() or QApplication([])


class _Session:
    def __init__(self, events):
        self.bound = True
        self.events = events

    def has_target(self):
        return self.bound

    def get_snapshot(self):
        return None

    def clear(self, reason):
        self.events.append(("clear", reason))
        self.bound = False


class _Workflow:
    def __init__(self, active=False):
        self.active = active
        self.last_error = None

    def is_active(self):
        return self.active

    def get_runtime_snapshot(self):
        return {"state": "IDLE"}


class _Widget:
    def __init__(self, events, expanded=True):
        self.events = events
        self.visible = True
        self.position = (240, 180)
        self.presentation = {
            "expanded": bool(expanded),
            "runtime_debug_expanded": False,
        }
        self.workflow_running = True
        self.runtime_info = {}

    def capture_control_presentation(self):
        self.events.append(("capture", self.position))
        return dict(self.presentation)

    def set_workflow_running(self, running):
        self.workflow_running = bool(running)

    def set_workflow_runtime_info(self, info):
        self.runtime_info = dict(info)

    def restore_control_presentation(self, presentation):
        self.events.append(("restore", dict(presentation)))
        self.presentation = dict(presentation)

    def ensure_self_managed_visible(self):
        self.events.append(("visible", self.position))
        self.visible = True


def _make_app(*, workflow=False, state=AppState.IDLE, expanded=True):
    events = []
    app = ScreenBotApp.__new__(ScreenBotApp)
    app.logger = logging.getLogger("test.target.clear.ui")
    app.target_session = _Session(events)
    app.workflow_runner = _Workflow(workflow)
    app.widget = _Widget(events, expanded=expanded)
    app.state = state
    app.workflow_ui_state = "WAIT_TRIGGER" if workflow else "IDLE"
    app._refresh_compact_presentation = lambda: events.append(("refresh", None))
    return app, events


class TargetClearUiStateTests(unittest.TestCase):
    def test_target_clear_releases_overlay_suppression(self):
        harness = CoordinatorHarness()
        harness.start_and_reconcile()
        harness.target_session.publish("TARGET_SESSION_CLEARED", harness.snapshot)
        self.assertIsNone(harness.coordinator._binding)
        self.assertFalse(harness.widget.suppression[-1])

    def test_idle_unlock_keeps_widget_visible_and_position(self):
        app, events = _make_app()
        before = app.widget.position
        self.assertTrue(app._f8_stop_then_unlock())
        self.assertTrue(app.widget.visible)
        self.assertEqual(before, app.widget.position)
        self.assertLess(events.index(("clear", "f8_unlock")), events.index(("visible", before)))

    def test_unlock_preserves_expanded_presentation(self):
        app, _events = _make_app(expanded=True)
        app._f8_stop_then_unlock()
        self.assertTrue(app.widget.presentation["expanded"])

    def test_unlock_preserves_compact_presentation(self):
        app, _events = _make_app(expanded=False)
        app._f8_stop_then_unlock()
        self.assertFalse(app.widget.presentation["expanded"])

    def test_workflow_stops_before_clear_and_finishes_visible(self):
        app, events = _make_app(workflow=True, expanded=False)

        def stop():
            events.append(("stop", "workflow"))
            app.workflow_runner.active = False

        app.stop_workflow = stop
        self.assertTrue(app._f8_stop_then_unlock())
        self.assertLess(events.index(("stop", "workflow")), events.index(("clear", "f8_unlock")))
        self.assertTrue(app.widget.visible)
        self.assertEqual(app.workflow_ui_state, "IDLE")

    def test_script_stops_before_clear_and_finishes_visible(self):
        app, events = _make_app(state=AppState.RUNNING)
        player = SimpleNamespace(active=True)
        player.is_active = lambda: player.active
        app.player = player

        def stop():
            events.append(("stop", "script"))
            player.active = False
            app.state = AppState.IDLE

        app.stop_script = stop
        self.assertTrue(app._f8_stop_then_unlock())
        self.assertLess(events.index(("stop", "script")), events.index(("clear", "f8_unlock")))
        self.assertTrue(app.widget.visible)

    def test_recorder_stops_before_clear_and_finishes_visible(self):
        app, events = _make_app(state=AppState.RECORDING)
        recorder = SimpleNamespace(active=True)
        recorder.is_recording = lambda: recorder.active
        app.recorder = recorder

        def stop():
            events.append(("stop", "recorder"))
            recorder.active = False
            app.state = AppState.IDLE

        app.stop_recording = stop
        self.assertTrue(app._f8_stop_then_unlock())
        self.assertLess(events.index(("stop", "recorder")), events.index(("clear", "f8_unlock")))
        self.assertTrue(app.widget.visible)

    def test_refresh_sets_idle_runtime_and_disables_stop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            widget = FloatingWidget(Path(temp_dir))
            app, _events = _make_app()
            app.widget = widget
            app.target_session.bound = False
            app._recorded_event_count = 0
            app.window_tracker = SimpleNamespace(target=None)
            app.start_handoff = None
            app.current_script = None
            app._refresh_compact_presentation = (
                lambda: ScreenBotApp._refresh_compact_presentation(app)
            )
            app.refresh_ui_from_runtime_state()
            self.assertEqual(widget.target_label.text(), "目標：未鎖定")
            self.assertIn("未鎖定", widget.state_text_label.text())
            self.assertFalse(widget.stop_workflow_button.isEnabled())
            self.assertEqual(widget.workflow_status_label.text(), "Workflow 狀態：IDLE")
            widget.close()

    def test_real_widget_visibility_position_and_modes_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for expanded in (True, False):
                widget = FloatingWidget(Path(temp_dir))
                widget.show()
                widget.move(QPoint(310, 210))
                if widget.expanded != expanded:
                    widget.toggle_panel()
                APP.processEvents()
                before = widget.pos()
                presentation = widget.capture_control_presentation()
                widget.set_target_visibility_suppressed(True)
                self.assertFalse(widget.isVisible())
                widget.restore_control_presentation(presentation)
                widget.ensure_self_managed_visible()
                APP.processEvents()
                self.assertTrue(widget.isVisible())
                self.assertEqual(widget.pos(), before)
                self.assertEqual(widget.expanded, expanded)
                widget.close()

    def test_duplicate_refresh_does_not_move_or_change_presentation(self):
        app, _events = _make_app(expanded=False)
        before = (app.widget.position, dict(app.widget.presentation))
        app._f8_stop_then_unlock()
        app.refresh_ui_from_runtime_state(presentation=app.widget.presentation)
        self.assertEqual(
            (app.widget.position, app.widget.presentation),
            before,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
