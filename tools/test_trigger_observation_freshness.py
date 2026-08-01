"""Fresh-frame and frozen TargetSession evidence checks for OCR bursts."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.test_disappear_burst_verification import (
    disappear_trigger,
    observation,
)


class TriggerObservationFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.trigger = disappear_trigger()
        self.trigger.update(observation("present", 0.0, 0), now=0.0)

    def test_duplicate_capture_id_is_not_counted_twice(self):
        first = observation(
            "absent", 0.10, 1, capture_id="same-frame"
        )
        self.trigger.update(first, now=0.10)
        duplicate = replace(
            observation(
                "absent", 0.30, 2, capture_id="same-frame"
            ),
            burst_id=self.trigger.disappear_burst_id,
        )
        result = self.trigger.update(duplicate, now=0.30)
        self.assertEqual(
            self.trigger.get_status_snapshot()["disappear_evidence_count"], 1
        )
        self.assertIn(
            "duplicate_capture",
            [event["data"].get("reason") for event in result.condition_events],
        )

    def test_old_burst_id_is_rejected(self):
        self.trigger.update(
            observation("absent", 0.10, 1), now=0.10
        )
        result = self.trigger.update(
            observation(
                "absent", 0.35, 2, burst_id="old-burst"
            ),
            now=0.35,
        )
        self.assertIn(
            "stale_burst",
            [event["data"].get("reason") for event in result.condition_events],
        )

    def test_old_generation_is_rejected_before_starting_burst(self):
        result = self.trigger.update(
            observation("absent", 0.10, 1, generation=2),
            now=0.10,
        )
        self.assertFalse(self.trigger.is_disappear_burst_active())
        self.assertIn(
            "target_identity_changed",
            [event["data"].get("reason") for event in result.condition_events],
        )

    def test_out_of_order_capture_is_rejected(self):
        self.trigger.update(
            observation("absent", 0.50, 1), now=0.50
        )
        result = self.trigger.update(
            observation(
                "absent",
                0.40,
                2,
                burst_id=self.trigger.disappear_burst_id,
            ),
            now=0.60,
        )
        self.assertIn(
            "out_of_order_capture",
            [event["data"].get("reason") for event in result.condition_events],
        )

    def test_stale_ocr_result_does_not_rewrite_newer_state(self):
        self.trigger.update(
            observation("absent", 0.10, 1), now=0.10
        )
        self.trigger.update(
            observation(
                "present",
                0.50,
                2,
                burst_id=self.trigger.disappear_burst_id,
            ),
            now=0.50,
        )
        stale = observation("absent", 0.20, 3)
        result = self.trigger.update(stale, now=2.10)
        self.assertFalse(result.triggered)
        self.assertFalse(self.trigger.is_disappear_burst_active())
        self.assertEqual(
            self.trigger.get_status_snapshot()["state_machine_state"],
            "ARMED_PRESENT",
        )

    def test_missing_metadata_cannot_be_absent_evidence(self):
        missing = replace(
            observation("absent", 0.10, 1),
            observation_id=None,
            capture_id=None,
            captured_monotonic=None,
        )
        result = self.trigger.update(missing, now=0.10)
        self.assertFalse(result.triggered)
        self.assertFalse(self.trigger.is_disappear_burst_active())
        self.assertIn(
            "missing_freshness_metadata",
            [event["data"].get("reason") for event in result.condition_events],
        )

    def test_target_invalidation_cannot_complete_existing_burst(self):
        self.trigger.update(
            observation("absent", 0.10, 1), now=0.10
        )
        invalid = replace(
            observation(
                "invalid",
                0.35,
                2,
                burst_id=self.trigger.disappear_burst_id,
            ),
            session_id=None,
            generation=None,
            root_hwnd=None,
        )
        result = self.trigger.update(invalid, now=0.35)
        self.assertFalse(result.triggered)
        self.assertFalse(self.trigger.is_disappear_burst_active())
        self.assertIn(
            "DISAPPEAR_BURST_CANCELLED",
            [event["event"] for event in result.condition_events],
        )
        self.assertIn(
            "target_identity_changed",
            [event["data"].get("reason") for event in result.condition_events],
        )


if __name__ == "__main__":
    unittest.main()
