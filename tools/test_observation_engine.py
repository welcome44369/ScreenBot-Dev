"""Regression tests for conservative OCR observation and disappear handling."""
from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import sys
import unittest

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.observation_engine import (
    ObservationEngine,
    ObservationResult,
    analyze_region_readability,
    calculate_text_similarity,
)
from app.text_trigger import TextTrigger
from app.roi_visual_confidence import prepare_visual_frame


TARGET = "尋找採集物"


def readable_image():
    image = Image.new("L", (100, 40), 105)
    draw = ImageDraw.Draw(image)
    for x in range(5, 100, 12):
        draw.line((x, 4, x, 36), fill=230, width=2)
    return image.convert("RGB")


def changed_image():
    image = Image.new("L", (100, 40), 105)
    draw = ImageDraw.Draw(image)
    for y in range(4, 40, 8):
        draw.line((4, y, 96, y), fill=230, width=2)
    return image.convert("RGB")


class ObservationEngineTest(unittest.TestCase):
    def setUp(self):
        self.engine = ObservationEngine()
        self.image = readable_image()

    def observe(self, text):
        return self.engine.observe(TARGET, text, self.image)

    def runtime_observe(self, text, index, image=None):
        image = image or self.image
        return self.engine.observe(
            TARGET,
            text,
            image,
            metadata={
                "observation_id": f"observation-{index}",
                "capture_id": f"capture-{index}",
                "captured_monotonic": float(index),
                "session_id": "session",
                "generation": 1,
                "root_hwnd": 100,
                "run_id": "run",
                "cycle": 1,
                "trigger_id": "trigger",
                "roi_revision": "roi",
                "roi_valid": True,
                "normalized_roi": (0.0, 0.0, 1.0, 1.0),
                "roi_pixel_rect": (0, 0, image.width, image.height),
                "client_size": image.size,
                "visual_frame": prepare_visual_frame(image),
            },
        )

    def test_exact_near_typo_and_absent_states(self):
        self.assertEqual(self.observe(TARGET).state, "PRESENT")
        self.assertEqual(self.observe("尋找採集").state, "UNCERTAIN")
        self.assertEqual(self.observe("尋找採").state, "UNCERTAIN")
        self.assertEqual(self.observe("尋找控物").state, "UNCERTAIN")
        self.assertEqual(self.observe("").state, "UNCERTAIN")
        self.assertEqual(self.observe("").reason, "empty_ocr")

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
        for index, now in enumerate((0.0, 0.2, 0.4)):
            trigger.update(self.runtime_observe(TARGET, index), now=now)
        for index, (now, text) in enumerate(
            ((1.0, "尋找採集"), (2.0, "尋找採"), (3.0, TARGET)),
            10,
        ):
            self.assertFalse(
                trigger.update(
                    self.runtime_observe(text, index), now=now
                ).triggered
            )
        self.assertEqual(trigger.get_status_snapshot()["state_machine_state"], "ARMED_PRESENT")

    def test_invalid_and_near_match_reset_absence_confirmation(self):
        trigger = TextTrigger(TARGET, "disappear", confirm_frames=3, min_absent_duration_ms=5000)
        for index, now in enumerate((0.0, 0.2, 0.4)):
            trigger.update(self.runtime_observe(TARGET, index), now=now)
        trigger.update(self.runtime_observe("", 10), now=1.0)
        self.assertEqual(trigger.get_status_snapshot()["absent_frames"], 0)
        invalid = self.engine.observe(TARGET, "", Image.new("RGB", (20, 20), "black"))
        trigger.update(invalid, now=2.0)
        self.assertEqual(trigger.get_status_snapshot()["absent_frames"], 0)
        trigger.update(self.runtime_observe("尋找採集", 30), now=3.0)
        self.assertEqual(trigger.get_status_snapshot()["absent_frames"], 0)
        self.assertEqual(trigger.get_status_snapshot()["state_machine_state"], "ARMED_PRESENT")

    def test_disappear_uses_fresh_bounded_evidence(self):
        trigger = TextTrigger(
            TARGET,
            "disappear",
            confirm_frames=1,
            min_absent_duration_ms=0,
        )
        present = replace(
            self.runtime_observe(TARGET, 0),
            observation_id="present",
            capture_id="present",
            captured_monotonic=0.0,
            session_id="session",
            generation=1,
            root_hwnd=100,
            run_id="run",
            cycle=1,
            trigger_id="trigger",
        )
        trigger.update(present, now=0.0)
        fired = None
        for index, now in enumerate((0.1, 0.35, 0.60, 0.85), 1):
            absent = ObservationResult(
                "ABSENT", False, 0.0, 0.9, None, 0.0, True,
                "low_presence", f"其他文字{index}", TARGET,
                observation_id=f"absent-{index}",
                capture_id=f"capture-{index}",
                captured_monotonic=now,
                session_id="session", generation=1, root_hwnd=100,
                burst_id=trigger.disappear_burst_id,
                run_id="run", cycle=1, trigger_id="trigger",
                roi_revision="roi", roi_valid=True,
                normalized_roi=(0.0, 0.0, 1.0, 1.0),
                roi_pixel_rect=(0, 0, 100, 40),
                client_size=(100, 40),
                visual_frame=prepare_visual_frame(changed_image()),
            )
            fired = trigger.update(absent, now=now)
        self.assertTrue(fired.triggered)
        self.assertEqual(fired.event_name, "on_text_edge_absent")

    def test_appear_remains_strict(self):
        trigger = TextTrigger(
            TARGET,
            "appear",
            confirm_frames=1,
            min_absent_duration_ms=0,
        )
        self.assertFalse(trigger.update(self.observe("尋找採集"), now=0.0).triggered)
        self.assertFalse(trigger.update(self.observe(TARGET), now=1.0).triggered)
        absent = ObservationResult(
            "ABSENT", False, 0.0, 0.9, None, 0.0, True,
            "low_presence", "其他文字", TARGET,
        )
        self.assertFalse(trigger.update(absent, now=2.0).triggered)
        self.assertTrue(trigger.update(self.observe(TARGET), now=3.0).triggered)

    def test_legacy_disappear_maps_to_edge_absent(self):
        trigger = TextTrigger(TARGET, "disappear", confirm_frames=3)
        self.assertEqual(
            trigger.condition, {"mode": "edge", "desired_state": "absent"}
        )
        self.assertIsNone(trigger.legacy_mode)


if __name__ == "__main__":
    unittest.main()
