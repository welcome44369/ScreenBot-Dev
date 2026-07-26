"""Regression tests for conservative OCR observation and disappear handling."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.observation_engine import ObservationEngine, analyze_region_readability, calculate_text_similarity
from app.text_trigger import TextTrigger


TARGET = "尋找採集物"


def readable_image():
    image = Image.new("L", (100, 40), 105)
    draw = ImageDraw.Draw(image)
    for x in range(5, 100, 12):
        draw.line((x, 4, x, 36), fill=230, width=2)
    return image.convert("RGB")


class ObservationEngineTest(unittest.TestCase):
    def setUp(self):
        self.engine = ObservationEngine()
        self.image = readable_image()

    def observe(self, text):
        return self.engine.observe(TARGET, text, self.image)

    def test_exact_near_typo_and_absent_states(self):
        self.assertEqual(self.observe(TARGET).state, "PRESENT")
        self.assertEqual(self.observe("尋找採集").state, "UNCERTAIN")
        self.assertEqual(self.observe("尋找採").state, "UNCERTAIN")
        self.assertEqual(self.observe("尋找控物").state, "UNCERTAIN")
        self.assertEqual(self.observe("").state, "ABSENT")

    def test_invalid_uniform_crop_never_means_absent(self):
        result = self.engine.observe(TARGET, "", Image.new("RGB", (20, 20), "white"))
        self.assertEqual(result.state, "INVALID")
        self.assertFalse(result.observation_valid)
        self.assertTrue(result.readability.near_white)

    def test_similarity_is_not_binary(self):
        self.assertEqual(calculate_text_similarity(TARGET, TARGET), 1.0)
        self.assertGreaterEqual(calculate_text_similarity(TARGET, "尋找採集"), 0.70)
        self.assertGreaterEqual(calculate_text_similarity(TARGET, "尋找控物"), 0.65)
        self.assertLess(calculate_text_similarity(TARGET, "採集小麥"), 0.70)

    def test_disappear_short_omission_and_typo_do_not_trigger(self):
        trigger = TextTrigger(TARGET, "disappear", confirm_frames=3, min_absent_duration_ms=5000)
        for now in (0.0, 0.2, 0.4):
            trigger.update(self.observe(TARGET), now=now)
        for now, text in ((1.0, "尋找採集"), (2.0, "尋找採"), (3.0, TARGET)):
            self.assertFalse(trigger.update(self.observe(text), now=now).triggered)
        self.assertEqual(trigger.get_status_snapshot()["state_machine_state"], "ARMED_PRESENT")

    def test_invalid_and_near_match_reset_absence_confirmation(self):
        trigger = TextTrigger(TARGET, "disappear", confirm_frames=3, min_absent_duration_ms=5000)
        for now in (0.0, 0.2, 0.4):
            trigger.update(self.observe(TARGET), now=now)
        trigger.update(self.observe(""), now=1.0)
        self.assertEqual(trigger.get_status_snapshot()["absent_frames"], 1)
        invalid = self.engine.observe(TARGET, "", Image.new("RGB", (20, 20), "black"))
        trigger.update(invalid, now=2.0)
        self.assertEqual(trigger.get_status_snapshot()["absent_frames"], 1)
        trigger.update(self.observe("尋找採集"), now=3.0)
        self.assertEqual(trigger.get_status_snapshot()["absent_frames"], 0)
        self.assertEqual(trigger.get_status_snapshot()["state_machine_state"], "ARMED_PRESENT")

    def test_disappear_requires_frames_and_monotonic_duration(self):
        trigger = TextTrigger(TARGET, "disappear", confirm_frames=3, min_absent_duration_ms=5000)
        for now in (0.0, 0.2, 0.4):
            trigger.update(self.observe(TARGET), now=now)
        self.assertFalse(trigger.update(self.observe(""), now=1.0).triggered)
        self.assertFalse(trigger.update(self.observe(""), now=3.0).triggered)
        fired = trigger.update(self.observe(""), now=6.1)
        self.assertTrue(fired.triggered)
        self.assertEqual(fired.event_name, "on_text_disappear")

    def test_appear_remains_strict(self):
        trigger = TextTrigger(TARGET, "appear", confirm_frames=2)
        self.assertFalse(trigger.update(self.observe("尋找採集"), now=0.0).triggered)
        self.assertFalse(trigger.update(self.observe(TARGET), now=1.0).triggered)
        self.assertTrue(trigger.update(self.observe(TARGET), now=2.0).triggered)

    def test_legacy_trigger_uses_conservative_duration_default(self):
        trigger = TextTrigger(TARGET, "disappear", confirm_frames=3)
        self.assertEqual(trigger.min_absent_duration_ms, 5000)


if __name__ == "__main__":
    unittest.main()
