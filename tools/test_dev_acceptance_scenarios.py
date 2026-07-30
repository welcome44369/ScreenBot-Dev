"""Focused regression tests for the non-input DEV acceptance command."""
from __future__ import annotations

import unittest

from tools.run_dev_acceptance import run_one


class DevAcceptanceScenarioTests(unittest.TestCase):
    def test_workflow_start_uses_one_trigger_and_one_script(self):
        result, output = run_one("workflow-start")
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["trigger_fires"], 1)
        self.assertEqual(result["script_schedules"], 1)
        self.assertTrue((output / "events.jsonl").is_file())
        self.assertTrue((output / "summary.json").is_file())
        self.assertTrue((output / "report.txt").is_file())

    def test_invalid_references_fail_before_input(self):
        result, _output = run_one("invalid-reference")
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["recorded_mouse_actions"], 0)
        self.assertEqual(result["recorded_keyboard_actions"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
