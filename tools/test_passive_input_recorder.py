"""Regression tests for passive semantic recording without physical input."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.recorder import ActionRecorder, _RawInputEvent
from app.player import ScriptPlayer
from tools.test_recorder_target_scope import Adapter, Session, ready_recorder, snapshot


class _Target:
    hwnd = 42


class _Tracker:
    target = _Target()


class _Key:
    def __init__(self, char=None, name=None, vk=None):
        self.char = char
        self.name = name
        self.vk = vk


def _recorder():
    recorder, _, _ = ready_recorder()
    return recorder


class PassiveInputRecorderTest(unittest.TestCase):
    def test_listeners_are_explicitly_passive_and_restartable(self):
        created = []

        class Listener:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                created.append(self)

            def start(self):
                return None

            def stop(self):
                return None

            def join(self, timeout=None):
                return None

        with (
            patch("app.recorder.pynput_keyboard.Listener", Listener),
            patch("app.recorder.pynput_mouse.Listener", Listener),
        ):
            recorder = ActionRecorder(
                _Tracker(),
                settings=None,
                target_session=Session(snapshot()),
                boundary_adapter=Adapter(),
            )
            recorder.start()
            recorder.stop()
            recorder.start()
            recorder.stop()
        self.assertEqual(len(created), 4)
        self.assertTrue(all(listener.kwargs["suppress"] is False for listener in created))

    def test_key_repeat_is_ignored_and_complete_key_is_committed(self):
        recorder = _recorder()
        key = _Key(char="w", vk=17)
        recorder._normalize(_RawInputEvent("key_down", 0.1, {"key": key}))
        recorder._normalize(_RawInputEvent("key_down", 0.2, {"key": key}))
        self.assertEqual(len(recorder.actions), 0)
        recorder._normalize(_RawInputEvent("key_up", 0.3, {"key": key}))
        self.assertEqual([(event["type"], event["event"]) for event in recorder.actions], [("key", "down"), ("key", "up")])
        self.assertFalse(any(event.get("synthetic") for event in recorder.actions))

    def test_hotkeys_and_free_moves_do_not_persist(self):
        recorder = _recorder()
        recorder._normalize(_RawInputEvent("key_down", 0.1, {"key": _Key(name="f8", vk=119)}))
        recorder._normalize(_RawInputEvent("mouse_move", 0.2, {"x": 1, "y": 1}))
        recorder._normalize(_RawInputEvent("mouse_move", 0.3, {"x": 2, "y": 2}))
        self.assertEqual(recorder.actions, [])
        self.assertEqual(recorder.get_diagnostics()["ignored_move_count"], 2)
        self.assertEqual(recorder.get_diagnostics()["ignored_hotkey_event_count"], 1)

    def test_click_drag_and_scroll_are_semantic(self):
        recorder = _recorder()
        recorder._normalize(_RawInputEvent("mouse_down", 0.1, {"x": 20, "y": 30, "button": "left"}))
        recorder._normalize(_RawInputEvent("mouse_up", 0.2, {"x": 22, "y": 31, "button": "left"}))
        recorder._normalize(_RawInputEvent("mouse_down", 0.4, {"x": 10, "y": 20, "button": "left"}))
        recorder._normalize(_RawInputEvent("mouse_up", 1.0, {"x": 90, "y": 80, "button": "left"}))
        recorder._normalize(_RawInputEvent("scroll", 1.1, {"x": 50, "y": 50, "dx": 0, "dy": -1}))
        recorder._normalize(_RawInputEvent("scroll", 1.15, {"x": 50, "y": 50, "dx": 0, "dy": -2}))
        self.assertEqual([event["type"] for event in recorder.actions], ["click", "drag", "scroll"])
        self.assertEqual(recorder.actions[2]["dy"], -3)
        self.assertFalse(any(event["type"] == "mouse_move" for event in recorder.actions))

    def test_player_accepts_version_two_drag_without_breaking_legacy_events(self):
        player = ScriptPlayer(_Tracker())
        new_actions = player._normalize_script({
            "version": 2, "name": "semantic", "coordinate_space": "target_client_ratio",
            "events": [{"type": "drag", "start_x_ratio": .1, "start_y_ratio": .2, "end_x_ratio": .8, "end_y_ratio": .9, "duration_ms": 200, "delay_ms": 0}],
        })["actions"]
        old_actions = player._normalize_script({
            "version": 1, "name": "legacy", "events": [{"type": "mouse_move", "x_ratio": .2, "y_ratio": .3, "delay_ms": 0}],
        })["actions"]
        self.assertEqual(new_actions[0]["type"], "drag")
        self.assertEqual(old_actions[0]["type"], "mouse_move")


if __name__ == "__main__":
    unittest.main()
