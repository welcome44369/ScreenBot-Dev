import json
import os
import tempfile
import time
import unittest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.runtime_run_log import RuntimeRunLogManager
from app.workflow_diagnostics import WorkflowDiagnostics


class RuntimeRunLogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _start_and_finish(self, manager, name="採集小麥"):
        manager.start({"workflow_id": "workflow_test", "workflow_name": name, "steps_total": 1})
        manager.record("Macro", "MACRO_STARTED", step_id="step_1", cycle=1)
        manager.record("Macro", "MACRO_FINISHED", step_id="step_1", cycle=1, data={"duration_ms": 17})
        manager.record("Cycle", "CYCLE_COMPLETED", step_id="step_1", cycle=1)
        manager.finalize("FINISHED", {
            "workflow_state": "FINISHED", "finish_reason": "workflow_completed",
            "completed_cycles": 1, "trigger_count": 1, "macro_count": 1,
            "poll_count": 1001, "last_error": None,
        })

    def test_one_run_has_isolated_structured_start_and_end(self):
        manager = RuntimeRunLogManager(self.root, retention_count=30, background_maintenance=False)
        self._start_and_finish(manager)
        logs = list((self.root / "logs" / "runtime").glob("*.log"))
        jsonls = list((self.root / "logs" / "runtime").glob("*.jsonl"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(len(jsonls), 1)
        text = logs[0].read_text(encoding="utf-8")
        self.assertIn("=== WORKFLOW RUN START ===", text)
        self.assertIn("=== WORKFLOW RUN END ===", text)
        events = [json.loads(line) for line in jsonls[0].read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(len({event["run_id"] for event in events}), 1)
        names = [event["event"] for event in events]
        self.assertLess(names.index("MACRO_STARTED"), names.index("MACRO_FINISHED"))
        self.assertLess(names.index("MACRO_FINISHED"), names.index("CYCLE_COMPLETED"))
        self.assertLess(names.index("CYCLE_COMPLETED"), names.index("WORKFLOW_FINISHED"))
        self.assertLess(names.index("WORKFLOW_FINISHED"), names.index("RUN_FINISHED"))

    def test_retention_deletes_complete_run_groups_only(self):
        manager = RuntimeRunLogManager(self.root, retention_count=30, background_maintenance=False)
        runtime_dir = self.root / "logs" / "runtime"
        for index in range(31):
            self._start_and_finish(manager, f"workflow-{index}")
            time.sleep(0.002)
        (runtime_dir / "notes.txt").write_text("keep", encoding="utf-8")
        (runtime_dir / "manual_backup.log").write_text("keep", encoding="utf-8")
        manager.run_retention_sync()
        jsonls = list(runtime_dir.glob("runtime_*_run_*.jsonl"))
        logs = list(runtime_dir.glob("runtime_*_run_*.log"))
        self.assertEqual(len(jsonls), 30)
        self.assertEqual(len(logs), 30)
        self.assertTrue((runtime_dir / "notes.txt").exists())
        self.assertTrue((runtime_dir / "manual_backup.log").exists())
        for jsonl_path in jsonls:
            events = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
            run_id = events[0]["run_id"]
            self.assertEqual(events[-1]["event"], "RUN_FINISHED")
            self.assertEqual(len(list(runtime_dir.glob(f"*{run_id}.log"))), 1)

    def test_active_run_is_never_retention_candidate(self):
        manager = RuntimeRunLogManager(self.root, retention_count=1, background_maintenance=False)
        self._start_and_finish(manager, "old")
        active = manager.start({"workflow_id": "workflow_active", "workflow_name": "active"})
        manager.run_retention_sync()
        self.assertTrue(active.log_path.exists())
        self.assertTrue(active.jsonl_path.exists())
        self.assertFalse(active.finalized)
        manager.finalize("STOPPED", {"workflow_state": "STOPPED", "finish_reason": "test_cleanup"})

    def test_diagnostics_finalizes_terminal_snapshot_after_macro(self):
        diagnostics = WorkflowDiagnostics(self.root, runtime_log_background_maintenance=False)
        workflow = {"id": "workflow_test", "name": "workflow", "steps": [{"id": "step_1"}]}
        diagnostics.mark_workflow_started(workflow)
        base = {
            "workflow_name": "workflow", "step_id": "step_1", "step_number": 1, "total_steps": 1,
            "trigger_event": "appear", "trigger_text": "target", "macro": "macro.json",
            "loop_mode": None, "restart_step": None, "max_cycles": None, "current_cycle": 1,
            "pending_stop": False, "stop_trigger_runtime": {}, "error": None, "finish_reason": None,
        }
        diagnostics.update_from_snapshot({**base, "state": "RUNNING_MACRO", "completed_cycles": 0,
                                          "trigger_runtime": {"macro_running": True, "macro_start_count": 1,
                                                              "trigger_fire_count": 1, "poll_count": 1}})
        diagnostics.update_from_snapshot({**base, "state": "FINISHED", "completed_cycles": 1,
                                          "finish_reason": "workflow_completed",
                                          "trigger_runtime": {"macro_running": False, "macro_start_count": 1,
                                                              "trigger_fire_count": 1, "poll_count": 1001,
                                                              "last_macro_duration_ms": 42}})
        jsonl = next((self.root / "logs" / "runtime").glob("*.jsonl"))
        names = [json.loads(line)["event"] for line in jsonl.read_text(encoding="utf-8").splitlines()]
        self.assertLess(names.index("MACRO_FINISHED"), names.index("CYCLE_COMPLETED"))
        self.assertLess(names.index("CYCLE_COMPLETED"), names.index("WORKFLOW_FINISHED"))
        self.assertEqual(names[-1], "RUN_FINISHED")

    def test_stale_dead_owner_run_is_marked_aborted(self):
        manager = RuntimeRunLogManager(self.root, background_maintenance=False)
        session = manager.start({"workflow_id": "workflow_stale", "workflow_name": "stale", "application_pid": 2_000_000_000})
        # Simulate an older process crash: handles are closed without a final
        # event, then maintenance may safely add the ABORTED terminal record.
        session._log_handle.close()
        session._jsonl_handle.close()
        session._finalized = True
        manager._active = None
        old = time.time() - 10
        os.utime(session.jsonl_path, (old, old))
        manager.run_retention_sync()
        events = [json.loads(line) for line in session.jsonl_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[-1]["event"], "RUN_ABORTED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
