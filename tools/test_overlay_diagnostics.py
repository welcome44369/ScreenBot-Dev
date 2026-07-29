"""Focused tests for the environment-gated overlay diagnostic service."""
from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.overlay_diagnostics import (
    EVENT_OBJECT_LOCATIONCHANGE,
    EVENT_SYSTEM_FOREGROUND,
    OverlayDiagnostics,
    WS_CHILD,
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    WS_VISIBLE,
    parse_window_styles,
)


class _FakeAdapter:
    is_supported = True

    def __init__(self):
        self.hook_callback = None
        self.unhooked = []
        self.windows = {
            10: self._window(10, 10, "ScreenBot", "Compact", True),
            20: self._window(20, 20, "Target", "Target A", False),
            30: self._window(30, 30, "Other", "Other B", False),
        }

    @staticmethod
    def _window(hwnd, root, process_name, title, topmost):
        return {
            "hwnd": hwnd,
            "is_window": True,
            "pid": hwnd,
            "thread_id": hwnd,
            "process_name": process_name,
            "executable": f"C:\\{process_name}.exe",
            "title": title,
            "class_name": "FakeWindow",
            "root_hwnd": root,
            "root_owner_hwnd": root,
            "parent_hwnd": 0,
            "owner_hwnd": 0,
            "window_style_raw": WS_VISIBLE,
            "extended_style_raw": WS_EX_TOPMOST if topmost else 0,
            "is_topmost": topmost,
            "is_noactivate": False,
            "is_toolwindow": False,
            "is_appwindow": False,
            "is_child": False,
            "is_visible": True,
            "is_enabled": True,
            "is_iconic": False,
            "is_cloaked": False,
            "window_rect": (0, 0, 100, 100),
            "client_rect_screen": (0, 0, 100, 100),
            "dpi": 96,
            "z_prev_hwnd": 0,
            "z_next_hwnd": 0,
            "foreground_hwnd": 20,
            "foreground_root_hwnd": 20,
            "is_foreground": root == 20,
        }

    def snapshot_window(self, hwnd):
        return dict(self.windows.get(hwnd, {"hwnd": hwnd, "is_window": False}))

    def enumerate_windows(self):
        return [30, 10, 20]

    def foreground_hwnd(self):
        return 20

    def root_hwnd(self, hwnd):
        return int(hwnd or 0)

    def install_win_event_hook(self, callback):
        self.hook_callback = callback
        return ("hook-a", "hook-b")

    def uninstall_win_event_hook(self, handles):
        self.unhooked.extend(handles)


class OverlayDiagnosticsTests(unittest.TestCase):
    def _read_records(self, diagnostics):
        diagnostics.close()
        with diagnostics.log_path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_disabled_diagnostic_creates_no_logs_or_hook(self):
        adapter = _FakeAdapter()
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = OverlayDiagnostics(
                directory, adapter=adapter, enabled=False
            )
            self.assertFalse(diagnostics.start())
            self.assertIsNone(diagnostics.log_path)
            self.assertIsNone(adapter.hook_callback)
            self.assertFalse((Path(directory) / "logs").exists())

    def test_enabled_diagnostic_starts_and_unhooks_read_only_hook(self):
        adapter = _FakeAdapter()
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = OverlayDiagnostics(directory, adapter=adapter, enabled=True)
            self.assertTrue(diagnostics.start())
            self.assertIsNotNone(adapter.hook_callback)

            records = self._read_records(diagnostics)

            self.assertEqual(adapter.unhooked, ["hook-a", "hook-b"])
            self.assertTrue(any(record["event"] == "WINEVENT_HOOK_STARTED" for record in records))
            self.assertTrue(any(record["event"] == "WINEVENT_HOOK_STOPPED" for record in records))

    def test_style_parser_and_z_order_capture_preserve_observed_order(self):
        flags = parse_window_styles(
            WS_CHILD | WS_VISIBLE,
            WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
        )
        self.assertEqual(
            flags,
            {
                "is_topmost": True,
                "is_noactivate": True,
                "is_toolwindow": True,
                "is_appwindow": False,
                "is_child": True,
                "is_visible": True,
            },
        )
        adapter = _FakeAdapter()
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = OverlayDiagnostics(directory, adapter=adapter, enabled=True)
            diagnostics.observe_compact_lifecycle(
                "COMPACT_WINID_FIRST_ACQUIRED", old_hwnd=0, new_hwnd=10
            )
            diagnostics.observe_target_event("TARGET_LOCK_SUCCEEDED", type("Target", (), {
                "session_id": "session",
                "generation": 1,
                "root_hwnd": 20,
                "client_hwnd": 20,
                "window_rect": (0, 0, 100, 100),
                "current_client_size": (100, 100),
                "visibility_state": type("Visibility", (), {"value": "FOREGROUND"})(),
                "identity_strength": "strong",
            })())
            diagnostics.capture_z_order_snapshot("test")

            records = self._read_records(diagnostics)
            snapshot = next(record for record in records if record["event"] == "Z_ORDER_SNAPSHOT" and record["reason"] == "test")
            self.assertEqual([item["hwnd"] for item in snapshot["top_to_bottom"]], [30, 10, 20])
            self.assertEqual(snapshot["compact_neighbors"]["below"][0]["hwnd"], 20)
            self.assertEqual(snapshot["top_to_bottom"][0]["z_index"], 0)
            self.assertEqual(snapshot["top_to_bottom"][0]["owner_hwnd"], 0)

    def test_layered_style_and_special_native_values_are_decoded(self):
        adapter = _FakeAdapter()
        adapter.windows[10]["extended_style_raw"] |= WS_EX_LAYERED
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = OverlayDiagnostics(
                directory, adapter=adapter, enabled=True
            )
            self.assertTrue(diagnostics.capture_window(10)["is_layered"])
            diagnostics.close()
        self.assertEqual(
            OverlayDiagnostics.decode_hwnd_insert_after(-2),
            "HWND_NOTOPMOST",
        )
        self.assertEqual(
            OverlayDiagnostics.decode_hwnd_insert_after(30),
            "HWND(30)",
        )
        self.assertEqual(
            OverlayDiagnostics.decode_swp_flags(0x0010 | 0x0001 | 0x0002),
            ["SWP_NOSIZE", "SWP_NOMOVE", "SWP_NOACTIVATE"],
        )

    def test_setwindowpos_observation_is_read_only(self):
        adapter = _FakeAdapter()
        original_order = list(adapter.enumerate_windows())
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = OverlayDiagnostics(
                directory, adapter=adapter, enabled=True
            )
            diagnostics.observe_compact_lifecycle(
                "COMPACT_WINID_FIRST_ACQUIRED", old_hwnd=0, new_hwnd=10
            )
            diagnostics.observe_set_window_pos(
                "before",
                reason="z_order",
                hwnd=10,
                insert_after=30,
                x=0,
                y=0,
                width=0,
                height=0,
                flags=0x0010 | 0x0001 | 0x0002,
            )
            diagnostics.observe_set_window_pos(
                "after",
                reason="z_order",
                hwnd=10,
                insert_after=30,
                x=0,
                y=0,
                width=0,
                height=0,
                flags=0x0010 | 0x0001 | 0x0002,
                result=True,
                last_error=0,
            )
            records = self._read_records(diagnostics)

        mutations = [
            item for item in records if item["event"] == "SET_WINDOW_POS"
        ]
        self.assertEqual(
            [item["phase"] for item in mutations],
            ["before", "after"],
        )
        self.assertEqual(mutations[-1]["insert_after_symbolic"], "HWND(30)")
        self.assertEqual(mutations[-1]["get_last_error"], 0)
        self.assertEqual(adapter.enumerate_windows(), original_order)

    def test_start_correlation_lifecycle_popup_and_event_sampling(self):
        adapter = _FakeAdapter()
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = OverlayDiagnostics(directory, adapter=adapter, enabled=True)
            diagnostics.observe_compact_lifecycle(
                "COMPACT_WINID_FIRST_ACQUIRED", old_hwnd=0, new_hwnd=10
            )
            interaction_id = diagnostics.begin_interaction("START")
            diagnostics.observe_ui_event("START_UI_MOUSE_PRESS", receiver="QPushButton")
            diagnostics.observe_ui_event("START_UI_MOUSE_RELEASE", receiver="QPushButton")
            diagnostics.observe_ui_event("START_SIGNAL_EMITTED")
            diagnostics._popup_roles[30] = "SCREENBOT_COMBO_POPUP"
            self.assertEqual(
                diagnostics.classify_window(adapter.snapshot_window(30)),
                "SCREENBOT_COMBO_POPUP",
            )
            diagnostics._record_win_event(
                {"event": EVENT_SYSTEM_FOREGROUND, "hwnd": 20}
            )
            diagnostics._record_win_event(
                {"event": EVENT_OBJECT_LOCATIONCHANGE, "hwnd": 20}
            )
            diagnostics._record_win_event(
                {"event": EVENT_OBJECT_LOCATIONCHANGE, "hwnd": 999}
            )
            diagnostics._record_win_event(
                {"event": EVENT_OBJECT_LOCATIONCHANGE, "hwnd": 20}
            )
            diagnostics.observe_compact_lifecycle(
                "COMPACT_HWND_REACQUIRED", old_hwnd=10, new_hwnd=11
            )

            records = self._read_records(diagnostics)
            start_records = [
                record for record in records
                if record["event"] in {
                    "START_UI_MOUSE_PRESS",
                    "START_UI_MOUSE_RELEASE",
                    "START_SIGNAL_EMITTED",
                }
            ]
            self.assertEqual({record["interaction_id"] for record in start_records}, {interaction_id})
            self.assertEqual(
                sum(
                    record["event"] == "WINEVENT"
                    and record["event_name"] == "EVENT_OBJECT_LOCATIONCHANGE"
                    for record in records
                ),
                1,
            )
            recreation = next(
                record for record in records
                if record["event"] == "COMPACT_HWND_REACQUIRED"
            )
            self.assertTrue(recreation["hwnd_changed"])

    def test_log_records_are_strict_json_when_window_data_contains_nan(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = OverlayDiagnostics(
                directory, adapter=_FakeAdapter(), enabled=True
            )
            diagnostics.record_event("NAN_SAMPLE", measurement=math.nan)

            records = self._read_records(diagnostics)

            nan_sample = next(
                record for record in records if record["event"] == "NAN_SAMPLE"
            )
            self.assertIsNone(nan_sample["measurement"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
