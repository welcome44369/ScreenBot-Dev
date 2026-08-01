import logging
import unittest
from types import SimpleNamespace

from app.application import ScreenBotApp
from app.state import AppState
from app.hotkeys import HotkeyBridge, HotkeyManager


class _Session:
    def __init__(self, bound=False): self.bound, self.reasons = bound, []
    def has_target(self): return self.bound
    def clear(self, reason): self.reasons.append(reason); self.bound = False


class _Workflow:
    def __init__(self, active=False): self.active, self.stops = active, 0
    def is_active(self): return self.active


class F8TargetToggleTests(unittest.TestCase):
    def app(self, bound=False):
        app = ScreenBotApp.__new__(ScreenBotApp)
        app.logger = logging.getLogger("test.f8.toggle")
        app.target_session = _Session(bound)
        app.workflow_runner = _Workflow()
        app.state = AppState.IDLE
        app._refresh_compact_presentation = lambda: None
        app.lock_calls = 0
        app.lock_or_refresh_target_only = lambda: setattr(app, "lock_calls", app.lock_calls + 1)
        return app
    def test_unbound_locks_and_bound_unlocks(self):
        app = self.app(); app._handle_f8(); self.assertEqual(app.lock_calls, 1)
        app.target_session.bound = True; app._handle_f8()
        self.assertEqual(app.target_session.reasons, ["f8_unlock"])
    def test_unlock_does_not_retarget_until_next_press(self):
        app = self.app(bound=True); app._handle_f8()
        self.assertEqual(app.lock_calls, 0)
        app._handle_f8(); self.assertEqual(app.lock_calls, 1)
    def test_screenbot_foreground_can_unlock_existing_target(self):
        app = self.app(bound=True); app._handle_f8()
        self.assertFalse(app.target_session.has_target())
    def test_active_workflow_must_stop_before_unlock(self):
        app = self.app(bound=True); app.workflow_runner.active = True
        app.stop_workflow = lambda: setattr(app.workflow_runner, "active", False)
        app._handle_f8(); self.assertEqual(app.target_session.reasons, ["f8_unlock"])
    def test_unsafe_active_owner_blocks_unlock(self):
        app = self.app(bound=True); app.workflow_runner.active = True
        app.stop_workflow = lambda: None
        app._handle_f8(); self.assertTrue(app.target_session.has_target())
    def test_old_generation_cannot_clear_replacement_session(self):
        app = self.app(bound=True)
        # There is no deferred clear: the synchronous owner check clears only
        # the current session, so an old pending generation has no callback.
        app._handle_f8(); self.assertEqual(app.target_session.reasons, ["f8_unlock"])
    def test_one_key_cycle_preserves_toggle_semantics(self):
        app = self.app(bound=True)
        bridge = HotkeyBridge()
        manager = HotkeyManager(bridge, logging.getLogger("test.f8.cycle"))
        class Keyboard:
            def add_hotkey(self, key, callback, **_kwargs):
                if key == "f8":
                    self.down = callback
                return "down"
            def on_release_key(self, key, callback, suppress=False):
                self.up = callback
                return "up"
            def remove_hotkey(self, _handle): pass
            def unhook(self, _handle): pass
        manager.keyboard, manager._available = Keyboard(), True
        bridge.f8_pressed.connect(app._handle_f8)
        manager.register_hotkeys()
        manager.keyboard.down(); manager.keyboard.down()
        self.assertFalse(app.target_session.has_target())
        self.assertEqual(app.lock_calls, 0)
        manager.keyboard.up(None); manager.keyboard.down()
        self.assertEqual(app.lock_calls, 1)


if __name__ == "__main__": unittest.main(verbosity=2)
