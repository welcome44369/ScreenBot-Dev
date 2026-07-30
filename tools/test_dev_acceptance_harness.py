from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.dev_acceptance_harness import EventLog, _run_recording_workflow


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
        direct = _run_recording_workflow(.05)
        blocked = _run_recording_workflow(.35, lambda _runner, _player, gate: setattr(gate, "blocked", True))
        self.assertEqual(direct["clicks"], 1)
        self.assertEqual(direct["trigger_fires"], 1)
        self.assertEqual(direct["script_schedules"], 1)
        self.assertEqual(blocked["clicks"], 0)
        self.assertEqual(blocked["playback_status"], "INPUT_BLOCKED")


if __name__ == "__main__":
    unittest.main()
