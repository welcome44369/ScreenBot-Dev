import time
from datetime import datetime
from dataclasses import dataclass

from app.observation_engine import ObservationEngine, ObservationResult


@dataclass
class TriggerResult:
    triggered: bool
    event_name: str | None
    present: bool
    stable_present: bool | None
    changed: bool
    observation: ObservationResult | None = None


class TextTrigger:
    """Strict appear and conservative disappear state machine."""

    def __init__(self, target_text, event, confirm_frames=1, cooldown_ms=0,
                 min_absent_duration_ms=5000, observation_config=None):
        if event not in {"appear", "disappear"}:
            raise ValueError("Text trigger event must be 'appear' or 'disappear'")
        if confirm_frames < 1 or min_absent_duration_ms < 0 or cooldown_ms < 0:
            raise ValueError("Invalid trigger confirmation settings")
        self.target_texts = [target_text] if isinstance(target_text, str) else [str(v) for v in target_text]
        self.target_text = self.target_texts[0] if self.target_texts else ""
        self.event = event
        self.confirm_frames = confirm_frames
        self.cooldown_ms = cooldown_ms
        self.min_absent_duration_ms = min_absent_duration_ms
        self.observer = ObservationEngine(observation_config)
        self.state = "DISARMED"
        self._present_count = 0
        self._absent_count = 0
        self._absent_started_at = None
        self._last_trigger_at = None
        self._last_trigger_wall_time = None
        self._last_observation = None
        self.has_ever_been_seen = False

    def update(self, observation, now=None):
        if isinstance(observation, str):
            observation = self.observer.observe(self.target_texts, observation, image=None)
        if not isinstance(observation, ObservationResult):
            raise TypeError("TextTrigger.update requires ObservationResult")
        now = time.monotonic() if now is None else now
        self._last_observation = observation
        previous = self.state
        if observation.state == "INVALID":
            return self._result(False, None, previous != self.state)
        if self.event == "appear":
            return self._update_appear(observation, now, previous)
        return self._update_disappear(observation, now, previous)

    def _update_appear(self, observation, now, previous):
        if observation.state == "PRESENT" and observation.exact_match:
            self._present_count += 1
            if self._present_count >= self.confirm_frames and self.state != "TRIGGERED":
                self.state = "TRIGGERED"
                if not self._in_cooldown(now):
                    self._mark_trigger(now)
                    return self._result(True, "on_text_appear", previous != self.state)
        else:
            self._present_count = 0
            if self.state == "TRIGGERED":
                self.state = "DISARMED"
        return self._result(False, None, previous != self.state)

    def _update_disappear(self, observation, now, previous):
        if observation.state == "PRESENT":
            self.has_ever_been_seen = True
            self._present_count += 1
            self._reset_absence()
            if self._present_count >= self.confirm_frames:
                self.state = "ARMED_PRESENT"
            else:
                self.state = "PRESENT_CONFIRMING"
            return self._result(False, None, previous != self.state)
        if observation.state == "UNCERTAIN":
            # A near match disproves neither presence nor disappearance, but it
            # always breaks an in-progress missing sequence.
            self._reset_absence()
            # Do not promote a partially confirmed presence to ARMED merely
            # because a near match arrived.  A disappear trigger becomes
            # armed only after the configured number of definite PRESENT
            # observations.
            if self.state in {"ARMED_PRESENT", "ABSENT_CONFIRMING", "TRIGGERED"}:
                self.state = "ARMED_PRESENT"
            return self._result(False, None, previous != self.state)
        # ABSENT: only armed, valid absence contributes confirmation evidence.
        self._present_count = 0
        if self.state not in {"ARMED_PRESENT", "ABSENT_CONFIRMING", "TRIGGERED"}:
            return self._result(False, None, previous != self.state)
        if self._absent_count == 0:
            self._absent_started_at = now
        self._absent_count += 1
        self.state = "ABSENT_CONFIRMING"
        duration_ms = self._absent_duration_ms(now)
        if (
            self._absent_count >= self.confirm_frames
            and duration_ms >= self.min_absent_duration_ms
            and not self._in_cooldown(now)
        ):
            self.state = "TRIGGERED"
            self._mark_trigger(now)
            self._reset_absence()
            return self._result(True, "on_text_disappear", previous != self.state)
        return self._result(False, None, previous != self.state)

    def _reset_absence(self):
        self._absent_count = 0
        self._absent_started_at = None

    def _absent_duration_ms(self, now):
        return 0 if self._absent_started_at is None else int((now - self._absent_started_at) * 1000)

    def _mark_trigger(self, now):
        self._last_trigger_at = now
        self._last_trigger_wall_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _in_cooldown(self, now):
        return self._last_trigger_at is not None and self.cooldown_ms > 0 and (now - self._last_trigger_at) * 1000 < self.cooldown_ms

    def _result(self, triggered, event_name, changed):
        observation = self._last_observation
        present = observation is not None and observation.state == "PRESENT"
        stable = True if self.state in {"ARMED_PRESENT", "TRIGGERED"} and present else (False if self.state == "ABSENT_CONFIRMING" else None)
        return TriggerResult(triggered, event_name, present, stable, changed, observation)

    def get_status_snapshot(self):
        now = time.monotonic()
        observation = self._last_observation
        return {
            "stable_present": self.state == "ARMED_PRESENT",
            "candidate_present": observation.state == "PRESENT" if observation else None,
            "candidate_count": self._absent_count if self.state == "ABSENT_CONFIRMING" else self._present_count,
            "confirm_frames": self.confirm_frames,
            "has_ever_been_seen": self.has_ever_been_seen,
            "last_present": observation.state == "PRESENT" if observation else None,
            "cooldown_remaining_ms": max(0, self.cooldown_ms - int((now - self._last_trigger_at) * 1000)) if self._last_trigger_at else 0,
            "last_trigger_time": self._last_trigger_wall_time,
            "observation": observation.as_dict() if observation else None,
            "observation_state": observation.state if observation else None,
            "armed": self.has_ever_been_seen,
            "state_machine_state": self.state,
            "absent_frames": self._absent_count,
            "absent_duration_ms": self._absent_duration_ms(now),
            "required_absent_duration_ms": self.min_absent_duration_ms,
            "required_frames": self.confirm_frames,
        }
