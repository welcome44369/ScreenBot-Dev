"""Focused Recorder tests for frozen TargetSession event boundaries."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.application import ScreenBotApp
from app.recorder import ActionRecorder, _RawInputEvent
from app.state import AppState
from app.target_boundary import FrozenTargetBoundary, TargetBoundaryFilter


def snapshot(**changes):
    values = {
        "session_id": "recording-session",
        "generation": 3,
        "pid": 6000,
        "process_creation_time": 8080,
        "root_hwnd": 100,
        "client_hwnd": 101,
        "executable_path": r"C:\Target\target.exe",
        "identity_strength": "strong",
        "identity_valid": True,
    }
    values.update(changes)
    return SimpleNamespace(**values)


class Session:
    def __init__(self, current=None):
        self.current = current
        self.listeners = []

    def get_snapshot(self):
        return self.current

    def refresh(self):
        return self.current

    def subscribe(self, listener):
        if listener not in self.listeners:
            self.listeners.append(listener)

    def unsubscribe(self, listener):
        if listener in self.listeners:
            self.listeners.remove(listener)

    def publish(self, event):
        for listener in tuple(self.listeners):
            listener(event, self.current, {})


class Adapter:
    def __init__(self):
        self.foreground = 100
        self.screenbot = 900

    def window_from_point(self, x, _y):
        if x < 0:
            return 0
        if x < 100:
            return 102
        if x < 200:
            return 201
        if x < 300:
            return self.screenbot
        if x < 400:
            return 300
        if x < 500:
            return 400
        return 500

    def root_hwnd(self, hwnd):
        return {100: 100, 101: 100, 102: 100, 201: 201}.get(hwnd, hwnd)

    def owner_hwnd(self, hwnd):
        return 100 if hwnd == 201 else 0

    def pid_for_window(self, hwnd):
        if hwnd == self.screenbot:
            return os.getpid()
        return 6000 if hwnd in {100, 101, 102, 201} else 7000

    def foreground_hwnd(self):
        return self.foreground

    def screen_to_client(self, _hwnd, x, y):
        return int(x) % 100, int(y)

    def client_size(self, _hwnd):
        return 100, 100


class Key:
    def __init__(self, *, char=None, name=None, vk=None):
        self.char = char
        self.name = name
        self.vk = vk


def ready_recorder(*, invalidated=None):
    session = Session(snapshot())
    adapter = Adapter()
    recorder = ActionRecorder(
        SimpleNamespace(target=None),
        settings=None,
        target_session=session,
        boundary_adapter=adapter,
        on_invalidated=invalidated,
    )
    recorder._frozen_boundary = FrozenTargetBoundary.from_snapshot(session.current)
    recorder._boundary_filter = TargetBoundaryFilter(
        session,
        recorder._frozen_boundary,
        adapter=adapter,
        screenbot_pid=os.getpid(),
    )
    recorder._target_hwnd = 101
    recorder._start_time = 0.0
    recorder._last_action_end = 0.0
    recorder._recording = True
    recorder._accepting_events = True
    return recorder, session, adapter


def raw(kind, timestamp, **payload):
    return _RawInputEvent(kind, timestamp, payload)


class RecorderTargetScopeTests(unittest.TestCase):
    def test_target_click_and_client_coordinates_are_recorded(self):
        recorder, _, _ = ready_recorder()
        recorder._normalize(raw("mouse_down", 0.1, x=20, y=30, button="left"))
        recorder._normalize(raw("mouse_up", 0.2, x=22, y=31, button="left"))
        self.assertEqual(len(recorder.actions), 1)
        event = recorder.actions[0]
        self.assertEqual(event["type"], "click")
        self.assertEqual((event["client_x"], event["client_y"]), (20, 30))
        self.assertEqual((event["x_ratio"], event["y_ratio"]), (0.2, 0.3))
        self.assertNotIn("screen_x", event)
        self.assertNotIn("screen_y", event)

    def test_target_child_and_owned_popup_are_accepted(self):
        recorder, _, _ = ready_recorder()
        recorder._normalize(raw("mouse_down", 0.1, x=20, y=20, button="left"))
        recorder._normalize(raw("mouse_up", 0.2, x=21, y=20, button="left"))
        recorder._normalize(raw("mouse_down", 0.3, x=120, y=20, button="left"))
        recorder._normalize(raw("mouse_up", 0.4, x=121, y=20, button="left"))
        self.assertEqual([event["type"] for event in recorder.actions], ["click", "click"])

    def test_screenbot_stop_overlay_other_desktop_and_taskbar_are_excluded(self):
        recorder, _, _ = ready_recorder()
        for x in (220, 320, 420, 520, -1):
            recorder._normalize(raw("mouse_down", 0.1, x=x, y=20, button="left"))
            recorder._normalize(raw("mouse_up", 0.2, x=x, y=20, button="left"))
        self.assertEqual(recorder.actions, [])

    def test_cross_boundary_drag_discards_entire_transaction(self):
        recorder, _, _ = ready_recorder()
        recorder._normalize(raw("mouse_down", 0.1, x=10, y=10, button="left"))
        recorder._normalize(raw("mouse_move", 0.2, x=320, y=20))
        recorder._normalize(raw("mouse_up", 0.3, x=80, y=80, button="left"))
        self.assertEqual(recorder.actions, [])
        self.assertEqual(recorder.get_diagnostics()["open_mouse_button_count"], 0)

    def test_listener_enqueues_moves_only_during_a_physical_gesture(self):
        recorder, _, _ = ready_recorder()
        queued = []
        recorder._enqueue = lambda kind, payload: queued.append((kind, payload))
        recorder._on_mouse_move(10, 10)
        recorder._on_mouse_click(10, 10, "left", True)
        recorder._on_mouse_move(20, 20)
        recorder._on_mouse_click(20, 20, "left", False)
        recorder._on_mouse_move(30, 30)
        self.assertEqual(
            [kind for kind, _payload in queued],
            ["mouse_down", "mouse_move", "mouse_up"],
        )

    def test_generation_change_discards_pending_and_requests_stop(self):
        invalidations = []
        recorder, session, _ = ready_recorder(invalidated=invalidations.append)
        recorder._normalize(raw("mouse_down", 0.1, x=10, y=10, button="left"))
        session.current = snapshot(generation=4)
        recorder._normalize(raw("mouse_move", 0.2, x=20, y=20))
        self.assertEqual(invalidations, ["target_invalidated"])
        self.assertFalse(recorder._accepting_events)
        self.assertEqual(recorder.actions, [])
        self.assertEqual(recorder._mouse_down, {})

    def test_keyboard_commits_only_complete_target_foreground_gesture(self):
        recorder, _, adapter = ready_recorder()
        key = Key(char="a", vk=65)
        recorder._normalize(raw("key_down", 0.1, key=key))
        self.assertEqual(recorder.actions, [])
        recorder._normalize(raw("key_up", 0.2, key=key))
        self.assertEqual(
            [(event["event"], event["key"]) for event in recorder.actions],
            [("down", "a"), ("up", "a")],
        )
        adapter.foreground = 300
        recorder._normalize(raw("key_down", 0.3, key=Key(char="b", vk=66)))
        recorder._normalize(raw("key_up", 0.4, key=Key(char="b", vk=66)))
        self.assertEqual(len(recorder.actions), 2)

    def test_screenbot_foreground_and_control_keys_are_excluded(self):
        recorder, _, adapter = ready_recorder()
        for name, vk in (("f8", 119), ("f9", 120), ("esc", 27)):
            key = Key(name=name, vk=vk)
            recorder._normalize(raw("key_down", 0.1, key=key))
            recorder._normalize(raw("key_up", 0.2, key=key))
        adapter.foreground = adapter.screenbot
        key = Key(char="x", vk=88)
        recorder._normalize(raw("key_down", 0.3, key=key))
        recorder._normalize(raw("key_up", 0.4, key=key))
        self.assertEqual(recorder.actions, [])

    def test_incomplete_key_is_discarded_without_synthetic_key_up(self):
        recorder, _, _ = ready_recorder()
        recorder._normalize(raw("key_down", 0.1, key=Key(char="w", vk=87)))
        script = recorder.stop()
        self.assertEqual(script["events"], [])
        self.assertFalse(any(event.get("synthetic") for event in script["events"]))

    def test_target_clear_stops_acceptance_and_preserves_completed_events(self):
        invalidations = []
        recorder, session, _ = ready_recorder(invalidated=invalidations.append)
        recorder._target_listener_registered = True
        session.subscribe(recorder._on_target_session_event)
        recorder._normalize(raw("mouse_down", 0.1, x=20, y=30, button="left"))
        recorder._normalize(raw("mouse_up", 0.2, x=20, y=30, button="left"))
        session.current = None
        session.publish("TARGET_SESSION_CLEARED")
        self.assertEqual(len(recorder.actions), 1)
        self.assertFalse(recorder._accepting_events)
        self.assertEqual(invalidations, ["target_invalidated"])

    def test_no_target_or_no_authoritative_session_cannot_start(self):
        without_session = ActionRecorder(SimpleNamespace(target=None), None)
        with self.assertRaises(RuntimeError):
            without_session.start()
        session = Session(None)
        recorder = ActionRecorder(
            SimpleNamespace(target=SimpleNamespace(hwnd=999)),
            None,
            target_session=session,
            boundary_adapter=Adapter(),
        )
        with self.assertRaises(RuntimeError):
            recorder.start()

    def test_listener_shutdown_and_subscription_are_idempotent(self):
        created = []

        class Listener:
            def __init__(self, **_kwargs):
                self.stop_count = 0
                self.join_count = 0
                created.append(self)

            def start(self):
                return None

            def stop(self):
                self.stop_count += 1

            def join(self, timeout=None):
                self.join_count += 1

        session = Session(snapshot())
        recorder = ActionRecorder(
            SimpleNamespace(target=None),
            None,
            target_session=session,
            boundary_adapter=Adapter(),
        )
        with (
            patch("app.recorder.pynput_keyboard.Listener", Listener),
            patch("app.recorder.pynput_mouse.Listener", Listener),
            patch("app.recorder.ctypes.windll.user32.SetForegroundWindow"),
        ):
            recorder.start()
            first = recorder.stop()
            second = recorder.stop()
        self.assertIs(first, second)
        self.assertEqual(len(created), 2)
        self.assertTrue(all(item.stop_count == 1 for item in created))
        self.assertTrue(all(item.join_count == 1 for item in created))
        self.assertEqual(session.listeners, [])

    def test_player_activity_blocks_recorder_start(self):
        messages = []
        harness = SimpleNamespace(
            state=AppState.IDLE,
            workflow_runner=SimpleNamespace(is_active=lambda: False),
            player=SimpleNamespace(is_active=lambda: True),
            recorder=SimpleNamespace(start=lambda: self.fail("must not start")),
            _show_message=lambda title, text: messages.append((title, text)),
        )
        ScreenBotApp.start_recording(harness)
        self.assertEqual(messages[0][0], "腳本播放中")

    def test_application_invalidation_stops_recorder_and_returns_idle(self):
        states = []
        messages = []
        recorder = SimpleNamespace(
            stop=lambda reason=None: {
                "version": 2,
                "events": [{"type": "click"}],
            }
        )
        harness = SimpleNamespace(
            state=AppState.RECORDING,
            recorder=recorder,
            recorder_overlay=SimpleNamespace(stop=lambda: None),
            last_recording=None,
            last_recording_saved=True,
            _recorded_event_count=0,
            logger=SimpleNamespace(info=lambda *_args: None),
            set_state=lambda state: states.append(state),
            _macro_manager=None,
            _show_message=lambda title, text: messages.append((title, text)),
        )
        harness.stop_recording = lambda reason="normal_stop": (
            ScreenBotApp.stop_recording(harness, reason=reason)
        )
        ScreenBotApp._on_recording_target_invalidated(
            harness, "target_invalidated"
        )
        self.assertEqual(states, [AppState.IDLE])
        self.assertEqual(harness.last_recording["events"][0]["type"], "click")
        self.assertEqual(messages, [("目標失效", "目標失效，錄製已停止。")])


if __name__ == "__main__":
    unittest.main()
