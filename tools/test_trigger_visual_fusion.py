"""OCR-positive veto and OpenCV absence fusion regression tests."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.test_disappear_burst_verification import (
    _visual_frame,
    disappear_trigger,
    observation,
)


def armed_trigger():
    trigger = disappear_trigger()
    trigger.update(observation("present", 0.0, 0), now=0.0)
    return trigger


def visual_absence(kind, moment, index, **changes):
    item = replace(
        observation(kind, moment, index),
        visual_frame=_visual_frame("absent"),
        **changes,
    )
    return item


class TriggerVisualFusionTests(unittest.TestCase):
    def test_high_visual_score_keeps_present_when_ocr_is_empty(self):
        trigger = armed_trigger()
        result = trigger.update(observation("empty", 0.2, 1), now=0.2)
        self.assertEqual(
            result.observation.visual_classification,
            "VISUAL_PRESENT_STRONG",
        )
        self.assertEqual(
            result.observation.fused_classification, "PRESENT_STRONG"
        )
        self.assertFalse(trigger.is_disappear_burst_active())

    def test_ocr_near_match_vetoes_low_visual_score(self):
        trigger = armed_trigger()
        result = trigger.update(
            visual_absence("likely", 0.2, 1, text_similarity=0.57),
            now=0.2,
        )
        self.assertEqual(
            result.observation.fused_classification, "PRESENT_LIKELY"
        )
        self.assertFalse(trigger.is_disappear_burst_active())

    def test_empty_ocr_without_visual_frame_is_unknown(self):
        trigger = armed_trigger()
        result = trigger.update(
            replace(
                observation("empty", 0.2, 1),
                visual_frame=None,
            ),
            now=0.2,
        )
        self.assertEqual(result.observation.fused_classification, "UNKNOWN")
        self.assertFalse(result.triggered)

    def test_empty_ocr_plus_repeated_visual_absence_can_trigger(self):
        trigger = armed_trigger()
        results = []
        for index, moment in enumerate((0.1, 0.35, 0.60, 0.85), 1):
            results.append(
                trigger.update(
                    visual_absence("empty", moment, index),
                    now=moment,
                )
            )
        self.assertEqual(sum(item.triggered for item in results), 1)
        self.assertLessEqual(0.85, 1.8)

    def test_single_low_template_score_never_triggers(self):
        trigger = armed_trigger()
        result = trigger.update(
            visual_absence("empty", 0.2, 1), now=0.2
        )
        self.assertFalse(result.triggered)
        self.assertTrue(trigger.is_disappear_burst_active())

    def test_four_of_five_fused_absence_samples_trigger(self):
        trigger = armed_trigger()
        sequence = (
            ("empty", 0.10),
            ("invalid", 0.25),
            ("empty", 0.35),
            ("empty", 0.60),
            ("empty", 0.85),
        )
        results = [
            trigger.update(
                visual_absence(kind, moment, index), now=moment
            )
            for index, (kind, moment) in enumerate(sequence, 1)
        ]
        self.assertEqual(sum(item.triggered for item in results), 1)

    def test_visual_ocr_conflict_is_present_likely(self):
        trigger = armed_trigger()
        result = trigger.update(
            visual_absence("likely", 0.2, 1, text_similarity=0.75),
            now=0.2,
        )
        self.assertEqual(
            result.observation.fused_classification, "PRESENT_LIKELY"
        )

    def test_latched_macro_transition_cannot_start_new_burst(self):
        trigger = armed_trigger()
        for index, moment in enumerate((0.1, 0.35, 0.60, 0.85), 1):
            result = trigger.update(
                visual_absence("empty", moment, index), now=moment
            )
        self.assertTrue(result.triggered)
        after = trigger.update(
            visual_absence("empty", 1.1, 10), now=1.1
        )
        self.assertFalse(after.triggered)
        self.assertFalse(trigger.is_disappear_burst_active())

    def test_present_after_latch_creates_new_template_and_rearms(self):
        trigger = armed_trigger()
        for index, moment in enumerate((0.1, 0.35, 0.60, 0.85), 1):
            trigger.update(
                visual_absence("empty", moment, index), now=moment
            )
        result = trigger.update(
            observation("present", 1.0, 10), now=1.0
        )
        events = [item["event"] for item in result.condition_events]
        self.assertIn("PRESENT_TEMPLATE_CREATED", events)
        self.assertIn("TRIGGER_REARMED", events)
        self.assertTrue(
            trigger.get_status_snapshot()["present_template_available"]
        )

    def test_stable_replacement_text_uses_two_sample_fast_path(self):
        trigger = armed_trigger()
        first = trigger.update(
            observation(
                "absent",
                0.1,
                1,
                recognized_text="前往庫漢",
            ),
            now=0.1,
        )
        second = trigger.update(
            observation(
                "absent",
                0.5,
                2,
                recognized_text="前往庫漢",
            ),
            now=0.5,
        )
        self.assertFalse(first.triggered)
        self.assertTrue(second.triggered)
        confirmed = next(
            item
            for item in second.condition_events
            if item["event"] == "DISAPPEAR_CONFIRMED"
        )
        self.assertEqual(
            confirmed["data"]["confirmation_reason"],
            "stable_replacement_text",
        )

    def test_old_empty_ocr_leak_replay_stays_present(self):
        trigger = armed_trigger()
        results = [
            trigger.update(observation("empty", moment, index), now=moment)
            for index, moment in enumerate((0.2, 0.4, 0.6, 0.8), 1)
        ]
        self.assertFalse(any(item.triggered for item in results))
        self.assertFalse(trigger.is_disappear_burst_active())


if __name__ == "__main__":
    unittest.main()
