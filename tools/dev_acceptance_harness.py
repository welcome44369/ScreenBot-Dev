"""Safe DEV acceptance windows plus a no-real-input workflow timing lane.

This tool never changes foreground, Z-order, or ScreenBot UI state.  The
interactive scenario only records clicks received by its own Tk windows.  The
autonomous scenario patches the Player's mouse module with a recording fake.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.input_safety import InputAuthorizationCode
from app.player import PlaybackStatus, ScriptPlayer
from app.script_store import ScriptStore
from app.trigger_store import TriggerStore
from app.workflow_resolver import WorkflowResolver
from app.workflow_runner import WorkflowRunner
from app.workflow_store import WorkflowStore
from tools.recording_input_backend import RecordingInputBackend


class EventLog:
    def __init__(self, output: Path, session_id: str):
        self.output = output
        self.output.mkdir(parents=True, exist_ok=True)
        self.path = self.output / "events.jsonl"
        self.session_id = session_id
        self.events: list[dict] = []

    def record(self, event: str, role: str, **fields) -> dict:
        item = {
            "timestamp_monotonic": time.monotonic(),
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
            "session_id": self.session_id,
            "window_role": role,
            "hwnd": 0,
            "foreground_hwnd": 0,
            "click_count": 0,
            "client_x": None,
            "client_y": None,
            "screen_x": None,
            "screen_y": None,
            **fields,
        }
        self.events.append(item)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        return item


class AcceptanceWindow:
    def __init__(self, root: tk.Tk, title: str, role: str, log: EventLog):
        self.role, self.log, self.count = role, log, 0
        self.window = root if role == "target" else tk.Toplevel(root)
        self.window.title(title)
        self.window.geometry("800x600" if role == "target" else "420x220")
        self.window.minsize(800, 600) if role == "target" else self.window.minsize(420, 220)
        self.window.bind("<FocusIn>", self._non_input_event)
        self.window.bind("<Configure>", self._non_input_event)
        self.heading = tk.Label(self.window, font=("Segoe UI", 17, "bold"))
        self.heading.pack(pady=(42, 14))
        self.count_label = tk.Label(self.window, font=("Segoe UI", 15))
        self.count_label.pack(pady=8)
        self.detail_label = tk.Label(self.window, justify="left")
        self.detail_label.pack(pady=3)
        self.last_click_utc = None
        self.last_click_coordinates = None
        if role == "target":
            self.heading.configure(text="SCREENBOT DEV ACCEPTANCE TARGET")
            self.session_label = tk.Label(self.window, text=f"Session: {log.session_id}")
            self.session_label.pack(pady=3)
            self.button = tk.Button(self.window, text="SAFE CLICK AREA", font=("Segoe UI", 16, "bold"), width=28, height=5)
            # The production action is target-relative (0.5, 0.5), so the
            # receiving area must include the exact client centre rather than
            # merely be visually central within the remaining packed layout.
            self.button.place(relx=0.5, rely=0.5, anchor="center")
            self.button.bind("<Button-1>", self._click)
        else:
            self.heading.configure(text="SCREENBOT DEV SAFETY OBSERVER")
            self.button = tk.Button(self.window, text="Observer client area", width=28, height=5)
            self.button.pack(expand=True)
            self.button.bind("<Button-1>", self._click)
        self._refresh()
        self.window.update_idletasks()
        self.log.record("window_ready", role, hwnd=int(self.window.winfo_id()), foreground_hwnd=int(self.window.winfo_toplevel().winfo_id()), click_count=0)

    def _non_input_event(self, _event) -> None:
        self.log.record("window_state", self.role, hwnd=int(self.window.winfo_id()), click_count=self.count)

    def _click(self, event) -> None:
        self.count += 1
        self.last_click_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.last_click_coordinates = (event.x, event.y, event.x_root, event.y_root)
        self._refresh()
        self.log.record(
            "click", self.role, hwnd=int(self.window.winfo_id()),
            foreground_hwnd=int(self.window.winfo_toplevel().winfo_id()), click_count=self.count,
            client_x=event.x, client_y=event.y, screen_x=event.x_root, screen_y=event.y_root,
        )

    def _refresh(self) -> None:
        label = "Click count" if self.role == "target" else "Unexpected click count"
        self.count_label.configure(text=f"{label}: {self.count}")
        foreground = "Yes" if self.window.focus_displayof() else "No"
        last = self.last_click_utc or "None"
        coords = self.last_click_coordinates or "None"
        self.detail_label.configure(text=f"HWND: {self.window.winfo_id()}\nForeground: {foreground}\nLast click: {last}\nCoordinates: {coords}")


def run_interactive(args) -> int:
    session_id = uuid4().hex
    output = Path(args.output) if args.output else Path(tempfile.gettempdir()) / "screenbot-dev-acceptance" / session_id
    log = EventLog(output, session_id)
    root = tk.Tk()
    target = AcceptanceWindow(root, "ScreenBot DEV Acceptance Target", "target", log)
    observer = AcceptanceWindow(root, "ScreenBot DEV Safety Observer", "observer", log)
    print("HARNESS_READY", flush=True)
    print(f"SESSION_ID={session_id}", flush=True)
    print(f"TARGET_HWND={target.window.winfo_id()}", flush=True)
    print(f"OBSERVER_HWND={observer.window.winfo_id()}", flush=True)
    print(f"OUTPUT={output}", flush=True)

    def finish() -> None:
        if observer.count:
            result = "FAIL"
        elif target.count == 0:
            result = "INCOMPLETE"
        elif target.count == 1:
            result = "PASS"
        else:
            result = "FAIL"
        summary = {
            "session_id": session_id, "scenario": args.scenario,
            "target_click_count": target.count, "observer_click_count": observer.count,
            "event_log": str(log.path),
            "result": result,
        }
        (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"HARNESS_RESULT={summary['result']}", flush=True)
        print(f"TARGET_CLICK_COUNT={target.count}", flush=True)
        print(f"OBSERVER_CLICK_COUNT={observer.count}", flush=True)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", finish)
    observer.window.protocol("WM_DELETE_WINDOW", finish)
    if not args.keep_open:
        root.after(int(args.timeout * 1000), finish)
    root.mainloop()
    return 0


@dataclass
class _Snapshot:
    session_id: str = "target-a"
    generation: int = 1
    current_client_size: tuple[int, int] = (800, 600)
    client_screen_origin: tuple[int, int] = (100, 100)


class _Session:
    def __init__(self): self.snapshot = _Snapshot()
    def get_snapshot(self): return self.snapshot


class _Gate:
    def __init__(self, backend=None): self.target_session, self.blocked, self.backend = _Session(), False, backend
    def expected_current_session(self): return self.target_session.get_snapshot()
    def _result(self, expected):
        current = self.target_session.get_snapshot()
        allowed = not self.blocked and expected.session_id == current.session_id and expected.generation == current.generation
        code = InputAuthorizationCode.ALLOWED if allowed else InputAuthorizationCode.TARGET_NOT_FOREGROUND
        if self.backend is not None:
            self.backend.record_authorization(allowed, None if allowed else code.value)
        return SimpleNamespace(allowed=allowed, code=code, snapshot=current)
    def authorize_foreground_input(self, expected): return self._result(expected)
    def authorize_foreground_input_fast(self, expected): return self._result(expected)


class _Tracker: target = SimpleNamespace(from_client_ratio=lambda x, y: (round(x * 800), round(y * 600)))
class _Detector:
    @staticmethod
    def get_capture_runtime_diagnostics(): return {}
    @staticmethod
    def invalidate_capture_target(): pass


def _wait(runner: WorkflowRunner, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while runner.is_active() and time.monotonic() < deadline:
        time.sleep(.02)
    if runner.is_active():
        raise AssertionError("workflow did not complete")


def _temporary_resolved(delay: float):
    root = tempfile.TemporaryDirectory()
    scripts, triggers, workflows = ScriptStore(root.name), TriggerStore(root.name), WorkflowStore(root.name)
    script_name = scripts.save_script({"name": "safe", "version": 1, "created_at": "x", "target_window": {}, "scan_interval": 0, "metadata": {}, "actions": [{"type": "mouse_click", "button": "left", "delay": delay, "ratio_x": .5, "ratio_y": .5}]})
    trigger_id = triggers.save_trigger({"version": 1, "name": "start", "type": "workflow_start"})
    workflow_name = workflows.save_workflow({"version": 1, "name": "workflow", "steps": [{"id": "one", "trigger_ref": trigger_id, "macro_ref": script_name[:-5]}]})
    workflow = workflows.load_workflow(workflow_name)
    identities = {
        "workflow_id": workflow["id"],
        "trigger_id": trigger_id,
        "script_id": script_name.removesuffix(".json"),
    }
    return root, scripts, WorkflowResolver(triggers, scripts).resolve(workflow), identities


def _run_recording_workflow(delay: float, mutate=None, check_before_delay: bool = False) -> dict:
    temp, scripts, workflow, identities = _temporary_resolved(delay)
    recorder = RecordingInputBackend()
    gate = _Gate(recorder)
    recorder.set_context(target_identity="target-a", target_hwnd=100, execution_id=uuid4().hex, **identities)
    player = ScriptPlayer(_Tracker(), gate)
    runner = WorkflowRunner(_Detector(), scripts, player, input_safety_gate=gate)
    started = time.monotonic()
    recorder.scheduled_timestamp = started
    with patch("app.player.mouse", recorder), patch("app.player.keyboard", recorder.keyboard):
        runner.load_workflow(workflow)
        runner.start()
        clicks_before_delay = None
        if check_before_delay:
            time.sleep(.2)
            clicks_before_delay = sum(item["action_type"] == "mouse.click" for item in recorder.records)
        if mutate:
            time.sleep(.12)
            mutate(runner, player, gate)
        _wait(runner, timeout=max(2.0, delay + 2.0))
    clicks = [item for item in recorder.records if item["action_type"] == "mouse.click"]
    status = runner.get_runtime_snapshot()
    trigger_status = status.get("trigger_runtime") or {}
    player_result = player.get_last_result()
    result = {"elapsed": (clicks[0]["emission_attempt_timestamp"] - started) if clicks else None, "clicks": len(clicks), "clicks_before_delay": clicks_before_delay, "state": runner.state.name, "finish_reason": runner.finish_reason, "start_requests": 1, "workflow_executions": 1, "trigger_fires": trigger_status.get("trigger_fire_count", 0), "script_schedules": trigger_status.get("macro_start_count", 0), "terminal_completions": int(runner.state.name == "FINISHED"), "playback_status": getattr(getattr(player_result, "status", None), "value", None), "playback_reason": getattr(player_result, "reason", None), "input_records": recorder.records}
    temp.cleanup()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="harness-self-test")
    parser.add_argument("--output")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    return run_interactive(args)


if __name__ == "__main__":
    raise SystemExit(main())
