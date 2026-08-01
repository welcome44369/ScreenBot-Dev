"""Focused regression coverage for shutdown and trigger editor flow."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication

from app.application import ScreenBotApp
from app.state import AppState
from app.trigger_conditions import CONDITION_CHOICES, LEGACY_APPEAR, LEGACY_DISAPPEAR
from app.trigger_manager import TriggerManager
from app.trigger_store import TriggerStore
from app.trigger_wizard import TriggerWizard
from app.workflow_resolver import WorkflowResolver


class TriggerWizardFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_create_ocr_trigger_save_reload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            wizard = TriggerWizard(
                store,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                parent=None,
            )
            wizard.name_edit.setText("New OCR trigger")
            wizard.event.setCurrentText("disappear")
            wizard.region = {
                "x_ratio": 0.1,
                "y_ratio": 0.2,
                "width_ratio": 0.3,
                "height_ratio": 0.4,
            }
            wizard.candidates.clear()
            wizard._add_candidate({"source": "ocr", "text": "alpha", "original_text": "alpha"}, checked=True)
            wizard.save()
            triggers = store.list_triggers()
            self.assertEqual(len(triggers), 1)
            saved = store.load_trigger(triggers[0]["id"])
            self.assertEqual(saved["name"], "New OCR trigger")
            self.assertEqual(saved["event"], "disappear")
            self.assertEqual(saved["texts"], ["alpha"])
            wizard.close()

    def test_trigger_wizard_updates_existing_trigger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            trigger_id = store.save_trigger(
                {
                    "version": 1,
                    "name": "Existing trigger",
                    "type": "text",
                    "event": "appear",
                    "texts": ["old text"],
                    "match_mode": "any",
                    "region": {"x_ratio": 0.1, "y_ratio": 0.2, "width_ratio": 0.3, "height_ratio": 0.4},
                    "poll_interval_ms": 500,
                    "confirm_frames": 2,
                    "cooldown_ms": 0,
                }
            )
            wizard = TriggerWizard(
                store,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                parent=None,
                data=store.load_trigger(trigger_id),
            )
            self.assertEqual(wizard.name_edit.text(), "Existing trigger")
            self.assertEqual(wizard.event.currentData(), "edge_present")
            self.assertEqual(wizard.candidates.count(), 1)
            wizard.name_edit.setText("Updated trigger")
            wizard.region = {
                "x_ratio": 0.2,
                "y_ratio": 0.3,
                "width_ratio": 0.4,
                "height_ratio": 0.5,
            }
            wizard.candidates.clear()
            wizard._add_candidate({"source": "existing", "text": "new text", "original_text": "new text"}, checked=True)
            wizard.save()
            updated = store.load_trigger(trigger_id)
            self.assertEqual(updated["name"], "Updated trigger")
            self.assertEqual(updated["texts"], ["new text"])
            self.assertEqual(updated["region"]["x_ratio"], 0.2)
            wizard.close()

    def test_ocr_condition_dropdown_contains_all_canonical_conditions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            wizard = TriggerWizard(
                store,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                parent=None,
            )
            expected_codes = [code for code, _label, _help, _mode, _desired_state in CONDITION_CHOICES]
            actual_codes = [wizard.event.itemData(index) for index in range(wizard.event.count())]
            self.assertEqual(actual_codes, expected_codes)
            self.assertNotIn("workflow_start", actual_codes)
            self.assertNotIn(LEGACY_APPEAR, actual_codes)
            self.assertNotIn(LEGACY_DISAPPEAR, actual_codes)
            wizard.close()

    def test_real_ocr_wizard_event_combo_has_six_items(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            manager = TriggerManager(
                store,
                workflow_store=None,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                on_select_start=None,
                on_select_end=None,
            )
            wizard = manager._open_ocr_wizard()
            self.assertEqual(wizard.event.count(), 6)
            wizard.close()

    def test_real_ocr_wizard_event_combo_texts_match_canonical_labels(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            manager = TriggerManager(
                store,
                workflow_store=None,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                on_select_start=None,
                on_select_end=None,
            )
            wizard = manager._open_ocr_wizard()
            expected_labels = [label for _code, label, _help, _mode, _desired_state in CONDITION_CHOICES]
            actual_labels = [wizard.event.itemText(index) for index in range(wizard.event.count())]
            self.assertEqual(actual_labels, expected_labels)
            wizard.close()

    def test_real_ocr_wizard_event_combo_data_match_canonical_codes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            manager = TriggerManager(
                store,
                workflow_store=None,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                on_select_start=None,
                on_select_end=None,
            )
            wizard = manager._open_ocr_wizard()
            actual_codes = [wizard.event.itemData(index) for index in range(wizard.event.count())]
            self.assertEqual(actual_codes, [code for code, _label, _help, _mode, _desired_state in CONDITION_CHOICES])
            wizard.close()

    def test_real_ocr_wizard_has_no_appear_disappear_raw_items(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            manager = TriggerManager(
                store,
                workflow_store=None,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                on_select_start=None,
                on_select_end=None,
            )
            wizard = manager._open_ocr_wizard()
            self.assertNotIn("appear", [wizard.event.itemText(index) for index in range(wizard.event.count())])
            self.assertNotIn("disappear", [wizard.event.itemText(index) for index in range(wizard.event.count())])
            wizard.close()

    def test_new_and_edit_modes_use_same_condition_combo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            create_wizard = TriggerWizard(
                store,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                parent=None,
            )
            self.assertEqual(create_wizard.event.count(), 6)
            trigger_id = store.save_trigger(
                {
                    "version": 1,
                    "name": "Edit trigger",
                    "type": "text",
                    "event": "appear",
                    "condition": {"mode": "edge", "desired_state": "present"},
                    "texts": ["alpha"],
                    "match_mode": "any",
                    "region": {"x_ratio": 0.1, "y_ratio": 0.2, "width_ratio": 0.3, "height_ratio": 0.4},
                    "poll_interval_ms": 500,
                    "confirm_frames": 2,
                    "cooldown_ms": 0,
                }
            )
            edit_wizard = TriggerWizard(
                store,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                parent=None,
                data=store.load_trigger(trigger_id),
            )
            self.assertEqual(edit_wizard.event.count(), 6)
            self.assertEqual(edit_wizard.event.currentData(), "edge_present")
            create_wizard.close()
            edit_wizard.close()

    def test_legacy_appear_selects_edge_present(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            wizard = TriggerWizard(
                store,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                parent=None,
                data={
                    "id": "legacy_appear",
                    "name": "Legacy appear",
                    "type": "text",
                    "event": "appear",
                    "texts": ["alpha"],
                    "match_mode": "any",
                    "region": {"x_ratio": 0.1, "y_ratio": 0.2, "width_ratio": 0.3, "height_ratio": 0.4},
                    "poll_interval_ms": 500,
                    "confirm_frames": 2,
                    "cooldown_ms": 0,
                },
            )
            self.assertEqual(wizard.event.currentData(), "edge_present")
            wizard.close()

    def test_legacy_disappear_selects_edge_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            wizard = TriggerWizard(
                store,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                parent=None,
                data={
                    "id": "legacy_disappear",
                    "name": "Legacy disappear",
                    "type": "text",
                    "event": "disappear",
                    "texts": ["alpha"],
                    "match_mode": "any",
                    "region": {"x_ratio": 0.1, "y_ratio": 0.2, "width_ratio": 0.3, "height_ratio": 0.4},
                    "poll_interval_ms": 500,
                    "confirm_frames": 2,
                    "cooldown_ms": 0,
                },
            )
            self.assertEqual(wizard.event.currentData(), "edge_absent")
            wizard.close()

    def test_workflow_start_is_not_in_ocr_condition_dropdown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            wizard = TriggerWizard(
                store,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                parent=None,
            )
            self.assertNotIn("workflow_start", [wizard.event.itemData(index) for index in range(wizard.event.count())])
            wizard.close()

    def test_each_condition_create_save_reload_round_trip(self):
        for code, _label, _help, mode, desired_state in CONDITION_CHOICES:
            with tempfile.TemporaryDirectory() as temp_dir:
                store = TriggerStore(temp_dir)
                wizard = TriggerWizard(
                    store,
                    window_tracker=SimpleNamespace(target=None),
                    text_detector=SimpleNamespace(),
                    parent=None,
                )
                wizard.name_edit.setText(f"Condition {code}")
                for index in range(wizard.event.count()):
                    if wizard.event.itemData(index) == code:
                        wizard.event.setCurrentIndex(index)
                        break
                wizard.region = {
                    "x_ratio": 0.1,
                    "y_ratio": 0.2,
                    "width_ratio": 0.3,
                    "height_ratio": 0.4,
                }
                wizard.candidates.clear()
                wizard._add_candidate({"source": "ocr", "text": "alpha", "original_text": "alpha"}, checked=True)
                wizard.save()
                trigger_id = store.list_triggers()[0]["id"]
                saved = store.load_trigger(trigger_id)
                self.assertEqual(saved["condition"]["mode"], mode)
                self.assertEqual(saved["condition"]["desired_state"], desired_state)
                self.assertEqual(saved["event"], "appear" if desired_state == "present" else "disappear")
                wizard.close()

    def test_existing_appear_trigger_maps_to_expected_condition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            wizard = TriggerWizard(
                store,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                parent=None,
                data={
                    "id": "legacy_appear",
                    "name": "Legacy appear",
                    "type": "text",
                    "event": "appear",
                    "texts": ["alpha"],
                    "match_mode": "any",
                    "region": {"x_ratio": 0.1, "y_ratio": 0.2, "width_ratio": 0.3, "height_ratio": 0.4},
                    "poll_interval_ms": 500,
                    "confirm_frames": 2,
                    "cooldown_ms": 0,
                },
            )
            self.assertEqual(wizard.event.currentData(), "edge_present")
            wizard.close()

    def test_existing_disappear_trigger_maps_to_expected_condition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            wizard = TriggerWizard(
                store,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                parent=None,
                data={
                    "id": "legacy_disappear",
                    "name": "Legacy disappear",
                    "type": "text",
                    "event": "disappear",
                    "texts": ["alpha"],
                    "match_mode": "any",
                    "region": {"x_ratio": 0.1, "y_ratio": 0.2, "width_ratio": 0.3, "height_ratio": 0.4},
                    "poll_interval_ms": 500,
                    "confirm_frames": 2,
                    "cooldown_ms": 0,
                },
            )
            self.assertEqual(wizard.event.currentData(), "edge_absent")
            wizard.close()

    def test_edit_existing_trigger_restores_selected_condition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            trigger_id = store.save_trigger(
                {
                    "version": 1,
                    "name": "State trigger",
                    "type": "text",
                    "event": "disappear",
                    "condition": {"mode": "state", "desired_state": "absent"},
                    "texts": ["alpha"],
                    "match_mode": "any",
                    "region": {"x_ratio": 0.1, "y_ratio": 0.2, "width_ratio": 0.3, "height_ratio": 0.4},
                    "poll_interval_ms": 500,
                    "confirm_frames": 2,
                    "cooldown_ms": 0,
                }
            )
            wizard = TriggerWizard(
                store,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                parent=None,
                data=store.load_trigger(trigger_id),
            )
            self.assertEqual(wizard.event.currentData(), "state_absent")
            for index in range(wizard.event.count()):
                if wizard.event.itemData(index) == "initial_present":
                    wizard.event.setCurrentIndex(index)
                    break
            wizard.name_edit.setText("Edited trigger")
            wizard.region = {
                "x_ratio": 0.2,
                "y_ratio": 0.3,
                "width_ratio": 0.4,
                "height_ratio": 0.5,
            }
            wizard.candidates.clear()
            wizard._add_candidate({"source": "existing", "text": "beta", "original_text": "beta"}, checked=True)
            wizard.save()
            updated = store.load_trigger(trigger_id)
            self.assertEqual(updated["condition"], {"mode": "initial", "desired_state": "present"})
            self.assertEqual(updated["event"], "appear")
            wizard.close()

    def test_resolved_trigger_reference_preserves_condition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            trigger_id = store.save_trigger(
                {
                    "version": 1,
                    "name": "Resolver trigger",
                    "type": "text",
                    "event": "appear",
                    "condition": {"mode": "edge", "desired_state": "present"},
                    "texts": ["alpha"],
                    "match_mode": "any",
                    "region": {"x_ratio": 0.1, "y_ratio": 0.2, "width_ratio": 0.3, "height_ratio": 0.4},
                    "poll_interval_ms": 500,
                    "confirm_frames": 2,
                    "cooldown_ms": 0,
                }
            )
            workflow = {
                "name": "workflow",
                "steps": [{"id": "step-1", "trigger_ref": trigger_id, "macro": "test.json"}],
                "loop": {"stop_trigger_ref": trigger_id},
            }
            resolver = WorkflowResolver(store, SimpleNamespace(load_script=lambda _filename: None))
            resolved = resolver.resolve(workflow)
            self.assertEqual(resolved["steps"][0]["trigger"]["condition"], {"mode": "edge", "desired_state": "present"})
            self.assertEqual(resolved["loop"]["stop_trigger"]["condition"], {"mode": "edge", "desired_state": "present"})


class TriggerManagerFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_add_button_opens_ocr_wizard_directly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            manager = TriggerManager(
                store,
                workflow_store=None,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                on_select_start=None,
                on_select_end=None,
            )
            created = []

            class FakeWizard:
                def __init__(self, *args, **kwargs):
                    created.append(kwargs.get("data"))
                    self.finished = SimpleNamespace(connect=lambda *args, **kwargs: None)

                def setWindowModality(self, modality):
                    pass

                def show(self):
                    pass

                def raise_(self):
                    pass

                def activateWindow(self):
                    pass

            with patch("app.trigger_manager.TriggerWizard", FakeWizard):
                with patch("app.trigger_manager.QInputDialog.getItem", side_effect=AssertionError("type selector should not be used")):
                    manager._new()
            self.assertEqual(len(created), 1)
            self.assertIsNone(created[0])

    def test_trigger_manager_primary_add_opens_real_ocr_wizard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            manager = TriggerManager(
                store,
                workflow_store=None,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                on_select_start=None,
                on_select_end=None,
            )
            wizard = manager._open_ocr_wizard()
            self.assertIsInstance(wizard, TriggerWizard)
            self.assertEqual(wizard.event.count(), 6)
            self.assertEqual(
                [wizard.event.itemData(index) for index in range(wizard.event.count())],
                [code for code, _label, _help, _mode, _desired_state in CONDITION_CHOICES],
            )
            self.assertNotIn("workflow_start", [wizard.event.itemData(index) for index in range(wizard.event.count())])
            self.assertNotIn(LEGACY_APPEAR, [wizard.event.itemData(index) for index in range(wizard.event.count())])
            self.assertNotIn(LEGACY_DISAPPEAR, [wizard.event.itemData(index) for index in range(wizard.event.count())])
            wizard.close()

    def test_add_button_does_not_open_type_selector(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            manager = TriggerManager(
                store,
                workflow_store=None,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                on_select_start=None,
                on_select_end=None,
            )

            class FakeWizard:
                def __init__(self, *args, **kwargs):
                    self.finished = SimpleNamespace(connect=lambda *args, **kwargs: None)

                def setWindowModality(self, modality):
                    pass

                def show(self):
                    pass

                def raise_(self):
                    pass

                def activateWindow(self):
                    pass

            with patch("app.trigger_manager.TriggerWizard", FakeWizard):
                with patch("app.trigger_manager.QInputDialog.getItem", side_effect=AssertionError("type selector should not be used")):
                    manager._new()

    def test_workflow_start_remains_available_through_secondary_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TriggerStore(temp_dir)
            manager = TriggerManager(
                store,
                workflow_store=None,
                window_tracker=SimpleNamespace(target=None),
                text_detector=SimpleNamespace(),
                on_select_start=None,
                on_select_end=None,
            )
            created = []

            class FakeDialog:
                def __init__(self, *args, **kwargs):
                    created.append(kwargs.get("data"))
                    self.finished = SimpleNamespace(connect=lambda callback: None)

                def exec(self):
                    return None

            with patch("app.trigger_manager.WorkflowStartTriggerDialog", FakeDialog):
                manager._new_workflow_start()
            self.assertEqual(len(created), 1)
            self.assertIsNone(created[0])


class ShutdownLifecycleTests(unittest.TestCase):
    def test_request_shutdown_is_idempotent(self):
        app = ScreenBotApp.__new__(ScreenBotApp)
        app._shutdown_requested = False
        app._shutdown_completed = False
        app._shutdown_reason = None
        app.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
        app.widget = SimpleNamespace(
            _suppress_exit_requests=False,
            close=lambda: None,
            pos=lambda: SimpleNamespace(x=lambda: 0, y=lambda: 0),
        )
        app.countdown_timer = SimpleNamespace(stop=lambda: None)
        app.start_handoff_timer = SimpleNamespace(stop=lambda: None)
        app.workflow_ui_timer = SimpleNamespace(stop=lambda: None)
        app._cancel_armed_start = lambda *_args, **_kwargs: False
        app.stop_workflow = lambda: None
        app.stop_text_trigger = lambda: None
        app.state = AppState.IDLE
        app.recorder_overlay = None
        app.player = SimpleNamespace(is_active=lambda: False, stop=lambda: None)
        app.text_detector = SimpleNamespace(close=lambda: None)
        app.target_relative_overlay = None
        app.target_session = None
        app._unregister_hotkeys = lambda: None
        app.settings = SimpleNamespace(set=lambda *_args, **_kwargs: None)
        app._workflow_editor = None
        app._workflow_debug_panel = None
        app.overlay_diagnostics = None
        app.ocr_process_diagnostics = None
        app.workflow_process_diagnostics = None
        app.app = SimpleNamespace(quit=lambda: None)
        app.recorder = SimpleNamespace(stop=lambda: None)
        app.request_shutdown = ScreenBotApp.request_shutdown.__get__(app, ScreenBotApp)
        app.shutdown = ScreenBotApp.shutdown.__get__(app, ScreenBotApp)

        self.assertTrue(app.request_shutdown("unit_test"))
        self.assertFalse(app.request_shutdown("unit_test_again"))
        self.assertTrue(app._shutdown_completed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
