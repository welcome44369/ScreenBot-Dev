"""No-I/O regression coverage for the workflow_start trigger capability."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from PySide6.QtWidgets import QApplication
    from app.trigger_manager import WorkflowStartTriggerDialog
except ModuleNotFoundError:
    QApplication = None
    WorkflowStartTriggerDialog = None
from app.trigger_runner import TriggerRunner
from app.trigger_store import TriggerStore
from app.script_store import ScriptStore
from app.workflow_resolver import WorkflowResolver
from app.workflow_runner import WorkflowRunner


APP = (QApplication.instance() or QApplication([])) if QApplication else None


class _NeverObserve:
    def __init__(self):
        self.observe_calls = 0

    def observe_text(self, *_args, **_kwargs):
        self.observe_calls += 1
        raise AssertionError("workflow_start must not observe OCR")

    @staticmethod
    def get_capture_runtime_diagnostics():
        return {}


class _ScriptStore:
    def __init__(self):
        self.loads = 0

    def load_script(self, filename):
        self.loads += 1
        self.filename = filename
        return {"name": "fake", "actions": []}


class _Player:
    def __init__(self):
        self.starts = []

    def is_active(self):
        return False

    def start(self, script, expected_target=None):
        self.starts.append((script, expected_target))
        return SimpleNamespace(status=SimpleNamespace(name="STARTED", value="STARTED"), reason=None)


class _RejectedGate:
    def __init__(self):
        self.calls = 0

    def authorize_foreground_input(self, _expected):
        self.calls += 1
        return SimpleNamespace(
            allowed=False,
            code=SimpleNamespace(value="TARGET_NOT_FOREGROUND"),
            snapshot=None,
        )


def workflow_start_data():
    return {
        "name": "Start only",
        "trigger": {"type": "workflow_start"},
        "macro": "fake.json",
    }


class WorkflowStartTriggerTests(unittest.TestCase):
    def test_store_accepts_start_without_ocr_and_round_trips(self):
        with tempfile.TemporaryDirectory() as root:
            store = TriggerStore(root)
            trigger_id = store.save_trigger(
                {"version": 1, "name": "Workflow Start", "type": "workflow_start"}
            )
            loaded = store.load_trigger(trigger_id)
            self.assertEqual(loaded["id"], trigger_id)
            self.assertEqual(loaded["type"], "workflow_start")
            self.assertNotIn("region", loaded)
            self.assertNotIn("poll_interval_ms", loaded)

    def test_text_schema_remains_strict_and_unknown_types_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store = TriggerStore(root)
            text = {
                "id": "text_1", "version": 1, "name": "Text", "type": "text",
                "event": "appear", "text": "ready",
                "region": {"x_ratio": 0, "y_ratio": 0, "width_ratio": 1, "height_ratio": 1},
            }
            self.assertEqual(store.validate_trigger(text)["type"], "text")
            with self.assertRaises(ValueError):
                store.validate_trigger({"id": "bad", "name": "Bad", "type": "workflow_start", "region": {}})
            with self.assertRaises(ValueError):
                store.validate_trigger({"id": "bad", "name": "Bad", "type": "unknown"})
            with self.assertRaises(ValueError):
                store.validate_trigger({"id": "bad", "name": "Bad", "type": "text", "event": "appear"})

    def test_workflow_start_dispatches_once_without_ocr_or_polling(self):
        detector, scripts, player = _NeverObserve(), _ScriptStore(), _Player()
        runner = TriggerRunner(detector, scripts, player, workflow_start_data())
        runner._run()
        runner._run()  # repeated callback/tick in the same execution is inert
        status = runner.get_status_snapshot()
        self.assertEqual(detector.observe_calls, 0)
        self.assertEqual(len(player.starts), 1)
        self.assertEqual(scripts.loads, 1)
        self.assertEqual(status["poll_count"], 0)
        self.assertEqual(status["trigger_fire_count"], 1)
        self.assertEqual(status["macro_start_count"], 1)

    def test_new_execution_can_schedule_once_again(self):
        detector, scripts, player = _NeverObserve(), _ScriptStore(), _Player()
        runner = TriggerRunner(detector, scripts, player, workflow_start_data())
        runner.start()
        runner._thread.join(timeout=1)
        runner.start()
        runner._thread.join(timeout=1)
        self.assertEqual(detector.observe_calls, 0)
        self.assertEqual(len(player.starts), 2)

    def test_gate_rejection_never_schedules_or_retries(self):
        detector, scripts, player, gate = _NeverObserve(), _ScriptStore(), _Player(), _RejectedGate()
        runner = TriggerRunner(detector, scripts, player, workflow_start_data(), input_safety_gate=gate)
        runner._run()
        self.assertEqual(gate.calls, 1)
        self.assertEqual(detector.observe_calls, 0)
        self.assertEqual(scripts.loads, 0)
        self.assertEqual(player.starts, [])
        self.assertEqual(runner.get_status_snapshot()["poll_count"], 0)

    def test_workflow_runner_accepts_start_trigger_without_text_fields(self):
        runner = WorkflowRunner(_NeverObserve(), _ScriptStore(), _Player())
        workflow = runner._validate_workflow({
            "name": "Start workflow",
            "steps": [{"id": "one", "trigger": {"type": "workflow_start"}, "macro": "fake.json"}],
        })
        self.assertEqual(workflow["steps"][0]["trigger"]["type"], "workflow_start")

    def test_resolver_preserves_workflow_start_without_ocr_fields(self):
        with tempfile.TemporaryDirectory() as root:
            triggers, scripts = TriggerStore(root), ScriptStore(root)
            trigger_id = triggers.save_trigger(
                {"version": 1, "name": "Start", "type": "workflow_start"}
            )
            scripts.save_script({"name": "Script", "version": 1, "events": []}, "script.json")
            resolved = WorkflowResolver(triggers, scripts).resolve({
                "name": "Workflow",
                "steps": [{"id": "one", "trigger_ref": trigger_id, "macro_ref": "script"}],
            })
            self.assertEqual(resolved["steps"][0]["trigger"], {"type": "workflow_start"})

    @unittest.skipUnless(WorkflowStartTriggerDialog, "PySide6 is unavailable in this Python runtime")
    def test_start_trigger_dialog_has_no_ocr_controls(self):
        with tempfile.TemporaryDirectory() as root:
            dialog = WorkflowStartTriggerDialog(TriggerStore(root))
            self.assertEqual(dialog.windowTitle(), "Workflow Start Trigger")
            self.assertFalse(hasattr(dialog, "poll"))
            self.assertFalse(hasattr(dialog, "region"))
            dialog.close()


if __name__ == "__main__":
    unittest.main()
