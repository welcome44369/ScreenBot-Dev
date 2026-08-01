import time
from datetime import datetime
from dataclasses import dataclass, field, replace
from uuid import uuid4

from app.observation_engine import ObservationEngine, ObservationResult
from app.roi_visual_confidence import (
    VISUAL_ABSENT_THRESHOLD,
    VISUAL_PRESENT_LIKELY_THRESHOLD,
    VISUAL_PRESENT_STRONG_THRESHOLD,
    classify_visual,
    compare_visual_frame,
)
from app.trigger_conditions import LEGACY_APPEAR, LEGACY_DISAPPEAR, label_for_code, normalize_condition


@dataclass
class TriggerResult:
    triggered: bool
    event_name: str | None
    present: bool
    stable_present: bool | None
    changed: bool
    observation: ObservationResult | None = None
    condition_events: tuple = field(default_factory=tuple)


class TextTrigger:
    """Observation consumer and condition state machine.

    ObservationEngine owns OCR confidence and four-state classification.  This
    class owns trigger semantics only, including the run-scoped state supplied
    by WorkflowRunner for conditions that must survive a cycle restart.
    """

    def __init__(self, target_text, event=None, confirm_frames=1, cooldown_ms=0,
                 min_absent_duration_ms=5000, observation_config=None,
                 condition=None, condition_memory=None):
        if confirm_frames < 1 or min_absent_duration_ms < 0 or cooldown_ms < 0:
            raise ValueError("Invalid trigger confirmation settings")
        self.target_texts = [target_text] if isinstance(target_text, str) else [str(v) for v in target_text]
        self.target_text = self.target_texts[0] if self.target_texts else ""
        self.condition, self.legacy_mode = normalize_condition(condition, event)
        self.event = event
        self.confirm_frames = confirm_frames
        self.cooldown_ms = cooldown_ms
        self.min_absent_duration_ms = min_absent_duration_ms
        self.observer = ObservationEngine(observation_config)
        self.memory = condition_memory if condition_memory is not None else {}
        self.state = "DISARMED"
        self._present_count = 0
        self._absent_count = 0
        self._absent_started_at = None
        self._last_trigger_at = None
        self._last_trigger_wall_time = None
        self._last_observation = None
        self.has_ever_been_seen = False
        self._pending_state = None
        self._pending_count = 0
        self._pending_started_at = None
        self._pending_last_valid_at = None
        self._condition_events = []
        self._disappear_burst_id = None
        self._disappear_burst_started_at = None
        self._disappear_evidence = []
        self._disappear_valid_samples = 0
        self._disappear_unknown_count = 0
        self._disappear_burst_context = None
        self._last_accepted_capture_id = None
        self._last_accepted_captured_monotonic = None
        self._confirmed_present_template = None
        self._present_template_candidates = []
        self._replacement_candidates = []

    def update(self, observation, now=None):
        if isinstance(observation, str):
            observation = self.observer.observe(self.target_texts, observation, image=None)
        if not isinstance(observation, ObservationResult):
            raise TypeError("TextTrigger.update requires ObservationResult")
        now = time.monotonic() if now is None else now
        self._condition_events = []
        previous = self.state
        if self.legacy_mode == LEGACY_APPEAR:
            if observation.state == "INVALID":
                return self._result(False, None, previous != self.state)
            return self._update_legacy_appear(observation, now, previous)
        if self.legacy_mode == LEGACY_DISAPPEAR:
            if observation.state == "INVALID":
                return self._result(False, None, previous != self.state)
            return self._update_legacy_disappear(observation, now, previous)
        if (
            self.condition["mode"] == "edge"
            and self.condition["desired_state"] == "absent"
        ):
            observation = self._fuse_edge_absent_observation(observation, now)
            self._last_observation = observation
            return self._update_edge_absent(observation, now, previous)
        self._last_observation = observation
        return self._update_condition(observation, now, previous)

    @staticmethod
    def classify_observation(observation):
        """Return the already fused class, or conservative OCR-only presence."""
        if observation.fused_classification:
            return observation.fused_classification
        if (
            not observation.observation_valid
            or observation.state == "INVALID"
            or observation.reason
            in {
                "unreadable_region",
                "near_black",
                "near_white",
                "near_uniform",
                "invalid_crop",
                "stale_observation",
            }
        ):
            return "UNKNOWN"
        similarity = float(observation.text_similarity or 0.0)
        if observation.exact_match or similarity >= 0.85:
            return "PRESENT_STRONG"
        if similarity >= 0.50:
            return "PRESENT_LIKELY"
        if not (observation.recognized_text or "").strip():
            return "UNKNOWN"
        # Absence authorization is never OCR-only.
        return "UNKNOWN"

    def _fuse_edge_absent_observation(self, observation, now):
        self._invalidate_template_if_identity_changed(observation)
        template = self._confirmed_present_template
        template_score = None
        if (
            template is not None
            and observation.roi_valid
            and observation.visual_frame is not None
        ):
            template_score = compare_visual_frame(
                template["frame"], observation.visual_frame
            )
        visual_classification = classify_visual(template_score)
        similarity = float(observation.text_similarity or 0.0)
        ocr_strong = bool(
            observation.exact_match or similarity >= 0.85
        )
        ocr_likely = similarity >= 0.50
        evidence_sources = []

        if not observation.roi_valid:
            fused = "UNKNOWN"
            evidence_sources.append(
                observation.roi_invalid_reason or "roi_invalid"
            )
        elif (
            not observation.observation_valid
            or observation.state == "INVALID"
            or observation.readability_score <= 0.0
        ):
            fused = "UNKNOWN"
            evidence_sources.append("observation_invalid")
        elif ocr_strong:
            fused = "PRESENT_STRONG"
            evidence_sources.append("ocr_present_strong")
        elif ocr_likely:
            fused = "PRESENT_LIKELY"
            evidence_sources.append("ocr_present_likely")
        elif (
            visual_classification == "VISUAL_PRESENT_STRONG"
        ):
            fused = "PRESENT_STRONG"
            evidence_sources.append("visual_present_strong")
        elif (
            visual_classification == "VISUAL_PRESENT_LIKELY"
        ):
            fused = "PRESENT_LIKELY"
            evidence_sources.append("visual_present_likely")
        elif (
            template is not None
            and visual_classification == "VISUAL_ABSENT_EVIDENCE"
            and similarity < 0.50
        ):
            fused = "ABSENT_STRONG"
            evidence_sources.append("visual_absent_evidence")
        else:
            fused = "UNKNOWN"
            evidence_sources.append(
                "template_unavailable"
                if template is None
                else "visual_ambiguous"
            )

        if fused in {"PRESENT_STRONG", "PRESENT_LIKELY"}:
            self._replacement_candidates.clear()
        elif fused == "ABSENT_STRONG":
            replacement = self._normalized_replacement_text(observation)
            if replacement:
                self._replacement_candidates.append(
                    (
                        observation.capture_id,
                        observation.captured_monotonic,
                        replacement,
                    )
                )
                self._replacement_candidates = (
                    self._replacement_candidates[-3:]
                )
                if (
                    sum(
                        item[2] == replacement
                        for item in self._replacement_candidates
                    )
                    >= 2
                ):
                    evidence_sources.append("replacement_text_2_of_3")

        return replace(
            observation,
            template_available=self._confirmed_present_template is not None,
            template_score=template_score,
            visual_classification=visual_classification,
            fused_classification=fused,
            evidence_sources=tuple(evidence_sources),
        )

    @staticmethod
    def _normalized_replacement_text(observation):
        value = "".join(
            (observation.recognized_text or "").split()
        ).casefold()
        if not value or observation.text_similarity >= 0.50:
            return None
        return value

    def _record_present_template_candidate(self, observation, now):
        required = (
            observation.visual_frame,
            observation.capture_id,
            observation.captured_monotonic,
            observation.session_id,
            observation.generation,
            observation.root_hwnd,
            observation.run_id,
            observation.cycle,
            observation.trigger_id,
            observation.roi_revision,
            observation.client_size,
        )
        if any(value is None for value in required):
            return
        identity = self._visual_identity(observation)
        if (
            self._present_template_candidates
            and self._present_template_candidates[-1]["identity"]
            != identity
        ):
            self._present_template_candidates.clear()
        if any(
            item["capture_id"] == observation.capture_id
            for item in self._present_template_candidates
        ):
            return
        self._present_template_candidates.append(
            {
                "identity": identity,
                "capture_id": observation.capture_id,
                "captured_monotonic": observation.captured_monotonic,
                "frame": observation.visual_frame.copy(),
                "client_size": tuple(observation.client_size),
            }
        )
        self._present_template_candidates = (
            self._present_template_candidates[-3:]
        )
        matching = [
            item
            for item in self._present_template_candidates
            if item["identity"] == identity
        ]
        # An exact/strong OCR observation is already an independent positive
        # signal. Keep one fixed template for the armed session; never drift it
        # by updating on later frames.
        if len(matching) < 1:
            return
        latest = matching[-1]
        self._confirmed_present_template = {
            "frame": latest["frame"],
            "session_id": observation.session_id,
            "generation": observation.generation,
            "root_hwnd": observation.root_hwnd,
            "run_id": observation.run_id,
            "cycle": observation.cycle,
            "trigger_id": observation.trigger_id,
            "roi_revision": observation.roi_revision,
            "client_size": tuple(observation.client_size),
            "capture_id": latest["capture_id"],
            "captured_monotonic": latest["captured_monotonic"],
            "template_width": int(latest["frame"].shape[1]),
            "template_height": int(latest["frame"].shape[0]),
        }
        self._present_template_candidates.clear()
        self._event(
            "PRESENT_TEMPLATE_CREATED",
            **self._template_log_metadata(),
        )

    def _invalidate_template_if_identity_changed(self, observation):
        template = self._confirmed_present_template
        if template is None:
            return
        identity_values = (
            observation.session_id,
            observation.generation,
            observation.root_hwnd,
            observation.run_id,
            observation.cycle,
            observation.trigger_id,
            observation.roi_revision,
        )
        if any(value is None for value in identity_values):
            return
        reason = None
        if observation.session_id != template["session_id"]:
            reason = "session_changed"
        elif observation.generation != template["generation"]:
            reason = "generation_changed"
        elif observation.root_hwnd != template["root_hwnd"]:
            reason = "root_hwnd_changed"
        elif observation.run_id != template["run_id"]:
            reason = "run_changed"
        elif observation.cycle != template["cycle"]:
            reason = "cycle_changed"
        elif observation.trigger_id != template["trigger_id"]:
            reason = "trigger_changed"
        elif observation.roi_revision != template["roi_revision"]:
            reason = "roi_revision_changed"
        elif self._client_aspect_changed(
            template["client_size"], observation.client_size
        ):
            reason = "client_aspect_changed"
        if reason:
            self._invalidate_present_template(reason)

    @staticmethod
    def _client_aspect_changed(expected, actual):
        if not expected or not actual or min(*expected, *actual) <= 0:
            return True
        expected_ratio = expected[0] / expected[1]
        actual_ratio = actual[0] / actual[1]
        return abs(actual_ratio / expected_ratio - 1.0) > 0.05

    @staticmethod
    def _visual_identity(observation):
        return (
            observation.session_id,
            observation.generation,
            observation.root_hwnd,
            observation.run_id,
            observation.cycle,
            observation.trigger_id,
            observation.roi_revision,
        )

    def _invalidate_present_template(self, reason):
        if self._confirmed_present_template is None:
            self._present_template_candidates.clear()
            return
        metadata = self._template_log_metadata()
        self._confirmed_present_template = None
        self._present_template_candidates.clear()
        self._replacement_candidates.clear()
        self._event(
            "PRESENT_TEMPLATE_INVALIDATED",
            reason=reason,
            **metadata,
        )

    def _template_log_metadata(self):
        template = self._confirmed_present_template or {}
        return {
            key: template.get(key)
            for key in (
                "session_id",
                "generation",
                "root_hwnd",
                "run_id",
                "cycle",
                "trigger_id",
                "roi_revision",
                "client_size",
                "capture_id",
                "captured_monotonic",
                "template_width",
                "template_height",
            )
        }

    def is_disappear_burst_active(self):
        return self._disappear_burst_id is not None

    @property
    def disappear_burst_id(self):
        return self._disappear_burst_id

    def cancel_disappear_burst(self, reason="runtime_stopped", now=None):
        """Cancel pending absence evidence without changing the edge latch."""
        self._condition_events = []
        if self.is_disappear_burst_active():
            self._cancel_disappear_burst(
                reason, time.monotonic() if now is None else now
            )
        return tuple(self._condition_events)

    def _update_edge_absent(self, observation, now, previous):
        classification = self.classify_observation(observation)
        freshness_reason = self._observation_freshness_reason(
            observation, classification, now
        )
        if freshness_reason:
            if (
                freshness_reason
                in {
                    "target_identity_changed",
                    "target_identity_unavailable",
                }
                and self.is_disappear_burst_active()
            ):
                self._cancel_disappear_burst(
                    freshness_reason, now
                )
            if freshness_reason == "target_identity_changed":
                self._invalidate_present_template(
                    "target_identity_changed"
                )
            self._event(
                "DISAPPEAR_EVIDENCE_IGNORED",
                burst_id=self._disappear_burst_id,
                observation_id=observation.observation_id,
                classification=classification,
                similarity=observation.text_similarity,
                observation_valid=observation.observation_valid,
                reason=freshness_reason,
                roi_revision=observation.roi_revision,
                roi_valid=observation.roi_valid,
                template_available=observation.template_available,
                template_score=observation.template_score,
                visual_classification=observation.visual_classification,
                ocr_similarity=observation.text_similarity,
                fused_classification=classification,
                evidence_sources=observation.evidence_sources,
                capture_id=observation.capture_id,
                run_id=observation.run_id,
                cycle=observation.cycle,
                trigger_id=observation.trigger_id,
                session_id=observation.session_id,
                generation=observation.generation,
                root_hwnd=observation.root_hwnd,
            )
            return self._result(False, None, previous != self.state)

        self._remember_observation_order(observation)
        if self._disappear_burst_expired(now):
            self._cancel_disappear_burst("window_expired", now)

        if classification == "PRESENT_STRONG":
            if (
                self._confirmed_present_template is None
                and (
                    observation.exact_match
                    or observation.text_similarity >= 0.85
                )
                and not self.is_disappear_burst_active()
            ):
                self._record_present_template_candidate(observation, now)
                if self._confirmed_present_template is not None:
                    template_score = compare_visual_frame(
                        self._confirmed_present_template["frame"],
                        observation.visual_frame,
                    )
                    observation = replace(
                        observation,
                        template_available=True,
                        template_score=template_score,
                        visual_classification=classify_visual(
                            template_score
                        ),
                        evidence_sources=(
                            *observation.evidence_sources,
                            "present_template_created",
                        ),
                    )
                    self._last_observation = observation
            was_latched = bool(self.memory.get("latched"))
            if self.is_disappear_burst_active():
                self._cancel_disappear_burst("present_strong", now)
            already_armed = bool(
                self.memory.get("armed") and not was_latched
            )
            self._present_count = (
                self.confirm_frames
                if already_armed
                else self._present_count + 1
            )
            self._reset_absence()
            if self._confirmed_present_template is None:
                self.state = "PRESENT_CONFIRMING"
                return self._result(
                    False, None, previous != self.state
                )
            if self._present_count >= self.confirm_frames:
                first_present_baseline = (
                    self.memory.get("baseline_state") != "PRESENT"
                    or not self.memory.get("armed")
                )
                self.memory.update(
                    baseline_state="PRESENT",
                    armed=True,
                    latched=False,
                    session_id=observation.session_id,
                    generation=observation.generation,
                    root_hwnd=observation.root_hwnd,
                    roi_revision=observation.roi_revision,
                    run_id=observation.run_id,
                    cycle=observation.cycle,
                    trigger_id=observation.trigger_id,
                )
                self.state = "ARMED_PRESENT"
                if was_latched:
                    self._event(
                        "TRIGGER_REARMED",
                        confirmed_opposite_state="PRESENT",
                        previous_latched_state="ABSENT",
                    )
                elif first_present_baseline:
                    self._event(
                        "DISAPPEAR_BASELINE_CONFIRMED",
                        baseline_state="PRESENT",
                    )
            else:
                self.state = "PRESENT_CONFIRMING"
            return self._result(False, None, previous != self.state)

        self._present_count = 0
        if classification == "PRESENT_LIKELY":
            if self.is_disappear_burst_active():
                self._cancel_disappear_burst("present_likely", now)
            self._reset_absence()
            self.state = (
                "ARMED_PRESENT"
                if self.memory.get("armed")
                else "PRESENT_LIKELY"
            )
            return self._result(False, None, previous != self.state)

        if classification == "UNKNOWN":
            if self.is_disappear_burst_active():
                self._disappear_unknown_count += 1
                self._event(
                    "DISAPPEAR_EVIDENCE_IGNORED",
                    burst_id=self._disappear_burst_id,
                    observation_id=observation.observation_id,
                    classification=classification,
                    similarity=observation.text_similarity,
                    observation_valid=observation.observation_valid,
                    reason=observation.reason or "unknown",
                    roi_revision=observation.roi_revision,
                    roi_valid=observation.roi_valid,
                    template_available=observation.template_available,
                    template_score=observation.template_score,
                    visual_classification=observation.visual_classification,
                    fused_classification=classification,
                    evidence_sources=observation.evidence_sources,
                    capture_id=observation.capture_id,
                    run_id=observation.run_id,
                    cycle=observation.cycle,
                    trigger_id=observation.trigger_id,
                    session_id=observation.session_id,
                    generation=observation.generation,
                    root_hwnd=observation.root_hwnd,
                )
                if self._disappear_unknown_count >= 2:
                    self._cancel_disappear_burst(
                        "consecutive_unknown", now
                    )
            return self._result(False, None, previous != self.state)

        # ABSENT_STRONG never arms an initially absent trigger.
        if not self.memory.get("armed") or self.memory.get("latched"):
            if self.memory.get("baseline_state") is None:
                self.memory["baseline_state"] = "ABSENT"
                self._event("TRIGGER_BASELINE_SET", baseline_state="ABSENT")
            self.state = (
                "LATCHED_ABSENT"
                if self.memory.get("latched")
                else "CONFIRMED_ABSENT"
            )
            return self._result(False, None, previous != self.state)

        if not self.is_disappear_burst_active():
            self._start_disappear_burst(observation, now)

        self._disappear_unknown_count = 0
        captured = float(observation.captured_monotonic)
        self._disappear_evidence.append(
            (captured, observation.capture_id, observation.observation_id)
        )
        self._disappear_evidence = self._disappear_evidence[-5:]
        self._disappear_valid_samples += 1
        elapsed_ms = int((now - self._disappear_burst_started_at) * 1000)
        span_ms = int(
            (
                self._disappear_evidence[-1][0]
                - self._disappear_evidence[0][0]
            )
            * 1000
        )
        self.state = "DISAPPEAR_VERIFY_BURST"
        self._event(
            "DISAPPEAR_EVIDENCE_ACCEPTED",
            burst_id=self._disappear_burst_id,
            observation_id=observation.observation_id,
            classification=classification,
            similarity=observation.text_similarity,
            observation_valid=observation.observation_valid,
            evidence_count=len(self._disappear_evidence),
            valid_sample_count=self._disappear_valid_samples,
            elapsed_ms=min(elapsed_ms, 1800),
            roi_revision=observation.roi_revision,
            roi_valid=observation.roi_valid,
            template_available=observation.template_available,
            template_score=observation.template_score,
            visual_classification=observation.visual_classification,
            ocr_similarity=observation.text_similarity,
            fused_classification=classification,
            evidence_sources=observation.evidence_sources,
            capture_id=observation.capture_id,
            run_id=observation.run_id,
            cycle=observation.cycle,
            trigger_id=observation.trigger_id,
            session_id=observation.session_id,
            generation=observation.generation,
            root_hwnd=observation.root_hwnd,
        )
        replacement_fast_path = (
            "replacement_text_2_of_3" in observation.evidence_sources
        )
        required_evidence = 2 if replacement_fast_path else 4
        required_span_ms = 350 if replacement_fast_path else 700
        if (
            len(self._disappear_evidence) >= required_evidence
            and span_ms >= required_span_ms
            and elapsed_ms <= 1800
        ):
            burst_id = self._disappear_burst_id
            evidence_count = len(self._disappear_evidence)
            self.memory.update(
                baseline_state="ABSENT",
                armed=False,
                latched=True,
                latched_state="ABSENT",
            )
            self._clear_disappear_burst()
            self.state = "LATCHED_ABSENT"
            self._event(
                "DISAPPEAR_CONFIRMED",
                burst_id=burst_id,
                evidence_count=evidence_count,
                evidence_span_ms=span_ms,
                elapsed_ms=elapsed_ms,
                confirmation_reason=(
                    "stable_replacement_text"
                    if replacement_fast_path
                    else "visual_absence_quorum"
                ),
            )
            self._event(
                "TRIGGER_LATCHED",
                latched_state="ABSENT",
                rearm_requires="PRESENT",
            )
            self._invalidate_present_template(
                "disappear_confirmed"
            )
            return self._match(
                now, previous, "bounded_disappear_confirmation", latch=True
            )
        return self._result(False, None, previous != self.state)

    def _observation_freshness_reason(
        self, observation, classification, now
    ):
        if classification == "ABSENT_STRONG":
            if not observation.roi_valid:
                return "invalid_roi"
            if not observation.template_available:
                return "template_unavailable"
            required = (
                observation.observation_id,
                observation.capture_id,
                observation.captured_monotonic,
                observation.session_id,
                observation.generation,
                observation.root_hwnd,
                observation.roi_revision,
                observation.client_size,
                observation.template_score,
                observation.run_id,
                observation.cycle,
                observation.trigger_id,
            )
            if any(value is None for value in required):
                return "missing_freshness_metadata"
        if (
            observation.capture_id is not None
            and observation.capture_id == self._last_accepted_capture_id
        ):
            return "duplicate_capture"
        if (
            observation.captured_monotonic is not None
            and self._last_accepted_captured_monotonic is not None
            and observation.captured_monotonic
            <= self._last_accepted_captured_monotonic
        ):
            return "out_of_order_capture"
        if (
            observation.captured_monotonic is not None
            and now - observation.captured_monotonic > 1.8
        ):
            return "stale_observation"
        if (
            self.is_disappear_burst_active()
            and observation.burst_id is not None
            and observation.burst_id != self._disappear_burst_id
        ):
            return "stale_burst"
        if (
            self.is_disappear_burst_active()
            and self._disappear_burst_context
            and (
                observation.run_id,
                observation.cycle,
                observation.trigger_id,
            )
            != self._disappear_burst_context
        ):
            return "stale_run_context"
        expected = (
            self.memory.get("session_id"),
            self.memory.get("generation"),
            self.memory.get("root_hwnd"),
            self.memory.get("roi_revision"),
        )
        actual = (
            observation.session_id,
            observation.generation,
            observation.root_hwnd,
            observation.roi_revision,
        )
        if any(
            expected_value is not None and actual_value is None
            for expected_value, actual_value in zip(expected, actual)
        ):
            return "target_identity_unavailable"
        for expected_value, actual_value in zip(expected, actual):
            if (
                expected_value is not None
                and actual_value != expected_value
            ):
                return "target_identity_changed"
        return None

    def _remember_observation_order(self, observation):
        if observation.capture_id is not None:
            self._last_accepted_capture_id = observation.capture_id
        if observation.captured_monotonic is not None:
            self._last_accepted_captured_monotonic = (
                observation.captured_monotonic
            )

    def _start_disappear_burst(self, observation, now):
        self._disappear_burst_id = f"disappear-{uuid4().hex}"
        self._disappear_burst_started_at = now
        self._disappear_evidence = []
        self._disappear_valid_samples = 0
        self._disappear_unknown_count = 0
        self._disappear_burst_context = (
            observation.run_id,
            observation.cycle,
            observation.trigger_id,
        )
        self.memory.update(
            session_id=observation.session_id,
            generation=observation.generation,
            root_hwnd=observation.root_hwnd,
            roi_revision=observation.roi_revision,
            run_id=observation.run_id,
            cycle=observation.cycle,
            trigger_id=observation.trigger_id,
        )
        self._event(
            "DISAPPEAR_BURST_STARTED",
            burst_id=self._disappear_burst_id,
            observation_id=observation.observation_id,
            maximum_ms=1800,
            interval_ms=175,
            roi_revision=observation.roi_revision,
            roi_valid=observation.roi_valid,
            template_available=observation.template_available,
            template_score=observation.template_score,
            visual_classification=observation.visual_classification,
            fused_classification=observation.fused_classification,
            evidence_sources=observation.evidence_sources,
            capture_id=observation.capture_id,
            run_id=observation.run_id,
            cycle=observation.cycle,
            trigger_id=observation.trigger_id,
            session_id=observation.session_id,
            generation=observation.generation,
            root_hwnd=observation.root_hwnd,
        )

    def _disappear_burst_expired(self, now):
        return bool(
            self._disappear_burst_started_at is not None
            and now - self._disappear_burst_started_at > 1.8
        )

    def _cancel_disappear_burst(self, reason, now):
        if not self.is_disappear_burst_active():
            return
        burst_id = self._disappear_burst_id
        elapsed_ms = int((now - self._disappear_burst_started_at) * 1000)
        evidence_count = len(self._disappear_evidence)
        self._clear_disappear_burst()
        self.state = "ARMED_PRESENT"
        self._event(
            "DISAPPEAR_BURST_CANCELLED",
            burst_id=burst_id,
            reason=reason,
            evidence_count=evidence_count,
            elapsed_ms=min(elapsed_ms, 1800),
        )

    def _clear_disappear_burst(self):
        self._disappear_burst_id = None
        self._disappear_burst_started_at = None
        self._disappear_evidence = []
        self._disappear_valid_samples = 0
        self._disappear_unknown_count = 0
        self._disappear_burst_context = None

    # Exact legacy paths intentionally remain separate: existing user JSON
    # must not acquire new edge semantics just because the new enum exists.
    def _update_legacy_appear(self, observation, now, previous):
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

    def _update_legacy_disappear(self, observation, now, previous):
        if observation.state == "PRESENT":
            self.has_ever_been_seen = True
            self._present_count += 1
            self._reset_absence()
            self.state = "ARMED_PRESENT" if self._present_count >= self.confirm_frames else "PRESENT_CONFIRMING"
            return self._result(False, None, previous != self.state)
        if observation.state == "UNCERTAIN":
            self._reset_absence()
            if self.state in {"ARMED_PRESENT", "ABSENT_CONFIRMING", "TRIGGERED"}:
                self.state = "ARMED_PRESENT"
            return self._result(False, None, previous != self.state)
        self._present_count = 0
        if self.state not in {"ARMED_PRESENT", "ABSENT_CONFIRMING", "TRIGGERED"}:
            return self._result(False, None, previous != self.state)
        if self._absent_count == 0:
            self._absent_started_at = now
        self._absent_count += 1
        self.state = "ABSENT_CONFIRMING"
        if self._absent_count >= self.confirm_frames and self._absent_duration_ms(now) >= self.min_absent_duration_ms and not self._in_cooldown(now):
            self.state = "TRIGGERED"
            self._mark_trigger(now)
            self._reset_absence()
            return self._result(True, "on_text_disappear", previous != self.state)
        return self._result(False, None, previous != self.state)

    def _update_condition(self, observation, now, previous):
        # A latched state condition keeps polling for a *qualified opposite*
        # transition.  It deliberately does not treat a permanently desired
        # state as a failed transition attempt.
        if self.condition["mode"] == "state" and self.memory.get("latched"):
            return self._update_latched_state_tracking(observation, now, previous)
        # INVALID freezes the candidate timer; UNCERTAIN breaks only the
        # unconfirmed candidate and never changes a confirmed baseline/latch.
        if observation.state == "INVALID":
            self._pending_last_valid_at = None
            return self._result(False, None, previous != self.state)
        if observation.state == "UNCERTAIN":
            self._reset_pending()
            return self._result(False, None, previous != self.state)
        confirmed = self._advance_confirmation(observation.state, now)
        if not confirmed:
            self.state = f"{observation.state}_CONFIRMING"
            return self._result(False, None, previous != self.state)
        self.state = f"CONFIRMED_{observation.state}"
        return self._consume_confirmed_state(observation.state, now, previous)

    def _update_latched_state_tracking(self, observation, now, previous):
        desired = self.condition["desired_state"].upper()
        opposite = "PRESENT" if desired == "ABSENT" else "ABSENT"
        direction = f"{opposite}_TO_{desired}"

        if self.memory.get("recovery_pending"):
            return self._update_static_recovery(observation, now, previous, desired, opposite, direction)

        attempt_active = bool(self.memory.get("transition_attempt_active"))
        if not attempt_active:
            if observation.state == opposite:
                self.memory["transition_attempt_active"] = True
                self.memory["transition_candidate_state"] = opposite
                self.memory["transition_tracking_mode"] = f"TRACK_{direction}"
                self.memory["transition_attempt_index"] = int(self.memory.get("transition_attempt_index", 0)) + 1
                self.memory["transition_interrupt_count"] = 0
                self.memory["transition_result"] = "FAIL"
                self._event("TRIGGER_TRANSITION_TRACK_STARTED", direction=direction, candidate_state=opposite, attempt_index=self.memory["transition_attempt_index"], reason="opposite_observation")
                self._reset_pending()
            else:
                # Permanently desired observations must be inert: no attempt,
                # no lost episode, no recovery token.
                return self._result(False, None, previous != self.state)

        if observation.state == opposite:
            confirmed = self._advance_confirmation(opposite, now)
            self.memory["transition_interrupt_count"] = 0
            if confirmed:
                self._normal_state_rearm(opposite, direction)
            return self._result(False, None, previous != self.state)

        if observation.state == desired:
            confirmed = self._advance_confirmation(desired, now)
            if confirmed:
                self._record_lost(now, previous, direction, "returned_to_desired_state")
            return self._result(False, None, previous != self.state)

        # A near-match is never allowed to manufacture a static-recovery
        # opportunity. It remains inert until a confirmed opposite state.
        if observation.state == "UNCERTAIN" and desired == "ABSENT" and observation.reason == "near_match":
            return self._result(False, None, previous != self.state)
        interruptions = int(self.memory.get("transition_interrupt_count", 0)) + 1
        self.memory["transition_interrupt_count"] = interruptions
        if interruptions >= max(2, self.confirm_frames):
            self._record_lost(now, previous, direction, f"interrupted_{observation.state.lower()}")
        return self._result(False, None, previous != self.state)

    def _normal_state_rearm(self, opposite, direction):
        previous_latched = self.memory.get("latched_state")
        self.memory.update(
            latched=False,
            rearm_state_seen=opposite,
            transition_attempt_active=False,
            transition_candidate_state=None,
            transition_interrupt_count=0,
            transition_result="TRUE",
            transition_tracking_mode=f"TRACK_{direction}",
            lost_episode_count=0,
            recovery_pending=False,
            recovery_token=0,
            recovery_wait_next=False,
        )
        self._reset_pending()
        self._event("TRIGGER_REARMED", confirmed_opposite_state=opposite, previous_latched_state=previous_latched)

    def _record_lost(self, now, previous, direction, reason):
        count = int(self.memory.get("lost_episode_count", 0)) + 1
        threshold = int(self.memory.get("lost_threshold", 2))
        self.memory.update(
            transition_attempt_active=False,
            transition_candidate_state=None,
            transition_interrupt_count=0,
            transition_result="LOST",
            lost_episode_count=count,
            transition_tracking_mode=f"TRACK_{direction}",
            last_recovery_reason=reason,
        )
        self._reset_pending()
        self._event("TRIGGER_TRANSITION_TRACK_LOST", direction=direction, lost_episode_count=count, lost_threshold=threshold, interrupt_reason=reason, candidate_progress=0)
        if count >= threshold:
            self.memory.update(recovery_pending=True, recovery_token=int(self.memory.get("recovery_token", 0)) + 1, recovery_wait_next=True)

    def _update_static_recovery(self, observation, now, previous, desired, opposite, direction):
        if self.memory.pop("recovery_wait_next", False):
            self.memory["transition_tracking_mode"] = f"STATIC_RECOVERY_{desired}"
            self._event("TRIGGER_STATIC_RECOVERY_STARTED", desired_state=desired, recovery_token=self.memory.get("recovery_token", 0), reason="lost_threshold_reached")
            self._reset_pending()
        if observation.state in {"INVALID", "UNCERTAIN"}:
            if observation.state == "UNCERTAIN":
                self._reset_pending()
            return self._result(False, None, previous != self.state)
        if observation.state == opposite:
            self._normal_state_rearm(opposite, direction)
            return self._result(False, None, previous != self.state)
        if not self._advance_confirmation(desired, now):
            return self._result(False, None, previous != self.state)
        # Consume before returning a triggered result. Macro failure therefore
        # cannot reopen this recovery opportunity.
        token = int(self.memory.get("recovery_token", 0))
        lost_before = int(self.memory.get("lost_episode_count", 0))
        self.memory.update(recovery_token=max(0, token - 1), recovery_pending=False,
                           lost_episode_count=0, transition_result="RECOVERED",
                           transition_tracking_mode=f"TRACK_{direction}",
                           recovery_generation=int(self.memory.get("recovery_generation", 0)) + 1)
        self._reset_pending()
        self._event("TRIGGER_STATIC_RECOVERY_MATCHED", desired_state=desired, consumed_token=token, lost_episode_count_before_reset=lost_before)
        return self._match(now, previous, "static_recovery_match", latch=True)

    def _advance_confirmation(self, state, now):
        if self._pending_state != state:
            self._pending_state, self._pending_count = state, 0
            self._pending_started_at = now
        if self._pending_last_valid_at is None:
            # A prior INVALID is excluded from duration rather than counted.
            self._pending_started_at = now if self._pending_count == 0 else self._pending_started_at
        self._pending_count += 1
        self._pending_last_valid_at = now
        duration_ms = int((now - self._pending_started_at) * 1000) if self._pending_started_at is not None else 0
        required_duration = self.min_absent_duration_ms if state == "ABSENT" else 0
        return self._pending_count >= self.confirm_frames and duration_ms >= required_duration

    def _consume_confirmed_state(self, state, now, previous):
        mode, desired = self.condition["mode"], self.condition["desired_state"]
        self.memory["last_confirmed_state"] = state
        if mode == "initial":
            if self.memory.get("initial_resolved"):
                return self._result(False, None, previous != self.state)
            matched = state.lower() == desired
            self.memory.update(initial_resolved=True, first_confirmed_state=state, initial_matched=matched)
            self._event("TRIGGER_INITIAL_STATE_RESOLVED", first_confirmed_state=state, matched=matched)
            if matched:
                return self._match(now, previous, "initial_match", latch=False)
            return self._result(False, None, previous != self.state)
        if mode == "edge":
            baseline = self.memory.get("baseline_state")
            if baseline is None:
                self.memory["baseline_state"] = state
                self.memory["armed"] = state.lower() != desired
                self._event("TRIGGER_BASELINE_SET", baseline_state=state)
                if self.memory["armed"]:
                    self._event("TRIGGER_ARMED", armed_for=desired, reason="opposite_baseline_confirmed")
                return self._result(False, None, previous != self.state)
            if state != baseline:
                self.memory["baseline_state"] = state
                if state.lower() == desired and self.memory.get("armed"):
                    self.memory["armed"] = False
                    return self._match(now, previous, "transition_match", latch=False)
                if state.lower() != desired:
                    self.memory["armed"] = True
                    self._event("TRIGGER_ARMED", armed_for=desired, reason="opposite_state_confirmed")
            return self._result(False, None, previous != self.state)
        # state mode: latch on match before cooldown decision, then require the
        # opposite confirmed state to rearm even across a cycle restart.
        latched = bool(self.memory.get("latched"))
        if state.lower() == desired:
            if latched:
                return self._result(False, None, previous != self.state)
            direction = "PRESENT_TO_ABSENT" if desired == "absent" else "ABSENT_TO_PRESENT"
            self.memory.update(latched=True, latched_state=state,
                               transition_tracking_mode=f"TRACK_{direction}",
                               transition_direction=direction,
                               transition_attempt_active=False,
                               transition_result=None,
                               lost_episode_count=0,
                               lost_threshold=2,
                               recovery_pending=False,
                               recovery_token=0,
                               recovery_wait_next=False)
            self._event("TRIGGER_LATCHED", latched_state=state, rearm_requires="PRESENT" if desired == "absent" else "ABSENT")
            return self._match(now, previous, "state_match", latch=True)
        if latched:
            self.memory["latched"] = False
            self.memory["rearm_state_seen"] = state
            self._event("TRIGGER_REARMED", confirmed_opposite_state=state, previous_latched_state=self.memory.get("latched_state"))
        return self._result(False, None, previous != self.state)

    def _match(self, now, previous, reason, latch):
        self._event("TRIGGER_CONDITION_MATCHED", condition=self.condition, trigger_reason=reason)
        if self._in_cooldown(now):
            self._event("TRIGGER_SUPPRESSED_BY_COOLDOWN", remaining_cooldown_ms=self._cooldown_remaining(now), latched=latch)
            return self._result(False, None, previous != self.state)
        self._mark_trigger(now)
        return self._result(True, f"on_text_{self.condition['mode']}_{self.condition['desired_state']}", previous != self.state)

    def _event(self, name, **data):
        self._condition_events.append({"event": name, "data": data})

    def _reset_pending(self):
        self._pending_state = self._pending_count = self._pending_started_at = self._pending_last_valid_at = None

    def _reset_absence(self):
        self._absent_count = 0
        self._absent_started_at = None

    def _absent_duration_ms(self, now):
        return 0 if self._absent_started_at is None else int((now - self._absent_started_at) * 1000)

    def _mark_trigger(self, now):
        self._last_trigger_at = now
        self._last_trigger_wall_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _cooldown_remaining(self, now):
        return max(0, self.cooldown_ms - int((now - self._last_trigger_at) * 1000)) if self._last_trigger_at else 0

    def _in_cooldown(self, now):
        return self._cooldown_remaining(now) > 0

    def _result(self, triggered, event_name, changed):
        observation = self._last_observation
        return TriggerResult(triggered, event_name, observation is not None and observation.state == "PRESENT", None, changed, observation, tuple(self._condition_events))

    def get_status_snapshot(self):
        now = time.monotonic()
        observation = self._last_observation
        code = None if self.legacy_mode else next((f"{m}_{d}" for m, d in [(self.condition['mode'], self.condition['desired_state'])]), None)
        burst_active = self.is_disappear_burst_active()
        burst_elapsed_ms = (
            min(
                1800,
                int((now - self._disappear_burst_started_at) * 1000),
            )
            if burst_active
            else 0
        )
        return {
            "stable_present": (
                self.memory.get("last_confirmed_state") == "PRESENT"
                or self.memory.get("baseline_state") == "PRESENT"
            ),
            "candidate_present": observation.state == "PRESENT" if observation else None,
            "candidate_count": (
                min(5, len(self._disappear_evidence))
                if burst_active
                else self._pending_count or 0
            ),
            "confirm_frames": 5 if burst_active else self.confirm_frames,
            "last_present": observation.state == "PRESENT" if observation else None,
            "cooldown_remaining_ms": self._cooldown_remaining(now),
            "last_trigger_time": self._last_trigger_wall_time,
            "observation": observation.as_dict() if observation else None,
            "observation_state": observation.state if observation else None,
            "armed": bool(self.memory.get("armed")),
            "latched": bool(self.memory.get("latched")),
            "state_machine_state": self.state,
            "absent_frames": self._absent_count,
            "absent_duration_ms": self._absent_duration_ms(now),
            "required_absent_duration_ms": self.min_absent_duration_ms,
            "required_frames": self.confirm_frames,
            "condition_mode": self.condition["mode"], "desired_state": self.condition["desired_state"],
            "condition_label": label_for_code(self.legacy_mode or code), "legacy_mode": self.legacy_mode,
            "first_confirmed_state": self.memory.get("first_confirmed_state"),
            "confirmed_state": self.memory.get("last_confirmed_state"),
            "baseline_state": self.memory.get("baseline_state"),
            "rearm_state": self.memory.get("rearm_state_seen"),
            "trigger_reason": self._condition_events[-1]["data"].get("trigger_reason") if self._condition_events else None,
            "last_condition_events": list(self._condition_events),
            "tracking_mode": self.memory.get("transition_tracking_mode"),
            "transition_direction": self.memory.get("transition_direction"),
            "transition_attempt_active": bool(self.memory.get("transition_attempt_active")),
            "transition_result": self.memory.get("transition_result"),
            "lost_episode_count": int(self.memory.get("lost_episode_count", 0)),
            "lost_threshold": int(self.memory.get("lost_threshold", 2)),
            "recovery_pending": bool(self.memory.get("recovery_pending")),
            "recovery_token": int(self.memory.get("recovery_token", 0)),
            "recovery_reason": self.memory.get("last_recovery_reason"),
            "disappear_burst_active": burst_active,
            "disappear_burst_id": self._disappear_burst_id,
            "disappear_evidence_count": len(self._disappear_evidence),
            "disappear_valid_sample_count": self._disappear_valid_samples,
            "disappear_burst_elapsed_ms": burst_elapsed_ms,
            "disappear_burst_maximum_ms": 1800,
            "present_template_available": (
                self._confirmed_present_template is not None
            ),
            "present_template_metadata": (
                self._template_log_metadata()
                if self._confirmed_present_template is not None
                else None
            ),
            "roi_revision": (
                observation.roi_revision if observation else None
            ),
            "roi_valid": (
                observation.roi_valid if observation else False
            ),
            "template_score": (
                observation.template_score if observation else None
            ),
            "visual_classification": (
                observation.visual_classification
                if observation
                else "VISUAL_UNAVAILABLE"
            ),
            "fused_classification": (
                observation.fused_classification if observation else None
            ),
        }
