"""Focused no-I/O regression tests for the six text-trigger conditions."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.observation_engine import ObservationResult
from app.text_trigger import TextTrigger


def observation(state, reason=None, index=0):
    return ObservationResult(
        state,
        state == "PRESENT",
        1.0 if state == "PRESENT" else 0.0,
        .9,
        None,
        .9,
        state != "INVALID",
        reason or state,
        "target" if state == "PRESENT" else ("other" if state == "ABSENT" else ""),
        "target",
        observation_id=f"observation-{index}",
        capture_id=f"capture-{index}",
        captured_monotonic=float(index + 1),
        session_id="session",
        generation=1,
        root_hwnd=100,
    )


def feed(trigger, states):
    return [trigger.update(observation(state, index=index), now=index + 1).triggered for index, state in enumerate(states)]


def trigger(condition, memory=None, frames=1):
    return TextTrigger("target", condition=condition, confirm_frames=frames, min_absent_duration_ms=0, condition_memory=memory)


def main():
    assert feed(trigger({"mode": "initial", "desired_state": "absent"}), ["ABSENT"]) == [True]
    assert feed(trigger({"mode": "initial", "desired_state": "absent"}), ["PRESENT", "ABSENT"]) == [False, False]
    assert feed(trigger({"mode": "initial", "desired_state": "present"}), ["PRESENT"]) == [True]
    assert feed(trigger({"mode": "edge", "desired_state": "present"}), ["PRESENT"]) == [False]
    assert feed(trigger({"mode": "edge", "desired_state": "present"}), ["ABSENT", "PRESENT"]) == [False, True]
    assert feed(trigger({"mode": "edge", "desired_state": "absent"}), ["ABSENT"]) == [False]
    assert feed(trigger({"mode": "edge", "desired_state": "absent"}), ["PRESENT", "ABSENT"]) == [False, False]
    memory = {}
    first = trigger({"mode": "state", "desired_state": "absent"}, memory)
    assert feed(first, ["ABSENT", "ABSENT", "UNCERTAIN", "INVALID"]) == [True, False, False, False]
    # Same workflow run / new runner / next cycle: latch is still held.
    assert feed(trigger({"mode": "state", "desired_state": "absent"}, memory), ["ABSENT"]) == [False]
    assert feed(trigger({"mode": "state", "desired_state": "absent"}, memory), ["PRESENT", "ABSENT"]) == [False, True]
    legacy = TextTrigger(
        "target", event="appear", confirm_frames=1,
        min_absent_duration_ms=0,
    )
    assert feed(legacy, ["PRESENT", "ABSENT", "PRESENT"]) == [False, False, True]

    # Near-matches are inert and cannot manufacture a recovery token.
    recovery = trigger({"mode": "state", "desired_state": "absent"})
    assert recovery.update(observation("ABSENT"), now=1).triggered
    assert not recovery.update(observation("UNCERTAIN", "near_match"), now=2).triggered
    assert not recovery.update(observation("ABSENT"), now=3).triggered
    assert not recovery.update(observation("UNCERTAIN", "near_match"), now=4).triggered
    assert not recovery.update(observation("ABSENT"), now=5).triggered
    assert recovery.memory.get("recovery_token", 0) == 0
    assert not recovery.memory.get("recovery_pending", False)

    # Desired state alone never creates a recovery attempt/token.
    permanent = trigger({"mode": "state", "desired_state": "absent"})
    results = [permanent.update(observation("ABSENT"), now=index).triggered for index in range(1, 101)]
    assert sum(results) == 1 and permanent.memory["lost_episode_count"] == 0 and permanent.memory["recovery_token"] == 0

    symmetric = trigger({"mode": "state", "desired_state": "present"}, frames=2)
    assert not symmetric.update(observation("PRESENT"), now=1).triggered
    assert symmetric.update(observation("PRESENT"), now=2).triggered
    assert not symmetric.update(observation("ABSENT"), now=3).triggered
    assert not symmetric.update(observation("PRESENT"), now=4).triggered
    assert not symmetric.update(observation("PRESENT"), now=5).triggered
    assert symmetric.memory["lost_episode_count"] == 1
    print("TRIGGER_CONDITIONS_OK")


if __name__ == "__main__":
    main()
