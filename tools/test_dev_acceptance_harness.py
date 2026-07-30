from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.dev_acceptance_harness import EventLog, run_autonomous


class DevAcceptanceHarnessTests(unittest.TestCase):
    def test_event_log_is_jsonl_and_monotonic(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            log = EventLog(Path(root), "test-session")
            first = log.record("ready", "target")
            second = log.record("window_state", "observer")
            rows = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertLessEqual(first["timestamp_monotonic"], second["timestamp_monotonic"])
            self.assertEqual(rows[0]["session_id"], "test-session")

    def test_autonomous_lane_delays_and_blocks_unsafe_clicks(self):
        result = run_autonomous()
        self.assertEqual(result["direct"]["clicks"], 1)
        self.assertGreaterEqual(result["direct"]["elapsed"], 1.8)
        self.assertEqual(result["a15"]["clicks"], 1)
        self.assertEqual(result["a15"]["action"]["delay"], 2.0)
        self.assertEqual(result["cancel"]["clicks"], 0)
        self.assertEqual(result["foreground_loss"]["clicks"], 0)
        self.assertEqual(result["target_change"]["clicks"], 0)


if __name__ == "__main__":
    unittest.main()
