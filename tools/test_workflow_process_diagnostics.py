import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

from app.trigger_runner import TriggerRunner
from app.workflow_process_diagnostics import WorkflowProcessDiagnostics


class FakeWindowSnapshot:
    def __init__(self, hwnd, pid=0):
        self.hwnd = int(hwnd or 0)
        self.pid = int(pid or 0)

    def to_dict(self):
        return {
            "hwnd": self.hwnd,
            "pid": self.pid,
            "thread_id": 0,
            "process_name": None,
            "title": None,
            "class_name": None,
            "root_hwnd": self.hwnd,
            "owner_hwnd": 0,
            "is_visible": bool(self.hwnd),
            "is_topmost": False,
            "is_foreground": self.hwnd == 900,
            "is_appwindow": True,
            "is_toolwindow": False,
        }


class FakeWindowAdapter:
    def __init__(self):
        self.uninstalled = False

    def foreground_hwnd(self):
        return 900

    def snapshot_window(self, hwnd):
        return FakeWindowSnapshot(hwnd)

    def lightweight_window_state(self, hwnd):
        return {
            "hwnd": int(hwnd),
            "pid": 0,
            "thread_id": 0,
            "is_visible": True,
        }

    def enumerate_windows(self):
        return [900, 700, 500]

    def install_win_event_hook(self, _callback):
        return (201,)

    def uninstall_win_event_hook(self, handles):
        self.uninstalled = tuple(handles or ()) == (201,)


class FakePlayer:
    def __init__(self):
        self.start_count = 0

    def is_active(self):
        return False

    def start(self, *_args, **_kwargs):
        self.start_count += 1
        raise AssertionError("Diagnostic quarantine must precede player.start")


class FakeDetector:
    def observe_text(self, *_args, **_kwargs):
        return SimpleNamespace(
            recognized_text="GO",
            state=SimpleNamespace(value="PRESENT"),
            exact_match=True,
            text_similarity=1.0,
            readability_score=1.0,
            presence_score=1.0,
            reason="exact_match",
        )

    def get_capture_runtime_diagnostics(self):
        return {}


class FakeTextTrigger:
    target_texts = ("GO",)

    def update(self, _observation):
        return SimpleNamespace(
            triggered=True,
            event_name="appear",
            condition_events=[],
            present=True,
        )

    def get_status_snapshot(self):
        return {"last_present": True}


class FakeScriptStore:
    def __init__(self):
        self.load_count = 0

    def load_script(self, _name):
        self.load_count += 1
        return {"actions": []}


class QuarantineDiagnostic:
    def __init__(self):
        self.calls = 0

    def observation_completed(self, **_payload):
        self.calls += 1
        return True


def trigger_data():
    return {
        "name": "diagnostic-step",
        "trigger": {
            "type": "text",
            "text": "GO",
            "condition": {"mode": "state", "desired_state": "present"},
            "region": {
                "x_ratio": 0.0,
                "y_ratio": 0.0,
                "width_ratio": 1.0,
                "height_ratio": 1.0,
            },
            "poll_interval_ms": 10,
        },
        "macro": "must-not-load.json",
    }


class WorkflowProcessDiagnosticsTests(unittest.TestCase):
    def test_disabled_diagnostic_creates_no_output(self):
        with tempfile.TemporaryDirectory() as root:
            diagnostics = WorkflowProcessDiagnostics(
                root,
                enabled=False,
                adapter=FakeWindowAdapter(),
            )
            self.assertFalse(
                diagnostics.begin_trace(
                    workflow_identifier="wf",
                    target_snapshot=SimpleNamespace(
                        root_hwnd=700,
                        session_id="s",
                        generation=1,
                    ),
                    compact_hwnd=500,
                    quarantine_callback=lambda *_args: None,
                )
            )
            self.assertFalse((Path(root) / "logs").exists())

    def test_trace_preserves_subprocess_contract_and_quarantines_once(self):
        with tempfile.TemporaryDirectory() as root:
            callbacks = []
            adapter = FakeWindowAdapter()
            diagnostics = WorkflowProcessDiagnostics(
                root,
                enabled=True,
                adapter=adapter,
            )
            self.assertTrue(
                diagnostics.begin_trace(
                    workflow_identifier="wf.json",
                    target_snapshot=SimpleNamespace(
                        root_hwnd=700,
                        session_id="session-a",
                        generation=4,
                    ),
                    compact_hwnd=500,
                    quarantine_callback=lambda reason, details: callbacks.append(
                        (reason, details)
                    ),
                )
            )
            token = diagnostics.variant_started("test/variant")
            completed = subprocess.run(
                [sys.executable, "-c", "print('workflow-child')"],
                capture_output=True,
                check=True,
                shell=False,
            )
            diagnostics.variant_finished(token)
            self.assertIn(b"workflow-child", completed.stdout)
            observation = SimpleNamespace(
                state=SimpleNamespace(value="ABSENT"),
                recognized_text="",
            )
            result = SimpleNamespace(triggered=False, event_name=None)
            self.assertTrue(
                diagnostics.observation_completed(
                    observation=observation,
                    result=result,
                    step_id="step-1",
                )
            )
            self.assertFalse(
                diagnostics.observation_completed(
                    observation=observation,
                    result=result,
                    step_id="step-1",
                )
            )
            diagnostics.end_trace("test_finished")

            records = [
                json.loads(line)
                for line in Path(diagnostics.log_paths["jsonl"])
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            launches = [r for r in records if r["event"] == "PROCESS_STARTED"]
            self.assertEqual(len(launches), 1)
            self.assertFalse(launches[0]["shell"])
            self.assertEqual(launches[0]["creationflags"], 0)
            self.assertEqual(
                [r["event"] for r in records].count(
                    "DIAGNOSTIC_INPUT_QUARANTINE_STOP"
                ),
                1,
            )
            self.assertEqual(len(callbacks), 1)
            self.assertTrue(adapter.uninstalled)

    def test_trigger_runner_quarantines_before_macro_load_or_player_start(self):
        player = FakePlayer()
        store = FakeScriptStore()
        diagnostic = QuarantineDiagnostic()
        runner = TriggerRunner(
            FakeDetector(),
            store,
            player,
            trigger_data(),
            workflow_process_diagnostics=diagnostic,
        )
        runner.text_trigger = FakeTextTrigger()
        runner.start()
        deadline = time.monotonic() + 2.0
        while runner.is_active() and time.monotonic() < deadline:
            time.sleep(0.01)
        runner.stop()
        self.assertEqual(diagnostic.calls, 1)
        self.assertEqual(store.load_count, 0)
        self.assertEqual(player.start_count, 0)
        self.assertEqual(runner.get_status_snapshot()["macro_start_count"], 0)

    def test_diagnostic_source_contains_no_process_or_foreground_repair(self):
        source = (
            Path(__file__).parents[1]
            / "app"
            / "workflow_process_diagnostics.py"
        ).read_text(encoding="utf-8-sig")
        self.assertNotIn("CREATE_" + "NO_WINDOW", source)
        self.assertNotIn("SetForegroundWindow", source)
        self.assertNotIn("BringWindowToTop", source)


if __name__ == "__main__":
    unittest.main()
