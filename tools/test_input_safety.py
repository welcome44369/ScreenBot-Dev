import unittest
import time
from dataclasses import dataclass
from unittest.mock import patch

from app.input_safety import ExpectedTargetSession, ForegroundInputSafetyGate, InputAuthorizationCode
from app.player import PlaybackResult, PlaybackStartStatus, PlaybackStatus, ScriptPlayer
from app.target_session import TargetConnectionState, TargetVisibilityState
from app.observation_engine import ObservationResult
from app.text_trigger import TextTrigger
from app.trigger_runner import TriggerRunner
from app.workflow_runner import WorkflowRunner, WorkflowState


@dataclass(frozen=True)
class Snap:
    session_id:str='s'; generation:int=1; root_hwnd:int=10; connection_state:object=TargetConnectionState.ATTACHED
    identity_valid:bool=True; identity_strength:str='strong'; visibility_state:object=TargetVisibilityState.FOREGROUND
    current_client_size:tuple=(100,100); client_screen_origin:tuple=(0,0)

class Session:
    def __init__(self,s): self.s=s
    def get_snapshot(self): return self.s
    def refresh(self): return self.s
class Info:
    def __init__(self, hwnd): self.root_hwnd=hwnd
class Tracker:
    def __init__(self, foreground=10): self.foreground=foreground
    def get_foreground_hwnd(self): return self.foreground
    def query_window(self, hwnd): return Info(hwnd)


class WorkflowPlayer:
    def __init__(self, result):
        self.result = result
    def get_last_result(self): return self.result
    def get_last_authorization(self): return None
    def is_active(self): return False
    def request_stop(self): pass
    def stop(self): pass


class WorkflowDetector:
    def invalidate_capture_target(self): pass
    def get_capture_runtime_diagnostics(self): return {}


class BackgroundGate:
    def authorize_foreground_input(self, _expected):
        return type("Auth", (), {"allowed": False, "code": InputAuthorizationCode.TARGET_NOT_FOREGROUND})()


class NoObserveDetector:
    def __init__(self): self.calls=0
    def observe_text(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("suspended step trigger must not observe")
    def get_capture_runtime_diagnostics(self): return {}


class StopObserveDetector(WorkflowDetector):
    def __init__(self): self.calls=0
    def observe_text(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return ObservationResult(
                "ABSENT", False, 0.0, .9, None, .1, True,
                "low_presence", "other", "stop",
            )
        return ObservationResult(
            "PRESENT", True, 1.0, .9, None, .9, True,
            "test", "stop", "stop",
        )

class InputSafetyTests(unittest.TestCase):
    def test_background_is_rejected(self):
        gate=ForegroundInputSafetyGate(Session(Snap()),Tracker(99))
        self.assertEqual(gate.authorize_foreground_input(ExpectedTargetSession('s',1)).code,InputAuthorizationCode.TARGET_NOT_FOREGROUND)

    def test_player_emits_no_input_when_background(self):
        gate=ForegroundInputSafetyGate(Session(Snap()),Tracker(99)); player=ScriptPlayer(Tracker(99),gate)
        with patch('app.player.mouse.click') as click:
            result=player.start({'actions':[{'type':'mouse_click','ratio_x':.5,'ratio_y':.5}]})
        self.assertEqual(result.status,PlaybackStartStatus.INPUT_BLOCKED)
        self.assertEqual(player.get_last_result().status,PlaybackStatus.INPUT_BLOCKED)
        click.assert_not_called()

    def test_foreground_click_completes(self):
        gate=ForegroundInputSafetyGate(Session(Snap()),Tracker(10)); player=ScriptPlayer(Tracker(10),gate)
        with patch('app.player.mouse.move'),patch('app.player.mouse.click') as click:
            result=player.start({'actions':[{'type':'mouse_click','ratio_x':.5,'ratio_y':.5}]}); player._thread.join(1)
        self.assertEqual(result.status,PlaybackStartStatus.STARTED); self.assertEqual(player.get_last_result().status,PlaybackStatus.COMPLETED); click.assert_called_once()

    def test_started_does_not_occupy_terminal_result(self):
        gate=ForegroundInputSafetyGate(Session(Snap()),Tracker(10)); player=ScriptPlayer(Tracker(10),gate)
        with patch('app.player.mouse.move'), patch('app.player.mouse.click'):
            result=player.start({'actions':[{'type':'mouse_click','ratio_x':.5,'ratio_y':.5}]})
            player._thread.join(1)
        self.assertEqual(result.status, PlaybackStartStatus.STARTED)
        self.assertEqual(player.get_last_result().status, PlaybackStatus.COMPLETED)

    def test_move_then_foreground_loss_blocks_click(self):
        tracker=Tracker(10); gate=ForegroundInputSafetyGate(Session(Snap()),tracker); player=ScriptPlayer(tracker,gate)
        def lose_foreground(*_):
            tracker.foreground=99
        with patch('app.player.mouse.move', side_effect=lose_foreground), patch('app.player.mouse.click') as click:
            result=player.start({'actions':[{'type':'mouse_click','ratio_x':.5,'ratio_y':.5}]})
            player._thread.join(1)
        self.assertEqual(result.status, PlaybackStartStatus.STARTED)
        self.assertEqual(player.get_last_result().status, PlaybackStatus.INPUT_BLOCKED)
        click.assert_not_called()

    def test_keyboard_cleanup_releases_held_key_once_after_foreground_loss(self):
        tracker=Tracker(10); gate=ForegroundInputSafetyGate(Session(Snap()),tracker); player=ScriptPlayer(tracker,gate)
        def lose_foreground(*_):
            tracker.foreground=99
        with patch('app.player.keyboard.press', side_effect=lose_foreground), patch('app.player.keyboard.release') as release:
            result=player.start({'actions':[{'type':'key','event':'down','key':'a'},{'type':'noop','delay':.05}]})
            player._thread.join(1)
        self.assertEqual(result.status, PlaybackStartStatus.STARTED)
        self.assertEqual(player.get_last_result().status, PlaybackStatus.INPUT_BLOCKED)
        release.assert_called_once_with('a')
        self.assertFalse(player._pressed_keys)

    def test_mouse_cleanup_releases_held_button_once_after_foreground_loss(self):
        tracker=Tracker(10); gate=ForegroundInputSafetyGate(Session(Snap()),tracker); player=ScriptPlayer(tracker,gate)
        def lose_foreground(*_):
            tracker.foreground=99
        with patch('app.player.mouse.move'), patch('app.player.mouse.press', side_effect=lose_foreground), patch('app.player.mouse.release') as release:
            result=player.start({'actions':[{'type':'mouse_down','button':'left','ratio_x':.5,'ratio_y':.5},{'type':'noop','delay':.05}]})
            player._thread.join(1)
        self.assertEqual(result.status, PlaybackStartStatus.STARTED)
        self.assertEqual(player.get_last_result().status, PlaybackStatus.INPUT_BLOCKED)
        release.assert_called_once_with('left')
        self.assertFalse(player._pressed_mouse_buttons)

    def test_drag_stops_before_first_segment_when_foreground_is_lost(self):
        tracker=Tracker(10); gate=ForegroundInputSafetyGate(Session(Snap()),tracker); player=ScriptPlayer(tracker,gate)
        moves = []
        def record_move(*args):
            moves.append(args)
        def lose_foreground(*_):
            tracker.foreground=99
        with patch('app.player.mouse.move', side_effect=record_move), patch('app.player.mouse.press', side_effect=lose_foreground), patch('app.player.mouse.release') as release:
            result=player.start({'actions':[{'type':'drag','start_x_ratio':.1,'start_y_ratio':.1,'end_x_ratio':.9,'end_y_ratio':.9,'duration_ms':100}]})
            player._thread.join(1)
        self.assertEqual(result.status, PlaybackStartStatus.STARTED)
        self.assertEqual(player.get_last_result().status, PlaybackStatus.INPUT_BLOCKED)
        self.assertEqual(len(moves), 1)
        release.assert_called_once_with('left')

    def test_coordinate_invalid_is_failed_not_input_blocked(self):
        gate=ForegroundInputSafetyGate(Session(Snap()),Tracker(10)); player=ScriptPlayer(Tracker(10),gate)
        result=player.start({'actions':[{'type':'mouse_click','ratio_x':float('inf'),'ratio_y':.5}]})
        player._thread.join(1)
        self.assertEqual(result.status, PlaybackStartStatus.STARTED)
        terminal=player.get_last_result()
        self.assertEqual(terminal.status, PlaybackStatus.FAILED)
        self.assertEqual(terminal.reason, 'coordinate_invalid')

    def _workflow_for_result(self, result):
        runner = WorkflowRunner(WorkflowDetector(), object(), WorkflowPlayer(result))
        runner.workflow = {"name": "Safety", "steps": []}
        runner.state = WorkflowState.RUNNING_MACRO
        return runner

    def test_input_blocked_cannot_advance_or_restart(self):
        runner = self._workflow_for_result(PlaybackResult(PlaybackStatus.INPUT_BLOCKED, "TARGET_NOT_FOREGROUND"))
        self.assertFalse(runner._handle_inactive_player_result())
        self.assertEqual(runner.state, WorkflowState.STOPPED)
        self.assertEqual(runner.finish_reason, "input_blocked")
        self.assertFalse(runner._can_start_step_macro())

    def test_failed_playback_is_workflow_error_not_completion(self):
        runner = self._workflow_for_result(PlaybackResult(PlaybackStatus.FAILED, "coordinate_invalid"))
        self.assertFalse(runner._handle_inactive_player_result())
        self.assertEqual(runner.state, WorkflowState.ERROR)
        self.assertEqual(runner.finish_reason, "runtime_error")

    def test_first_terminal_claimant_wins(self):
        runner = self._workflow_for_result(PlaybackResult(PlaybackStatus.INPUT_BLOCKED, "TARGET_NOT_FOREGROUND"))
        self.assertTrue(runner.request_immediate_stop("input_blocked", {"authorization": "TARGET_NOT_FOREGROUND"}))
        self.assertFalse(runner.request_immediate_stop("manual_stop"))
        self.assertFalse(runner.request_immediate_stop("stop_trigger"))
        self.assertEqual(runner.finish_reason, "input_blocked")

    def test_max_cycle_completion_remains_finished(self):
        runner = self._workflow_for_result(PlaybackResult(PlaybackStatus.COMPLETED))
        runner.workflow = {"name": "Safety", "steps": [{"id": "one"}]}
        runner.loop_mode = "max_cycles"
        runner.max_cycles = 1
        runner._on_cycle_boundary()
        self.assertEqual(runner.state, WorkflowState.FINISHED)
        self.assertEqual(runner.finish_reason, "max_cycles_reached")

    def test_background_wait_suspends_normal_step_observation(self):
        trigger_data = {
            "name": "wait",
            "trigger": {
                "type": "text", "text": "needle", "event": "appear",
                "region": {"x_ratio": 0, "y_ratio": 0, "width_ratio": 1, "height_ratio": 1},
            },
            "macro": "macro.json",
        }
        detector = NoObserveDetector()
        runner = TriggerRunner(detector, object(), WorkflowPlayer(None), trigger_data,
                               input_safety_gate=BackgroundGate())
        runner.start()
        time.sleep(.03)
        runner.stop()
        self.assertEqual(detector.calls, 0)
        status = runner.get_status_snapshot()
        self.assertTrue(status["input_suspended"])
        self.assertEqual(status["poll_count"], 0)
        self.assertEqual(status["trigger_fire_count"], 0)

    def test_global_stop_observes_while_step_input_is_suspended(self):
        detector = StopObserveDetector()
        runner = WorkflowRunner(detector, object(), WorkflowPlayer(None))
        config = {
            "type": "text", "text": "stop", "event": "appear",
            "region": {"x_ratio": 0, "y_ratio": 0, "width_ratio": 1, "height_ratio": 1},
            "confirm_frames": 1, "poll_interval_ms": 1, "cooldown_ms": 0,
        }
        runner.workflow = {"name": "Safety", "steps": []}
        runner.state = WorkflowState.WAIT_TRIGGER
        runner.stop_trigger_enabled = True
        runner.stop_trigger_config = config
        runner._stop_text_trigger = TextTrigger(
            "stop",
            event="appear",
            confirm_frames=1,
            min_absent_duration_ms=0,
        )
        self.assertFalse(runner._check_stop_trigger(force=True))
        self.assertTrue(runner._check_stop_trigger(force=True))
        self.assertEqual(detector.calls, 2)
        self.assertEqual(runner.stop_source, "stop_trigger")

if __name__=='__main__': unittest.main(verbosity=2)
