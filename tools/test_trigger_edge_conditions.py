"""Canonical trigger mapping, edge latching, and recovery scope."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.observation_engine import ObservationResult
from app.text_trigger import TextTrigger
from app.trigger_conditions import normalize_condition


def basic_observation(state, *, similarity=None, reason=None):
    present = state == "PRESENT"
    return ObservationResult(
        state,
        present,
        1.0 if present else (0.0 if similarity is None else similarity),
        0.9,
        None,
        0.9 if present else 0.1,
        state != "INVALID",
        reason or state.lower(),
        "target" if present else ("other" if state == "ABSENT" else ""),
        "target",
    )


class TriggerEdgeConditionTests(unittest.TestCase):
    def test_legacy_names_normalize_to_canonical_edges(self):
        self.assertEqual(
            normalize_condition(event="disappear"),
            ({"mode": "edge", "desired_state": "absent"}, None),
        )
        self.assertEqual(
            normalize_condition(event="appear"),
            ({"mode": "edge", "desired_state": "present"}, None),
        )

    def test_state_absent_retains_current_state_semantics(self):
        trigger = TextTrigger(
            "target",
            condition={"mode": "state", "desired_state": "absent"},
            confirm_frames=1,
            min_absent_duration_ms=0,
        )
        self.assertTrue(
            trigger.update(basic_observation("ABSENT"), now=1.0).triggered
        )

    def test_appear_requires_absent_baseline_then_present_edge(self):
        trigger = TextTrigger(
            "target",
            event="appear",
            confirm_frames=1,
            min_absent_duration_ms=0,
        )
        self.assertFalse(
            trigger.update(basic_observation("PRESENT"), now=1.0).triggered
        )
        self.assertFalse(
            trigger.update(basic_observation("ABSENT"), now=2.0).triggered
        )
        self.assertTrue(
            trigger.update(basic_observation("PRESENT"), now=3.0).triggered
        )

    def test_edge_conditions_never_emit_static_recovery_events(self):
        for event in ("appear", "disappear"):
            with self.subTest(event=event):
                trigger = TextTrigger(
                    "target",
                    event=event,
                    confirm_frames=1,
                    min_absent_duration_ms=0,
                )
                events = []
                for index, state in enumerate(
                    ("PRESENT", "UNCERTAIN", "ABSENT", "PRESENT"), 1
                ):
                    result = trigger.update(
                        basic_observation(
                            state,
                            similarity=0.75,
                            reason="near_match",
                        ),
                        now=float(index),
                    )
                    events.extend(
                        item["event"] for item in result.condition_events
                    )
                self.assertNotIn("TRIGGER_STATIC_RECOVERY_STARTED", events)
                self.assertNotIn("TRIGGER_STATIC_RECOVERY_MATCHED", events)

    def test_near_match_does_not_create_state_recovery_token(self):
        memory = {}
        trigger = TextTrigger(
            "target",
            condition={"mode": "state", "desired_state": "absent"},
            condition_memory=memory,
            confirm_frames=1,
            min_absent_duration_ms=0,
        )
        trigger.update(basic_observation("ABSENT"), now=1.0)
        for index in range(2, 8):
            trigger.update(
                basic_observation(
                    "UNCERTAIN",
                    similarity=0.75,
                    reason="near_match",
                ),
                now=float(index),
            )
            trigger.update(basic_observation("ABSENT"), now=index + 0.1)
        self.assertEqual(memory.get("recovery_token", 0), 0)
        self.assertFalse(memory.get("recovery_pending", False))

    def test_state_recovery_token_is_consumed_once(self):
        memory = {}
        trigger = TextTrigger(
            "target",
            condition={"mode": "state", "desired_state": "absent"},
            condition_memory=memory,
            confirm_frames=2,
            min_absent_duration_ms=0,
        )
        trigger.update(basic_observation("ABSENT"), now=1.0)
        trigger.update(basic_observation("ABSENT"), now=1.1)
        # Two interrupted PRESENT candidates create exactly one recovery token.
        for index in (2.0, 4.0):
            trigger.update(basic_observation("PRESENT"), now=index)
            trigger.update(basic_observation("ABSENT"), now=index + 0.1)
            trigger.update(basic_observation("ABSENT"), now=index + 0.2)
        self.assertEqual(memory.get("recovery_token"), 1)

        results = [
            trigger.update(basic_observation("ABSENT"), now=6.0),
            trigger.update(basic_observation("ABSENT"), now=6.1),
            trigger.update(basic_observation("ABSENT"), now=7.0),
        ]
        matched = [
            event
            for result in results
            for event in result.condition_events
            if event["event"] == "TRIGGER_STATIC_RECOVERY_MATCHED"
        ]
        self.assertEqual(len(matched), 1)
        self.assertEqual(memory.get("recovery_token", 0), 0)
        self.assertFalse(memory.get("recovery_pending", False))


if __name__ == "__main__":
    unittest.main()
