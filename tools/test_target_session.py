import dataclasses
import logging
import os
import threading
import unittest

from app.application import ScreenBotApp
from app.target_session import (
    TargetConnectionState,
    TargetSessionService,
    TargetVisibilityState,
)
from app.window_tracker import WindowInfo, WindowTracker


def make_window_info(**changes):
    values = {
        "hwnd": 100,
        "title": "Safe Target",
        "process_name": "safe-target.exe",
        "pid": os.getpid() + 1000,
        "window_left": -1200,
        "window_top": 20,
        "window_right": -400,
        "window_bottom": 620,
        "client_left": -1192,
        "client_top": 52,
        "client_width": 784,
        "client_height": 560,
        "root_hwnd": 100,
        "client_hwnd": 100,
        "process_creation_time": 123456789,
        "executable_path": r"C:\SafeTarget\safe-target.exe",
        "window_class": "SafeTargetWindow",
        "dpi": 144,
        "visible": True,
        "minimized": False,
        "foreground": True,
    }
    values.update(changes)
    return WindowInfo(**values)


class FakeWindowAdapter:
    def __init__(self, info=None):
        self.info = info or make_window_info()
        self.foreground_hwnd = self.info.hwnd
        self.valid = True

    def get_foreground_hwnd(self):
        return self.foreground_hwnd

    def query_window(self, hwnd):
        if not self.valid or int(hwnd) != int(self.info.hwnd):
            raise RuntimeError("invalid test window")
        return self.info

    def validate_window(self, hwnd):
        return bool(self.valid and int(hwnd) == int(self.info.hwnd))


class TargetSessionTests(unittest.TestCase):
    def make_service(self):
        adapter = FakeWindowAdapter()
        service = TargetSessionService(
            adapter, logging.getLogger("ScreenBot.TargetSessionTest")
        )
        return service, adapter

    def test_explicit_lock_creates_complete_immutable_snapshot(self):
        service, _adapter = self.make_service()
        snapshot = service.lock_foreground_target()

        self.assertTrue(snapshot.session_id)
        self.assertEqual(snapshot.generation, 1)
        self.assertEqual(snapshot.root_hwnd, 100)
        self.assertEqual(snapshot.client_hwnd, 100)
        self.assertEqual(snapshot.initial_client_size, (784, 560))
        self.assertEqual(snapshot.current_client_size, (784, 560))
        self.assertEqual(snapshot.dpi, 144)
        self.assertEqual(snapshot.connection_state, TargetConnectionState.ATTACHED)
        self.assertEqual(snapshot.visibility_state, TargetVisibilityState.FOREGROUND)
        self.assertTrue(snapshot.identity_valid)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.window_title = "mutated"

    def test_dynamic_refresh_preserves_identity_and_generation(self):
        service, adapter = self.make_service()
        initial = service.lock_foreground_target()
        adapter.info = make_window_info(
            title="Renamed",
            window_left=10,
            window_top=30,
            window_right=1010,
            window_bottom=830,
            client_left=18,
            client_top=62,
            client_width=984,
            client_height=760,
            dpi=192,
            foreground=False,
        )

        refreshed = service.refresh()
        self.assertEqual(refreshed.session_id, initial.session_id)
        self.assertEqual(refreshed.generation, initial.generation)
        self.assertEqual(refreshed.window_title, "Renamed")
        self.assertEqual(refreshed.current_client_size, (984, 760))
        self.assertEqual(refreshed.dpi, 192)
        self.assertEqual(refreshed.visibility_state, TargetVisibilityState.BACKGROUND)

    def test_minimize_and_restore_are_visibility_changes_not_disconnects(self):
        service, adapter = self.make_service()
        initial = service.lock_foreground_target()
        adapter.info = make_window_info(minimized=True, foreground=False)

        minimized = service.refresh()
        self.assertEqual(minimized.connection_state, TargetConnectionState.ATTACHED)
        self.assertEqual(minimized.visibility_state, TargetVisibilityState.MINIMIZED)
        self.assertTrue(minimized.identity_valid)

        adapter.info = make_window_info(minimized=False, foreground=True)
        restored = service.refresh()
        self.assertEqual(restored.session_id, initial.session_id)
        self.assertEqual(restored.generation, initial.generation)
        self.assertEqual(restored.visibility_state, TargetVisibilityState.FOREGROUND)

    def test_invalid_hwnd_disconnects_once(self):
        service, adapter = self.make_service()
        events = []
        service.subscribe(lambda event, _snapshot, _payload: events.append(event))
        service.lock_foreground_target()
        adapter.valid = False

        first = service.refresh()
        second = service.refresh()
        self.assertIs(first, second)
        self.assertEqual(first.connection_state, TargetConnectionState.DISCONNECTED)
        self.assertEqual(first.disconnect_reason, "INVALID_HWND")
        self.assertEqual(events.count("TARGET_DISCONNECTED"), 1)

    def test_strong_identity_mismatches_disconnect(self):
        cases = {
            "PID_MISMATCH": {"pid": make_window_info().pid + 1},
            "PROCESS_INSTANCE_MISMATCH": {"process_creation_time": 987654321},
            "EXECUTABLE_MISMATCH": {
                "executable_path": r"C:\Other\replacement.exe"
            },
            "WINDOW_CLASS_MISMATCH": {"window_class": "ReplacementWindow"},
            "ROOT_HWND_MISMATCH": {"root_hwnd": 200},
            "CLIENT_HWND_MISMATCH": {"client_hwnd": 201},
        }
        for expected_reason, changes in cases.items():
            with self.subTest(expected_reason=expected_reason):
                service, adapter = self.make_service()
                service.lock_foreground_target()
                adapter.info = make_window_info(**changes)
                result = service.refresh()
                self.assertEqual(
                    result.connection_state, TargetConnectionState.DISCONNECTED
                )
                self.assertEqual(result.disconnect_reason, expected_reason)

    def test_explicit_relock_creates_new_session_and_generation(self):
        service, adapter = self.make_service()
        first = service.lock_foreground_target()
        adapter.info = make_window_info(title="Same Identity, Explicit Relock")
        second = service.lock_foreground_target()

        self.assertNotEqual(first.session_id, second.session_id)
        self.assertEqual(second.generation, first.generation + 1)

    def test_screenbot_process_cannot_be_locked_as_target(self):
        service, adapter = self.make_service()
        adapter.info = make_window_info(pid=os.getpid())
        with self.assertRaisesRegex(RuntimeError, "ScreenBot"):
            service.lock_foreground_target()
        self.assertFalse(service.has_target())

    def test_degraded_process_identity_is_visible_but_attachable(self):
        service, adapter = self.make_service()
        adapter.info = make_window_info(
            process_creation_time=None,
            executable_path=None,
            identity_degraded_reasons=(
                "process_creation_time_unavailable",
                "executable_path_unavailable",
            ),
        )
        snapshot = service.lock_foreground_target()
        self.assertEqual(snapshot.connection_state, TargetConnectionState.ATTACHED)
        self.assertEqual(snapshot.identity_strength, "degraded")
        self.assertIn(
            "process_creation_time_unavailable",
            snapshot.identity_degraded_reason,
        )
        adapter.info = make_window_info()
        refreshed = service.refresh()
        self.assertEqual(refreshed.identity_strength, "degraded")
        self.assertIn(
            "process_creation_time_unavailable",
            refreshed.identity_degraded_reason,
        )

    def test_listener_runs_outside_service_lock_and_can_reenter(self):
        service, _adapter = self.make_service()
        observed = []

        def listener(event, snapshot, _payload):
            observed.append((event, service.get_snapshot(), snapshot))

        service.subscribe(listener)
        locked = service.lock_foreground_target()
        self.assertEqual(observed[0][0], "TARGET_SESSION_CREATED")
        self.assertIs(observed[0][1], locked)

    def test_concurrent_readers_never_observe_partial_snapshot(self):
        service, adapter = self.make_service()
        initial = service.lock_foreground_target()
        errors = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                snapshot = service.get_snapshot()
                if snapshot is None:
                    errors.append("missing snapshot")
                    return
                if (
                    snapshot.session_id != initial.session_id
                    or snapshot.generation != initial.generation
                ):
                    errors.append("identity changed")
                    return

        readers = [threading.Thread(target=reader) for _ in range(4)]
        for thread in readers:
            thread.start()
        for index in range(50):
            adapter.info = make_window_info(
                title=f"Title {index}",
                client_width=784 + index,
            )
            service.refresh()
        stop.set()
        for thread in readers:
            thread.join(timeout=1)
        self.assertEqual(errors, [])

    def test_window_tracker_is_read_only_projection_when_attached(self):
        service, adapter = self.make_service()
        tracker = WindowTracker()
        tracker.attach_target_session(service)
        snapshot = service.lock_foreground_target()

        projection = tracker.target
        self.assertEqual(projection.hwnd, snapshot.hwnd)
        self.assertEqual(projection.client_width, snapshot.current_client_size[0])
        with self.assertRaises(AttributeError):
            tracker.target = make_window_info()
        adapter.info = make_window_info(title="Projection Refresh")
        service.refresh()
        self.assertEqual(tracker.target.title, "Projection Refresh")

    def test_diagnostic_events_are_edge_triggered(self):
        service, adapter = self.make_service()
        events = []
        service.subscribe(lambda event, _snapshot, _payload: events.append(event))
        service.lock_foreground_target()
        adapter.info = make_window_info(foreground=False)
        service.refresh()
        service.refresh()
        service.clear("test_complete")
        service.clear("duplicate_clear")

        self.assertEqual(events.count("TARGET_SESSION_CREATED"), 1)
        self.assertEqual(events.count("TARGET_STATE_CHANGED"), 1)
        self.assertEqual(events.count("TARGET_SESSION_CLEARED"), 1)
        self.assertFalse(service.has_target())

    def test_active_workflow_f8_does_not_replace_session(self):
        service, adapter = self.make_service()
        initial = service.lock_foreground_target()
        tracker = WindowTracker()
        tracker.attach_target_session(service)
        adapter.info = make_window_info(title="Would Be Replacement")

        app = ScreenBotApp.__new__(ScreenBotApp)
        app.logger = logging.getLogger("ScreenBot.ActiveWorkflowF8Test")
        app.workflow_runner = type(
            "ActiveWorkflow",
            (),
            {"is_active": lambda _self: True},
        )()
        app.window_tracker = tracker
        app._handle_f8()

        current = service.get_snapshot()
        self.assertEqual(current.session_id, initial.session_id)
        self.assertEqual(current.generation, initial.generation)
        self.assertEqual(current.window_title, initial.window_title)


if __name__ == "__main__":
    unittest.main()
