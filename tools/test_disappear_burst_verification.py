"""Bounded OCR burst verification for disappear edge conditions."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.observation_engine import ObservationResult
from app.text_trigger import TextTrigger
from app.trigger_runner import TriggerRunner


TARGET = "尋找採集物"


def observation(
    kind,
    captured,
    index,
    *,
    similarity=None,
    burst_id=None,
    generation=1,
    capture_id=None,
):
    if kind == "present":
        state, exact, score, text, valid, reason = (
            "PRESENT",
            True,
            1.0,
            TARGET,
            True,
            "exact_match",
        )
    elif kind == "likely":
        state, exact, score, text, valid, reason = (
            "UNCERTAIN",
            False,
            0.75 if similarity is None else similarity,
            "尋找採集",
            True,
            "near_match",
        )
    elif kind == "absent":
        state, exact, score, text, valid, reason = (
            "ABSENT",
            False,
            0.05 if similarity is None else similarity,
            "其他介面文字",
            True,
            "low_presence",
        )
    elif kind == "empty":
        state, exact, score, text, valid, reason = (
            "UNCERTAIN",
            False,
            0.0,
            "",
            True,
            "empty_ocr",
        )
    else:
        state, exact, score, text, valid, reason = (
            "INVALID",
            False,
            0.0,
            "",
            False,
            "near_black",
        )
    return ObservationResult(
        state,
        exact,
        score,
        0.9 if valid else 0.0,
        None,
        score,
        valid,
        reason,
        text,
        TARGET,
        observation_id=f"observation-{index}",
        capture_id=capture_id or f"capture-{index}",
        captured_monotonic=captured,
        session_id="session-a",
        generation=generation,
        root_hwnd=100,
        burst_id=burst_id,
        trigger_id="trigger-a",
    )


def disappear_trigger(memory=None):
    return TextTrigger(
        TARGET,
        condition={"mode": "edge", "desired_state": "absent"},
        condition_memory=memory,
        confirm_frames=1,
        min_absent_duration_ms=0,
    )


class DisappearBurstVerificationTests(unittest.TestCase):
    def test_first_absent_starts_burst_and_four_fresh_votes_confirm(self):
        trigger = disappear_trigger()
        trigger.update(observation("present", 0.0, 0), now=0.0)
        results = [
            trigger.update(
                observation("absent", moment, index),
                now=moment,
            )
            for index, moment in enumerate((0.10, 0.35, 0.60, 0.85), 1)
        ]
        self.assertTrue(results[-1].triggered)
        self.assertEqual(sum(result.triggered for result in results), 1)
        self.assertLessEqual(0.85 - 0.10, 1.8)
        events = [
            item["event"]
            for result in results
            for item in result.condition_events
        ]
        self.assertIn("DISAPPEAR_BURST_STARTED", events)
        self.assertIn("DISAPPEAR_CONFIRMED", events)
        self.assertIn("TRIGGER_LATCHED", events)

    def test_empty_ocr_replay_never_triggers(self):
        trigger = disappear_trigger()
        trigger.update(observation("present", 0.0, 0), now=0.0)
        results = [
            trigger.update(observation("empty", moment, index), now=moment)
            for index, moment in enumerate((0.2, 0.4, 0.6, 0.8), 1)
        ]
        self.assertFalse(any(result.triggered for result in results))
        self.assertFalse(trigger.is_disappear_burst_active())

    def test_near_match_replay_cancels_each_candidate(self):
        trigger = disappear_trigger()
        trigger.update(observation("present", 0.0, 0), now=0.0)
        sequence = (
            ("likely", 0.20, 0.75),
            ("absent", 0.40, None),
            ("likely", 0.55, 0.57),
            ("absent", 0.75, None),
            ("likely", 0.90, 0.80),
        )
        results = []
        for index, (kind, moment, score) in enumerate(sequence, 1):
            results.append(
                trigger.update(
                    observation(
                        kind,
                        moment,
                        index,
                        similarity=score,
                    ),
                    now=moment,
                )
            )
        self.assertFalse(any(result.triggered for result in results))
        self.assertFalse(trigger.is_disappear_burst_active())

    def test_invalid_invalid_absent_does_not_trigger(self):
        trigger = disappear_trigger()
        trigger.update(observation("present", 0.0, 0), now=0.0)
        results = [
            trigger.update(observation(kind, moment, index), now=moment)
            for index, (kind, moment) in enumerate(
                (("invalid", 0.2), ("invalid", 0.4), ("absent", 0.6)),
                1,
            )
        ]
        self.assertFalse(any(result.triggered for result in results))
        self.assertEqual(
            trigger.get_status_snapshot()["disappear_evidence_count"], 1
        )

    def test_evidence_span_below_700_ms_does_not_trigger(self):
        trigger = disappear_trigger()
        trigger.update(observation("present", 0.0, 0), now=0.0)
        results = [
            trigger.update(observation("absent", moment, index), now=moment)
            for index, moment in enumerate((0.10, 0.20, 0.30, 0.40), 1)
        ]
        self.assertFalse(any(result.triggered for result in results))

    def test_insufficient_evidence_expires_without_extending_window(self):
        trigger = disappear_trigger()
        trigger.update(observation("present", 0.0, 0), now=0.0)
        trigger.update(observation("absent", 0.10, 1), now=0.10)
        trigger.update(observation("absent", 0.50, 2), now=0.50)
        result = trigger.update(observation("empty", 1.95, 3), now=1.95)
        events = [item["event"] for item in result.condition_events]
        self.assertIn("DISAPPEAR_BURST_CANCELLED", events)
        self.assertFalse(result.triggered)
        self.assertFalse(trigger.is_disappear_burst_active())

    def test_present_likely_clears_accumulated_absence(self):
        trigger = disappear_trigger()
        trigger.update(observation("present", 0.0, 0), now=0.0)
        trigger.update(observation("absent", 0.10, 1), now=0.10)
        trigger.update(observation("absent", 0.35, 2), now=0.35)
        result = trigger.update(
            observation("likely", 0.50, 3, similarity=0.57),
            now=0.50,
        )
        self.assertFalse(result.triggered)
        self.assertFalse(trigger.is_disappear_burst_active())
        self.assertEqual(
            trigger.get_status_snapshot()["disappear_evidence_count"], 0
        )

    def test_latched_trigger_requires_present_before_second_fire(self):
        memory = {}
        trigger = disappear_trigger(memory)
        trigger.update(observation("present", 0.0, 0), now=0.0)
        first = None
        for index, moment in enumerate((0.10, 0.35, 0.60, 0.85), 1):
            first = trigger.update(
                observation("absent", moment, index), now=moment
            )
        self.assertTrue(first.triggered)
        for index, moment in enumerate((1.05, 1.30, 1.55, 1.80), 10):
            self.assertFalse(
                trigger.update(
                    observation("absent", moment, index), now=moment
                ).triggered
            )
        rearm = trigger.update(
            observation("present", 2.0, 20), now=2.0
        )
        self.assertIn(
            "TRIGGER_REARMED",
            [event["event"] for event in rearm.condition_events],
        )
        second_results = []
        for index, moment in enumerate((2.10, 2.35, 2.60, 2.85), 21):
            second_results.append(
                trigger.update(
                    observation("absent", moment, index), now=moment
                )
            )
        self.assertEqual(
            sum(result.triggered for result in second_results), 1
        )

    def test_initial_absent_only_sets_baseline(self):
        trigger = disappear_trigger()
        results = [
            trigger.update(observation("absent", moment, index), now=moment)
            for index, moment in enumerate((0.0, 0.3, 0.6, 0.9), 1)
        ]
        self.assertFalse(any(result.triggered for result in results))
        self.assertEqual(
            trigger.get_status_snapshot()["baseline_state"], "ABSENT"
        )

    def test_runner_uses_bounded_normal_and_burst_intervals(self):
        runner = TriggerRunner(
            object(),
            object(),
            object(),
            {
                "name": "disappear-step",
                "trigger": {
                    "type": "text",
                    "text": TARGET,
                    "event": "disappear",
                    "region": {
                        "x_ratio": 0.0,
                        "y_ratio": 0.0,
                        "width_ratio": 1.0,
                        "height_ratio": 1.0,
                    },
                    "poll_interval_ms": 2000,
                },
                "macro": "macro.json",
            },
        )
        runner._stop_event.wait = Mock(return_value=False)

        runner._wait_interval()
        runner._stop_event.wait.assert_called_once_with(0.5)

        runner.text_trigger.update(
            observation("present", 0.0, 100), now=0.0
        )
        runner.text_trigger.update(
            observation("absent", 0.1, 101), now=0.1
        )
        runner._stop_event.wait.reset_mock()
        runner._wait_interval()
        runner._stop_event.wait.assert_called_once_with(0.175)

    def test_runtime_stop_immediately_cancels_active_burst(self):
        runner = TriggerRunner(
            object(),
            object(),
            object(),
            {
                "name": "disappear-step",
                "trigger": {
                    "type": "text",
                    "text": TARGET,
                    "event": "disappear",
                    "region": {
                        "x_ratio": 0.0,
                        "y_ratio": 0.0,
                        "width_ratio": 1.0,
                        "height_ratio": 1.0,
                    },
                },
                "macro": "macro.json",
            },
        )
        runner.text_trigger.update(
            observation("present", 0.0, 110), now=0.0
        )
        runner.text_trigger.update(
            observation("absent", 0.1, 111), now=0.1
        )
        self.assertTrue(runner.text_trigger.is_disappear_burst_active())

        runner.request_stop()

        self.assertFalse(runner.text_trigger.is_disappear_burst_active())


if __name__ == "__main__":
    unittest.main()
