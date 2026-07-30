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
from app.workflow_store import WorkflowStore
from app.player import PlaybackStatus
from app.observation_engine import ObservationResult


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
        self.last_result = None

    def is_active(self):
        return False

    def start(self, script, expected_target=None):
        self.starts.append((script, expected_target))
        self.last_result = SimpleNamespace(status=PlaybackStatus.COMPLETED, duration_ms=0)
        return SimpleNamespace(status=SimpleNamespace(name="STARTED", value="STARTED"), reason=None)

    def get_last_result(self):
        return self.last_result


class _IntegrationDetector(_NeverObserve):
    def invalidate_capture_target(self):
        pass


class _PresentDetector:
    def __init__(self):
        self.observe_calls = 0

    def observe_text(self, *_args, **_kwargs):
        self.observe_calls += 1
        return ObservationResult(
            state="PRESENT", exact_match=True, text_similarity=1.0,
            readability_score=1.0, visual_similarity=None, presence_score=1.0,
            observation_valid=True, reason="exact_match", recognized_text="ready",
            target_text="ready",
        )

    @staticmethod
    def get_capture_runtime_diagnostics():
        return {}


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
        self.assertNotIn("min_absent_duration_ms", workflow["steps"][0]["trigger"])

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

    def test_full_store_resolver_runner_start_path_preserves_trigger_type_boundary(self):
        """Exercise the same Store -> Resolver -> Runner path as UI Start, without input."""
        with tempfile.TemporaryDirectory() as root:
            scripts, triggers, workflows = ScriptStore(root), TriggerStore(root), WorkflowStore(root)
            script_filename = scripts.save_script({"name": "Safe fake", "version": 1, "events": []})
            trigger_id = triggers.save_trigger({
                "version": 1, "name": "Start", "type": "workflow_start",
            })
            workflow_filename = workflows.save_workflow({
                "version": 1,
                "name": "Start workflow",
                "steps": [{"id": "one", "trigger_ref": trigger_id, "macro_ref": script_filename[:-5]}],
            })
            resolved = WorkflowResolver(triggers, scripts).resolve(workflows.load_workflow(workflow_filename))
            self.assertNotIn("min_absent_duration_ms", resolved["steps"][0]["trigger"])

            player = _Player()
            runner = WorkflowRunner(_IntegrationDetector(), scripts, player)
            runner.load_workflow(resolved)
            self.assertNotIn("min_absent_duration_ms", runner.workflow["steps"][0]["trigger"])
            runner.start()
            runner._thread.join(timeout=2)

            self.assertFalse(runner.is_active())
            self.assertEqual(runner.last_step_start_error, None)
            self.assertEqual(runner.state.name, "FINISHED")
            self.assertEqual(len(player.starts), 1)

    def test_text_resolver_and_runner_preserve_min_absent_duration(self):
        with tempfile.TemporaryDirectory() as root:
            triggers, scripts = TriggerStore(root), ScriptStore(root)
            trigger_id = triggers.save_trigger({
                "version": 1, "name": "Text", "type": "text", "event": "appear", "text": "ready",
                "region": {"x_ratio": 0, "y_ratio": 0, "width_ratio": 1, "height_ratio": 1},
                "min_absent_duration_ms": 321,
            })
            scripts.save_script({"name": "Safe fake", "version": 1, "events": []}, "fake.json")
            resolved = WorkflowResolver(triggers, scripts).resolve({
                "name": "Text workflow",
                "steps": [{"id": "one", "trigger_ref": trigger_id, "macro_ref": "fake"}],
            })
            self.assertEqual(resolved["steps"][0]["trigger"]["min_absent_duration_ms"], 321)
            runner = WorkflowRunner(_IntegrationDetector(), scripts, _Player())
            normalized = runner._validate_workflow(resolved)
            self.assertEqual(normalized["steps"][0]["trigger"]["min_absent_duration_ms"], 321)

    def test_text_trigger_runner_keeps_polling_and_min_absent_duration(self):
        detector, player = _PresentDetector(), _Player()
        runner = TriggerRunner(detector, _ScriptStore(), player, {
            "name": "Text polling",
            "trigger": {
                "type": "text", "event": "appear", "text": "ready",
                "region": {"x_ratio": 0, "y_ratio": 0, "width_ratio": 1, "height_ratio": 1},
                "poll_interval_ms": 1, "confirm_frames": 1, "cooldown_ms": 0,
                "min_absent_duration_ms": 321,
            },
            "macro": "fake.json",
        })
        runner._run()
        status = runner.get_status_snapshot()
        self.assertEqual(runner.text_trigger.min_absent_duration_ms, 321)
        self.assertEqual(detector.observe_calls, 1)
        self.assertEqual(status["poll_count"], 1)
        self.assertEqual(status["trigger_fire_count"], 1)
        self.assertEqual(len(player.starts), 1)

    def test_workflow_start_rejects_all_text_only_fields(self):
        for field, value in {
            "min_absent_duration_ms": 1,
            "text": "ready",
            "region": {},
            "poll_interval_ms": 1,
        }.items():
            with self.subTest(field=field):
                payload = workflow_start_data()
                payload["trigger"][field] = value
                with self.assertRaises(ValueError):
                    TriggerRunner(_NeverObserve(), _ScriptStore(), _Player(), payload)

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
