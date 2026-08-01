"""Focused tests for the Recorder TargetSession authorization boundary."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import os
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.target_boundary import FrozenTargetBoundary, TargetBoundaryFilter


def snapshot(**changes):
    values = {
        "session_id": "session-a",
        "generation": 7,
        "pid": 4100,
        "process_creation_time": 123456,
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
        self.refresh_count = 0

    def refresh(self):
        self.refresh_count += 1
        return self.current


class Adapter:
    def __init__(self):
        self.hit = 101
        self.foreground = 100
        self.roots = {100: 100, 101: 100, 102: 100, 200: 200, 201: 201}
        self.owners = {}
        self.pids = {100: 4100, 101: 4100, 102: 4100, 200: 5200, 201: 4100}
        self.client_point = (20, 30)
        self.size = (200, 100)

    def window_from_point(self, _x, _y):
        return self.hit

    def foreground_hwnd(self):
        return self.foreground

    def root_hwnd(self, hwnd):
        return self.roots.get(hwnd, hwnd)

    def owner_hwnd(self, hwnd):
        return self.owners.get(hwnd, 0)

    def pid_for_window(self, hwnd):
        return self.pids.get(hwnd, 0)

    def screen_to_client(self, _hwnd, _x, _y):
        return self.client_point

    def client_size(self, _hwnd):
        return self.size


class TargetBoundaryFilterTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = snapshot()
        self.session = Session(self.snapshot)
        self.adapter = Adapter()
        self.frozen = FrozenTargetBoundary.from_snapshot(self.snapshot)
        self.boundary = TargetBoundaryFilter(
            self.session,
            self.frozen,
            adapter=self.adapter,
            screenbot_pid=os.getpid(),
        )

    def test_frozen_token_contains_complete_identity(self):
        self.assertEqual(
            (
                self.frozen.session_id,
                self.frozen.generation,
                self.frozen.pid,
                self.frozen.process_creation_time,
                self.frozen.root_hwnd,
                self.frozen.client_hwnd,
                self.frozen.executable_path,
                self.frozen.identity_strength,
            ),
            (
                "session-a",
                7,
                4100,
                123456,
                100,
                101,
                r"C:\Target\target.exe",
                "strong",
            ),
        )

    def test_valid_frozen_snapshot_passes_and_is_refreshed_each_event(self):
        self.assertTrue(self.boundary.authorize_keyboard().accepted)
        self.assertTrue(self.boundary.authorize_mouse(40, 50).accepted)
        self.assertEqual(self.session.refresh_count, 2)

    def test_identity_mismatches_fail_closed(self):
        cases = {
            "session_id": "session-b",
            "generation": 8,
            "pid": 9999,
            "process_creation_time": 654321,
            "root_hwnd": 222,
            "client_hwnd": 223,
            "executable_path": r"C:\Other\other.exe",
            "identity_valid": False,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                self.session.current = snapshot(**{field: value})
                decision = self.boundary.validate_session()
                self.assertFalse(decision.accepted)
                self.assertFalse(decision.session_valid)

    def test_cleared_session_is_rejected_without_foreground_fallback(self):
        self.session.current = None
        decision = self.boundary.authorize_keyboard()
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "target_session_cleared")

    def test_mouse_accepts_target_child_and_owned_popup(self):
        self.adapter.hit = 102
        self.assertTrue(self.boundary.authorize_mouse(40, 50).accepted)
        self.adapter.hit = 201
        self.adapter.owners[201] = 100
        popup = self.boundary.authorize_mouse(40, 50)
        self.assertTrue(popup.accepted)

    def test_same_pid_window_is_not_sufficient(self):
        self.adapter.hit = 201
        decision = self.boundary.authorize_mouse(40, 50)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "window_outside_target")

    def test_screenbot_window_is_rejected_before_target_ownership(self):
        self.adapter.hit = 200
        self.adapter.pids[200] = os.getpid()
        self.adapter.roots[200] = 100
        decision = self.boundary.authorize_mouse(40, 50)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "screenbot_window")

    def test_desktop_taskbar_other_and_unknown_windows_are_rejected(self):
        for hit in (0, 200, 300, 400):
            with self.subTest(hit=hit):
                self.adapter.hit = hit
                self.assertFalse(
                    self.boundary.authorize_mouse(40, 50).accepted
                )

    def test_client_coordinates_and_ratios_are_emitted(self):
        decision = self.boundary.authorize_mouse(40, 50)
        self.assertEqual(
            (
                decision.client_x,
                decision.client_y,
                decision.x_ratio,
                decision.y_ratio,
            ),
            (20, 30, 0.1, 0.3),
        )

    def test_point_outside_frozen_client_is_rejected(self):
        self.adapter.client_point = (200, 30)
        decision = self.boundary.authorize_mouse(40, 50)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "outside_target_client")

    def test_keyboard_requires_frozen_foreground_root(self):
        self.adapter.foreground = 200
        decision = self.boundary.authorize_keyboard()
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "foreground_outside_target")

    def test_invalid_snapshot_cannot_be_frozen(self):
        with self.assertRaises(RuntimeError):
            FrozenTargetBoundary.from_snapshot(snapshot(identity_valid=False))
        with self.assertRaises(RuntimeError):
            FrozenTargetBoundary.from_snapshot(None)


if __name__ == "__main__":
    unittest.main()
