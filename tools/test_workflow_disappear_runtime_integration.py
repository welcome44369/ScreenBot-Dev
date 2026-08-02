"""End-to-end wiring tests for legacy workflow ``disappear`` triggers."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from app.observation_engine import ObservationResult
from app.player import (
    PlaybackResult,
    PlaybackStartResult,
    PlaybackStartStatus,
    PlaybackStatus,
)
from app.trigger_conditions import normalize_trigger_payload
from app.workflow_resolver import WorkflowResolver
from app.workflow_runner import WorkflowRunner
from app.workflow_store import WorkflowStore
from app.workflow_diagnostics import WorkflowDiagnostics


TARGET = "尋找採集物"
REGION = {
    "x_ratio": 0.1,
    "y_ratio": 0.2,
    "width_ratio": 0.3,
    "height_ratio": 0.4,
}


def _frame(kind):
    result = np.zeros((64, 128), dtype=np.uint8)
    if kind in {"present", "empty", "likely"}:
        result[:, ::8] = 255
    else:
        result[::8, :] = 255
    return result


def _observation(kind, index):
    now = time.monotonic()
    if kind == "present":
        state, exact, similarity, text, valid, reason = (
            "PRESENT", True, 1.0, TARGET, True, "exact_match"
        )
    elif kind == "likely":
        state, exact, similarity, text, valid, reason = (
            "UNCERTAIN", False, 0.75, "尋找採集", True, "near_match"
        )
    elif kind == "empty":
        state, exact, similarity, text, valid, reason = (
            "UNCERTAIN", False, 0.0, "", True, "empty_ocr"
        )
    elif kind == "invalid":
        state, exact, similarity, text, valid, reason = (
            "INVALID", False, 0.0, "", False, "near_black"
        )
    else:
        # Different readable replacement text on every capture exercises the
        # normal 4/5 visual-absence quorum rather than the 2/3 replacement
        # fast path.
        state, exact, similarity, text, valid, reason = (
            "ABSENT", False, 0.05, f"其他文字{index}", True, "low_presence"
        )
    return ObservationResult(
        state=state,
        exact_match=exact,
        text_similarity=similarity,
        readability_score=0.9 if valid else 0.0,
        visual_similarity=None,
        presence_score=similarity,
        observation_valid=valid,
        reason=reason,
        recognized_text=text,
        target_text=TARGET,
        observation_id=f"observation-{index}",
        capture_id=f"capture-{index}",
        captured_monotonic=now,
        session_id="session-a",
        generation=1,
        root_hwnd=100,
        roi_revision="roi-a",
        roi_valid=True,
        normalized_roi=(0.1, 0.2, 0.3, 0.4),
        roi_pixel_rect=(80, 120, 320, 360),
        client_size=(800, 600),
        visual_frame=_frame(kind),
    )


class _SequenceDetector:
    def __init__(self, kinds):
        self.kinds = list(kinds)
        self.index = 0
        self.invalidated = 0

    def observe_text(self, *_args, **_kwargs):
        kind = self.kinds[min(self.index, len(self.kinds) - 1)]
        self.index += 1
        return _observation(kind, self.index)

    def invalidate_capture_target(self):
        self.invalidated += 1

    def get_capture_runtime_diagnostics(self):
        return {}


class _ScriptStore:
    def load_script(self, _filename):
        return {"actions": []}


class _TriggerStore:
    def load_trigger(self, _trigger_id):
        raise AssertionError("inline integration fixture must not load a resource")


class _Player:
    def __init__(self):
        self.start_times = []
        self.stop_requests = 0
        self._last_result = None

    def is_active(self):
        return False

    def start(self, _script, expected_target=None):
        self.start_times.append(time.monotonic())
        self._last_result = PlaybackResult(
            PlaybackStatus.COMPLETED,
            finished_at_monotonic=time.monotonic(),
        )
        return PlaybackStartResult(PlaybackStartStatus.STARTED)

    def get_last_result(self):
        return self._last_result

    def get_last_authorization(self):
        return None

    def request_stop(self):
        self.stop_requests += 1

    def stop(self):
        self.stop_requests += 1


def _stale_workflow(*, loop=None, event="disappear", condition=None):
    trigger = {
        "type": "text",
        "event": event,
        "text": TARGET,
        "region": dict(REGION),
        "poll_interval_ms": 350,
        "confirm_frames": 1,
        "cooldown_ms": 0,
        "min_absent_duration_ms": 0,
    }
    if condition is not None:
        trigger["condition"] = condition
    result = {
        "id": "workflow-disappear",
        "name": "淨淨蘑菇",
        "steps": [{"id": "collect", "trigger": trigger, "macro": "macro.json"}],
    }
    if loop is not None:
        result["loop"] = loop
    return result


def _wait_for_runner(runner, timeout=8.0):
    deadline = time.monotonic() + timeout
    while runner.is_active() and time.monotonic() < deadline:
        time.sleep(0.02)
    if runner.is_active():
        runner.stop()
        raise AssertionError("WorkflowRunner did not reach a terminal state")


class WorkflowDisappearRuntimeIntegrationTests(unittest.TestCase):
    def test_legacy_disappear_loads_and_runs_as_edge_absent_verifier(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = WorkflowStore(root)
            raw = _stale_workflow(
                condition={"mode": "state", "desired_state": "absent"}
            )
            path = store.workflows_dir / "legacy.json"
            path.write_text(
                json.dumps(raw, ensure_ascii=False), encoding="utf-8"
            )

            loaded = store.load_workflow(path.name)
            self.assertEqual(
                loaded["steps"][0]["trigger"]["condition"],
                {"mode": "edge", "desired_state": "absent"},
            )
            # Load migration is in-memory only.
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))
                ["steps"][0]["trigger"]["condition"]["mode"],
                "state",
            )
            store.save_workflow(loaded, path.name)
            reloaded = store.load_workflow(path.name)
            resolved = WorkflowResolver(
                _TriggerStore(), _ScriptStore()
            ).resolve(reloaded)
            self.assertEqual(
                resolved["steps"][0]["trigger"]["condition"],
                {"mode": "edge", "desired_state": "absent"},
            )

            detector = _SequenceDetector(
                [
                    "absent",  # initial absence establishes no executable edge
                    "present",
                    "empty",
                    "likely",
                    "invalid",
                    "absent",
                    "absent",
                    "absent",
                    "absent",
                    "absent",
                ]
            )
            player = _Player()
            runner = WorkflowRunner(detector, _ScriptStore(), player)
            statuses = []
            original_callback = runner._on_trigger_status

            def capture_status(generation, status):
                statuses.append(status)
                original_callback(generation, status)

            runner._on_trigger_status = capture_status
            runner.load_workflow(resolved)
            started = time.monotonic()
            runner.start()
            _wait_for_runner(runner)

            self.assertEqual(len(player.start_times), 1)
            self.assertLess(player.start_times[0] - started, 4.0)
            trigger_statuses = [
                item.get("trigger_status") or {} for item in statuses
            ]
            events = [
                event
                for status in trigger_statuses
                for event in status.get("last_condition_events", [])
            ]
            event_names = [event["event"] for event in events]
            self.assertIn("PRESENT_TEMPLATE_CREATED", event_names)
            self.assertIn("DISAPPEAR_BURST_STARTED", event_names)
            self.assertIn("DISAPPEAR_CONFIRMED", event_names)
            self.assertEqual(
                sum(name == "DISAPPEAR_CONFIRMED" for name in event_names), 1
            )
            confirmations = [
                event["data"]
                for event in events
                if event["event"] == "DISAPPEAR_CONFIRMED"
            ]
            self.assertLessEqual(confirmations[0]["elapsed_ms"], 1800)
            self.assertGreaterEqual(
                confirmations[0]["evidence_span_ms"], 700
            )
            self.assertTrue(
                any(
                    status.get("condition_mode") == "edge"
                    and status.get("desired_state") == "absent"
                    for status in trigger_statuses
                )
            )
            observations = [
                status["observation"]
                for status in trigger_statuses
                if status.get("observation")
            ]
            fused = [
                item for item in observations
                if item.get("fused_classification") == "ABSENT_STRONG"
            ]
            self.assertTrue(fused)
            self.assertTrue(all(item["template_available"] for item in fused))
            self.assertTrue(all(item["template_score"] is not None for item in fused))
            self.assertTrue(all(item["visual_classification"] for item in fused))
            reasons = [
                event["data"].get("trigger_reason")
                for event in events
                if event["event"] == "TRIGGER_CONDITION_MATCHED"
            ]
            self.assertNotIn("state_match", reasons)
            self.assertTrue(
                any(
                    reason in {
                        "edge_absent_confirmed",
                        "replacement_state_confirmed",
                    }
                    for reason in reasons
                )
            )
            snapshot = runner.get_runtime_snapshot()
            self.assertEqual(snapshot["trigger_count"], 1)
            self.assertEqual(snapshot["macro_count"], 1)
            self.assertIsNotNone(snapshot["last_trigger_time"])

    def test_initial_absent_does_not_dispatch_macro(self):
        detector = _SequenceDetector(["absent"])
        player = _Player()
        runner = WorkflowRunner(detector, _ScriptStore(), player)
        runner.load_workflow(
            {
                **_stale_workflow(condition=None),
                "steps": [{
                    **_stale_workflow(condition=None)["steps"][0],
                    "trigger": normalize_trigger_payload(
                        _stale_workflow(condition=None)["steps"][0]["trigger"],
                        legacy_event_authoritative=True,
                    ),
                }],
            }
        )
        runner.start()
        time.sleep(0.8)
        runner.stop()
        self.assertEqual(player.start_times, [])

    def test_state_absent_remains_state_semantics(self):
        workflow = _stale_workflow(
            event=None,
            condition={"mode": "state", "desired_state": "absent"},
        )
        workflow["steps"][0]["trigger"].pop("event", None)
        player = _Player()
        runner = WorkflowRunner(
            _SequenceDetector(["absent"]), _ScriptStore(), player
        )
        runner.load_workflow(workflow)
        runner.start()
        _wait_for_runner(runner, timeout=3.0)
        self.assertEqual(len(player.start_times), 1)
        self.assertEqual(
            runner.workflow["steps"][0]["trigger"]["condition"]["mode"],
            "state",
        )

    def test_runtime_stop_cancels_pending_burst(self):
        player = _Player()
        runner = WorkflowRunner(
            _SequenceDetector(["present", "absent"]),
            _ScriptStore(),
            player,
        )
        statuses = []
        original_callback = runner._on_trigger_status

        def capture_status(generation, status):
            statuses.append(status)
            original_callback(generation, status)

        runner._on_trigger_status = capture_status
        runner.load_workflow(
            {
                **_stale_workflow(condition=None),
                "steps": [{
                    **_stale_workflow(condition=None)["steps"][0],
                    "trigger": normalize_trigger_payload(
                        _stale_workflow(condition=None)["steps"][0]["trigger"],
                        legacy_event_authoritative=True,
                    ),
                }],
            }
        )
        runner.start()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if any(
                (item.get("trigger_status") or {}).get(
                    "disappear_burst_active"
                )
                for item in statuses
            ):
                break
            time.sleep(0.02)
        self.assertTrue(runner.request_immediate_stop("f8_unlock"))
        _wait_for_runner(runner)
        self.assertEqual(player.start_times, [])

    def test_reappearing_text_rearms_and_next_disappearance_fires_once(self):
        trigger = normalize_trigger_payload(
            _stale_workflow(condition=None)["steps"][0]["trigger"],
            legacy_event_authoritative=True,
        )
        workflow = {
            "name": "two cycles",
            "steps": [{
                "id": "collect",
                "trigger": trigger,
                "macro": "macro.json",
            }],
            "loop": {
                "mode": "max_cycles",
                "restart_step": "collect",
                "max_cycles": 2,
            },
        }
        detector = _SequenceDetector(
            [
                "present",
                "absent", "absent", "absent", "absent", "absent",
                "present",
                "absent", "absent", "absent", "absent", "absent",
            ]
        )
        player = _Player()
        runner = WorkflowRunner(detector, _ScriptStore(), player)
        statuses = []
        original_callback = runner._on_trigger_status

        def capture_status(generation, status):
            statuses.append(status)
            original_callback(generation, status)

        runner._on_trigger_status = capture_status
        runner.load_workflow(workflow)
        runner.start()
        _wait_for_runner(runner, timeout=8.0)

        event_names = [
            event["event"]
            for item in statuses
            for event in (item.get("trigger_status") or {}).get(
                "last_condition_events", []
            )
        ]
        self.assertEqual(len(player.start_times), 2)
        self.assertEqual(event_names.count("DISAPPEAR_CONFIRMED"), 2)
        self.assertEqual(event_names.count("TRIGGER_REARMED"), 1)
        self.assertEqual(event_names.count("DISAPPEAR_BASELINE_CONFIRMED"), 1)
        snapshot = runner.get_runtime_snapshot()
        self.assertEqual(snapshot["trigger_count"], 2)
        self.assertEqual(snapshot["macro_count"], 2)

    def test_runtime_counters_accumulate_across_trigger_generations(self):
        runner = WorkflowRunner(
            _SequenceDetector(["present"]), _ScriptStore(), _Player()
        )
        runner._trigger_runner_generation = 1
        first = {
            "trigger_fire_count": 1,
            "macro_start_count": 1,
            "trigger_status": {"last_trigger_time": "first"},
        }
        runner._on_trigger_status(1, first)
        runner._on_trigger_status(1, first)
        runner._trigger_runner_generation = 2
        second = {
            "trigger_fire_count": 1,
            "macro_start_count": 1,
            "trigger_status": {"last_trigger_time": "second"},
        }
        runner._on_trigger_status(2, second)
        runner.workflow = _stale_workflow(condition=None)
        snapshot = runner.get_runtime_snapshot()
        self.assertEqual(snapshot["trigger_count"], 2)
        self.assertEqual(snapshot["macro_count"], 2)
        self.assertEqual(snapshot["last_trigger_time"], "second")

    def test_confirm_progress_is_capped(self):
        from app.text_trigger import TextTrigger

        trigger = TextTrigger(
            TARGET,
            condition={"mode": "state", "desired_state": "present"},
            confirm_frames=2,
        )
        trigger._pending_count = 3
        status = trigger.get_status_snapshot()
        self.assertEqual(status["candidate_count"], 2)

    def test_workflow_telemetry_uses_aggregate_counts_and_dedupes_transitions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diagnostics = WorkflowDiagnostics(
                temp_dir, runtime_log_background_maintenance=False
            )
            snapshot = {
                "workflow_name": "telemetry",
                "state": "WAIT_TRIGGER",
                "step_id": "collect",
                "step_number": 1,
                "total_steps": 1,
                "trigger_event": "disappear",
                "trigger_text": TARGET,
                "macro": "macro.json",
                "trigger_count": 2,
                "macro_count": 2,
                "last_trigger_time": "2026-08-02 14:00:00",
                "trigger_runtime": {
                    "poll_count": 3,
                    "trigger_fire_count": 1,
                    "macro_start_count": 1,
                    "trigger_status": {
                        "candidate_count": 3,
                        "confirm_frames": 2,
                        "last_condition_events": [{
                            "event": "TRIGGER_ARMED",
                            "data": {"baseline_state": "PRESENT"},
                        }],
                    },
                },
            }
            diagnostics.update_from_snapshot(snapshot)
            diagnostics.update_from_snapshot(snapshot)
            self.assertEqual(diagnostics.trigger_count, 2)
            self.assertEqual(diagnostics.macro_count, 2)
            self.assertEqual(
                diagnostics.last_trigger_time, "2026-08-02 14:00:00"
            )
            self.assertEqual(diagnostics.confirm_progress, "2 / 2")
            armed_lines = [
                item for item in diagnostics.timeline
                if "TRIGGER_ARMED" in item["message"]
            ]
            self.assertEqual(len(armed_lines), 1)


if __name__ == "__main__":
    unittest.main()
