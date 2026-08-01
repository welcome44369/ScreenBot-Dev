import logging
import unittest
from app.hotkeys import HotkeyBridge, HotkeyManager
from app.application import ScreenBotApp
from app.state import AppState

class _Keyboard:
    def __init__(self, fail=False): self.fail, self.added, self.removed = fail, [], []
    def add_hotkey(self, key, callback, **_kwargs):
        if self.fail and key == "f8": raise RuntimeError("denied")
        self.added.append((key, callback)); return key
    def remove_hotkey(self, handle): self.removed.append(handle)

class HotkeyRuntimeTests(unittest.TestCase):
    def make(self, fail=False):
        bridge, events = HotkeyBridge(), []
        manager = HotkeyManager(bridge, logging.getLogger("test.hotkey"), on_status=lambda event, **data: events.append((event, data)))
        manager.keyboard, manager._available = _Keyboard(fail), True
        return manager, bridge, events
    def test_registration_once_and_dispatches_bridge(self):
        manager, bridge, _ = self.make(); received = []
        bridge.f8_pressed.connect(lambda: received.append("f8"))
        self.assertTrue(manager.register_hotkeys()); self.assertTrue(manager.register_hotkeys())
        self.assertEqual([key for key, _ in manager.keyboard.added].count("f8"), 1)
        next(callback for key, callback in manager.keyboard.added if key == "f8")()
        self.assertEqual(received, ["f8"])
    def test_failure_is_reported(self):
        manager, _, events = self.make(fail=True)
        self.assertFalse(manager.register_hotkeys())
        self.assertEqual(events[0][0], "HOTKEY_REGISTRATION_FAILED")
    def test_pause_resume_and_shutdown_are_idempotent(self):
        manager, _, _ = self.make(); manager.register_hotkeys()
        self.assertTrue(manager.pause("test")); self.assertFalse(manager.pause("test"))
        self.assertTrue(manager.resume("test")); self.assertFalse(manager.resume("test"))
        self.assertTrue(manager.unregister_all()); self.assertFalse(manager.unregister_all())
    def test_bridge_reaches_application_callback(self):
        bridge, calls = HotkeyBridge(), []
        app = ScreenBotApp.__new__(ScreenBotApp)
        app.state = AppState.IDLE
        app.logger = logging.getLogger("test.hotkey.application")
        app._handle_f8 = lambda: calls.append("application")
        bridge.f8_pressed.connect(app._on_bridge_f8)
        bridge.f8_pressed.emit()
        self.assertEqual(calls, ["application"])

if __name__ == "__main__": unittest.main(verbosity=2)
