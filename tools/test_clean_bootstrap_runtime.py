"""Isolated Store/Resolver coverage for the A.1.5b clean bootstrap shape."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.script_store import ScriptStore
from app.trigger_store import TriggerStore
from app.workflow_resolver import WorkflowResolver
from app.workflow_store import WorkflowStore


def safe_click_script():
    return {
        "name": "A15 Safe Single Click",
        "version": 1,
        "created_at": "2026-07-30T00:00:00Z",
        "target_window": {},
        "scan_interval": 0,
        "metadata": {},
        "actions": [{
            "type": "mouse_click",
            "button": "left",
            "ratio_x": 0.5,
            "ratio_y": 0.5,
        }],
    }


class CleanBootstrapRuntimeTests(unittest.TestCase):
    def _create(self, root):
        scripts = ScriptStore(root)
        triggers = TriggerStore(root)
        workflows = WorkflowStore(root)
        script_filename = scripts.save_script(safe_click_script())
        script = scripts.load_script(script_filename)
        trigger_id = triggers.save_trigger({
            "version": 1,
            "name": "A15 Workflow Start",
            "type": "workflow_start",
        })
        workflow_filename = workflows.save_workflow({
            "version": 1,
            "name": "A15 Clean Bootstrap",
            "steps": [{
                "id": "step_1",
                "trigger_ref": trigger_id,
                "macro_ref": script["id"],
            }],
        })
        return scripts, triggers, workflows, script_filename, trigger_id, workflow_filename

    def test_create_save_reload_and_resolve(self):
        with tempfile.TemporaryDirectory() as root:
            scripts, triggers, workflows, script_filename, trigger_id, workflow_filename = self._create(root)
            # Recreate each loader to prove persistence rather than memory reuse.
            script = ScriptStore(root).load_script(script_filename)
            trigger = TriggerStore(root).load_trigger(trigger_id)
            workflow = WorkflowStore(root).load_workflow(workflow_filename)
            resolved = WorkflowResolver(TriggerStore(root), ScriptStore(root)).resolve(workflow)

            self.assertRegex(script["id"], r"^script_[0-9a-f]{32}$")
            self.assertEqual(script["name"], "A15 Safe Single Click")
            self.assertEqual(script["actions"], safe_click_script()["actions"])
            self.assertEqual(trigger["type"], "workflow_start")
            self.assertNotIn("region", trigger)
            self.assertNotIn("poll_interval_ms", trigger)
            self.assertRegex(workflow["id"], r"^workflow_[0-9a-f]{32}$")
            self.assertEqual(len(workflow["steps"]), 1)
            self.assertEqual(workflow["steps"][0]["trigger_ref"], trigger_id)
            self.assertEqual(workflow["steps"][0]["macro_ref"], script["id"])
            self.assertEqual(resolved["steps"][0]["trigger"], {"type": "workflow_start"})
            self.assertEqual(resolved["steps"][0]["macro"], script_filename)

    def test_invalid_references_and_payloads_are_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as root:
            scripts, triggers, _workflows, _script_filename, trigger_id, _workflow_filename = self._create(root)
            resolver = WorkflowResolver(triggers, scripts)
            with self.assertRaises(FileNotFoundError):
                resolver.resolve({"name": "Missing trigger", "steps": [{
                    "id": "one", "trigger_ref": "trigger_missing", "macro_ref": "script_missing",
                }]})
            with self.assertRaises(FileNotFoundError):
                resolver.resolve({"name": "Missing script", "steps": [{
                    "id": "one", "trigger_ref": trigger_id, "macro_ref": "script_missing",
                }]})
            with self.assertRaises(FileNotFoundError):
                resolver.resolve({"name": "Wrong types", "steps": [{
                    "id": "one", "trigger_ref": "script_missing", "macro_ref": trigger_id,
                }]})
            with self.assertRaises(ValueError):
                triggers.validate_trigger({
                    "id": "trigger_bad", "name": "Bad", "type": "workflow_start", "poll_interval_ms": 1,
                })

    def test_relative_click_rejects_invalid_or_desktop_coordinates(self):
        with tempfile.TemporaryDirectory() as root:
            store = ScriptStore(root)
            invalid = safe_click_script()
            invalid["actions"][0]["ratio_x"] = 1.1
            with self.assertRaises(ValueError):
                store.save_script(invalid)
            desktop = safe_click_script()
            desktop["actions"][0]["screen_x"] = 500
            with self.assertRaises(ValueError):
                store.save_script(desktop)


if __name__ == "__main__":
    unittest.main()
