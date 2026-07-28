"""Regression checks for post-macro completion and trigger-runner ownership."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.player import PlaybackResult, PlaybackStartStatus, PlaybackStatus
from app.trigger_runner import TriggerRunner
from app.workflow_diagnostics import WorkflowDiagnostics
from app.workflow_runner import WorkflowRunner, WorkflowState


@dataclass
class _Observation:
    recognized_text: str = "needle"
    state: str = "PRESENT"
    exact_match: bool = True
    text_similarity: float = 1.0
    readability_score: float = 1.0
    presence_score: float = 1.0
    reason: str = "test"


class _Store:
    def load_script(self, _name):
        return {"actions": []}


class _Player:
    def __init__(self):
        self.result = PlaybackResult(PlaybackStatus.COMPLETED, duration_ms=1015)
        self.starts = 0

    def start(self, _script, expected_target=None):
        self.starts += 1
        return SimpleNamespace(status=PlaybackStartStatus.STARTED, reason=None)

    def is_active(self):
        return False

    def get_last_result(self):
        return self.result

    def get_last_authorization(self):
        return None


class _Detector:
    def __init__(self):
        self.calls = 0

    def observe_text(self, *_args):
        self.calls += 1
        return _Observation()

    def get_capture_runtime_diagnostics(self):
        return {}


class PostMacroTransitionTests(unittest.TestCase):
    def test_completed_playback_keeps_workflow_nonterminal_after_foreground_change(self):
        player = _Player()
        runner = WorkflowRunner(_Detector(), _Store(), player)
        runner.state = WorkflowState.RUNNING_MACRO
        runner._macro_started_monotonic = time.monotonic() - 10

        self.assertTrue(runner._handle_inactive_player_result())

        self.assertFalse(runner.terminal_claimed)
        self.assertNotEqual(runner.finish_reason, "input_blocked")
        self.assertEqual(runner._last_macro_duration_ms, 1015)

    def test_trigger_runner_stops_polling_after_one_successful_fire(self):
        detector = _Detector()
        player = _Player()
        trigger = TriggerRunner(
            detector,
            _Store(),
            player,
            {
                "name": "step",
                "trigger": {
                    "type": "text",
                    "text": "needle",
                    "event": "appear",
                    "region": {
                        "x_ratio": 0,
                        "y_ratio": 0,
                        "width_ratio": 1,
                        "height_ratio": 1,
                    },
                    "poll_interval_ms": 1,
                },
                "macro": "macro.json",
            },
        )
        trigger.text_trigger.update = lambda _observation: SimpleNamespace(
            triggered=True,
            event_name="appear",
            present=True,
            condition_events=[],
        )
        trigger.start()
        trigger._thread.join(timeout=1)

        status = trigger.get_status_snapshot()
        self.assertFalse(trigger.is_active())
        self.assertTrue(status["trigger_fired"])
        self.assertEqual(status["trigger_fire_count"], 1)
        self.assertEqual(status["macro_start_count"], 1)
        self.assertEqual(player.starts, 1)
        self.assertEqual(detector.calls, 1)

    def test_late_trigger_callback_cannot_replace_current_generation(self):
        runner = WorkflowRunner(_Detector(), _Store(), _Player())
        runner.state = WorkflowState.WAIT_TRIGGER
        runner._trigger_runner_generation = 2

        runner._on_trigger_status(1, {"trigger_fired": True})

        self.assertIsNone(runner._latest_trigger_status)

    def test_input_blocked_diagnostics_include_player_and_foreground_source(self):
        diagnostics = WorkflowDiagnostics(PROJECT_ROOT, runtime_log_background_maintenance=False)
        events = []
        diagnostics.add_event = lambda category, message, **kwargs: events.append(
            (category, message, kwargs.get("event"), kwargs.get("data"))
        )

        diagnostics.update_from_snapshot(
            {
                "workflow_name": "test",
                "state": "STOPPED",
                "step_id": "step-1",
                "step_number": 1,
                "total_steps": 1,
                "trigger_runtime": {},
                "input_safety": {
                    "blocked": True,
                    "authorization_code": "TARGET_NOT_FOREGROUND",
                    "input_block_source": "player_per_emission",
                    "action_index": 3,
                    "player_active": False,
                    "player_terminal_status": "input_blocked",
                    "player_finished_at": 10.25,
                    "trigger_runner_state": "stopped",
                    "foreground_pid": 111,
                    "foreground_process_name": "other.exe",
                    "foreground_executable": "C:\\other.exe",
                    "foreground_title": "Other window",
                    "foreground_class": "OtherClass",
                    "foreground_hwnd": 10,
                    "foreground_root_hwnd": 10,
                    "locked_pid": 222,
                    "locked_title": "Target window",
                    "locked_class": "TargetClass",
                    "locked_root_hwnd": 20,
                    "screenbot_pid": 333,
                    "screenbot_root_hwnd": 30,
                    "screenbot_is_foreground": False,
                    "cached_target_visibility": "FOREGROUND",
                    "live_foreground_match": False,
                },
            }
        )

        input_blocked = next(event for event in events if event[2] == "INPUT_BLOCKED")
        event_data = input_blocked[3]
        self.assertEqual(event_data["input_block_source"], "player_per_emission")
        self.assertEqual(event_data["action_index"], 3)
        self.assertEqual(event_data["foreground_process_name"], "other.exe")
        self.assertEqual(event_data["locked_root_hwnd"], 20)
        self.assertFalse(event_data["live_foreground_match"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
