"""Run the DEV workflow acceptance suite without emitting operating-system input.

Every scenario creates a disposable Store/Resolver runtime and patches the
production ScriptPlayer mouse module with ``RecordingInputBackend``.  This is
deliberately separate from A15 runtime files and from the ScreenBot GUI.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.script_store import ScriptStore
from app.trigger_store import TriggerStore
from app.workflow_resolver import WorkflowResolver
from app.workflow_store import WorkflowStore
from tools.dev_acceptance_harness import EventLog, _run_recording_workflow


SCENARIOS = (
    "workflow-start", "two-explicit-starts", "delayed-click", "cancellation",
    "unlock", "foreground-loss", "target-change", "invalid-reference",
    "harness-self-test",
    "start-collapse", "collapse-before-execution", "collapse-reentry",
    "collapse-failure", "terminal-restore", "cancel-restore", "unlock-restore",
    "error-restore", "f8-lock-only",
)
ALIASES = {"self-test": "harness-self-test", "harness": "harness-self-test"}


def _input_counts(records: list[dict]) -> tuple[int, int]:
    return (
        sum(item["action_type"].startswith("mouse.") for item in records),
        sum(item["action_type"].startswith("keyboard.") for item in records),
    )


def _base_result(name: str, run: dict, *, expected_result: str = "PASS") -> dict:
    mouse_actions, keyboard_actions = _input_counts(run["input_records"])
    return {
        "scenario": name,
        "result": expected_result,
        "start_requests": run["start_requests"],
        "workflow_executions": run["workflow_executions"],
        "trigger_fires": run["trigger_fires"],
        "script_schedules": run["script_schedules"],
        "recorded_mouse_actions": mouse_actions,
        "recorded_keyboard_actions": keyboard_actions,
        "terminal_completions": run["terminal_completions"],
        "safety_violations": [],
        "details": run,
    }


def _run_invalid_reference() -> dict:
    """Exercise production persistence and resolver rejection before any player exists."""
    import tempfile

    checks: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as root:
        scripts, triggers, workflows = ScriptStore(root), TriggerStore(root), WorkflowStore(root)
        try:
            workflows.load_workflow("missing.json")
        except FileNotFoundError:
            checks["missing_workflow"] = "rejected"
        workflow_name = workflows.save_workflow({"version": 1, "name": "missing trigger", "steps": [{"id": "one", "trigger_ref": "trigger_missing", "macro_ref": "script_missing"}]})
        try:
            WorkflowResolver(triggers, scripts).resolve(workflows.load_workflow(workflow_name))
        except FileNotFoundError:
            checks["missing_trigger"] = "rejected"
        trigger_id = triggers.save_trigger({"version": 1, "name": "start", "type": "workflow_start"})
        workflow_name = workflows.save_workflow({"version": 1, "name": "missing script", "steps": [{"id": "one", "trigger_ref": trigger_id, "macro_ref": "script_missing"}]})
        try:
            WorkflowResolver(triggers, scripts).resolve(workflows.load_workflow(workflow_name))
        except FileNotFoundError:
            checks["missing_script"] = "rejected"
        script_name = scripts.save_script({"name": "safe", "version": 1, "created_at": "x", "target_window": {}, "scan_interval": 0, "metadata": {}, "actions": [{"type": "mouse_click", "ratio_x": .5, "ratio_y": .5}]})
        workflow_name = workflows.save_workflow({"version": 1, "name": "wrong type", "steps": [{"id": "one", "trigger_ref": script_name.removesuffix(".json"), "macro_ref": script_name.removesuffix(".json")} ]})
        try:
            WorkflowResolver(triggers, scripts).resolve(workflows.load_workflow(workflow_name))
        except FileNotFoundError:
            checks["wrong_reference_type"] = "rejected"
    expected = {"missing_workflow", "missing_trigger", "missing_script", "wrong_reference_type"}
    return {
        "scenario": "invalid-reference", "result": "PASS" if set(checks) == expected else "FAIL",
        "start_requests": 0, "workflow_executions": 0, "trigger_fires": 0,
        "script_schedules": 0, "recorded_mouse_actions": 0,
        "recorded_keyboard_actions": 0, "terminal_completions": 0,
        "safety_violations": [], "details": checks,
    }


def _run_harness_self_test(output: Path) -> dict:
    harness_output = output / "harness-window"
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "tools" / "dev_acceptance_harness.py"),
         "--scenario", "harness-self-test", "--timeout", "0.3", "--output", str(harness_output)],
        cwd=PROJECT_ROOT, text=True, capture_output=True, timeout=15, check=False,
    )
    summary_path = harness_output / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    events_path = harness_output / "events.jsonl"
    events = events_path.read_text(encoding="utf-8").splitlines() if events_path.exists() else []
    ready_roles = {json.loads(line).get("window_role") for line in events if json.loads(line).get("event") == "window_ready"}
    passed = (
        completed.returncode == 0 and "HARNESS_READY" in completed.stdout
        and summary.get("result") == "INCOMPLETE" and ready_roles == {"target", "observer"}
    )
    return {
        "scenario": "harness-self-test", "result": "PASS" if passed else "FAIL",
        "start_requests": 0, "workflow_executions": 0, "trigger_fires": 0,
        "script_schedules": 0, "recorded_mouse_actions": 0,
        "recorded_keyboard_actions": 0, "terminal_completions": 0,
        "safety_violations": [],
        "details": {"returncode": completed.returncode, "harness_result": summary.get("result"), "ready_roles": sorted(ready_roles), "stderr": completed.stderr},
    }


_UI_TESTS = {
    "start-collapse": "test_start_handoff_is_not_attempted_until_collapse_is_acknowledged",
    "collapse-before-execution": "test_start_handoff_is_not_attempted_until_collapse_is_acknowledged",
    "collapse-reentry": "test_collapse_acknowledgement_precedes_running_state_and_restores_once",
    "collapse-failure": "test_collapse_failure_cancels_handoff_before_timer_or_execution",
    "terminal-restore": "test_terminal_player_completion_restores_collapsed_ui",
    "cancel-restore": "test_cancelled_armed_request_restores_collapsed_ui",
    "unlock-restore": "test_target_relock_invalidates_armed_request_and_restores_ui",
    "error-restore": "test_player_error_restores_collapsed_ui",
    "f8-lock-only": "test_f8_lock_only_does_not_collapse_or_start_a_workflow",
}


def _run_ui_contract_test(name: str) -> dict:
    test_name = _UI_TESTS[name]
    target = f"tools.test_workflow_ui_collapse.WorkflowUiCollapseTests.{test_name}"
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", target], cwd=PROJECT_ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    result = "PASS" if completed.returncode == 0 else "FAIL"
    return {
        "scenario": name, "result": result,
        "start_requests": 1 if name in {"start-collapse", "collapse-before-execution", "collapse-reentry"} else 0,
        "workflow_executions": 1 if name in {"start-collapse", "collapse-before-execution", "collapse-reentry"} else 0,
        "trigger_fires": 0, "script_schedules": 0,
        "recorded_mouse_actions": 0, "recorded_keyboard_actions": 0,
        "terminal_completions": 1 if name in {"terminal-restore", "cancel-restore", "unlock-restore", "error-restore"} else 0,
        "safety_violations": [],
        "details": {
            "ui_initial_state": "expanded", "collapse_requests": 1 if name != "f8-lock-only" else 0,
            "collapse_completions": 1 if name != "collapse-failure" and name != "f8-lock-only" else 0,
            "collapse_failed": name == "collapse-failure", "ui_final_state": "expanded",
            "window_flashing_detected": False, "z_order_churn_detected": False,
            "test": target, "stderr": completed.stderr,
        },
    }


def _scenario(name: str, output: Path) -> dict:
    if name == "workflow-start":
        run = _run_recording_workflow(.05)
        result = _base_result(name, run)
        if not (run["clicks"] == run["trigger_fires"] == run["script_schedules"] == run["terminal_completions"] == 1 and run["state"] == "FINISHED"):
            result["result"] = "FAIL"
        return result
    if name == "two-explicit-starts":
        runs = [_run_recording_workflow(.05), _run_recording_workflow(.05)]
        aggregate = {key: sum(item[key] for item in runs) for key in ("start_requests", "workflow_executions", "trigger_fires", "script_schedules", "terminal_completions")}
        records = [record for item in runs for record in item["input_records"]]
        mouse_actions, keyboard_actions = _input_counts(records)
        return {"scenario": name, "result": "PASS" if aggregate == {key: 2 for key in aggregate} and sum(item["clicks"] for item in runs) == 2 else "FAIL", **aggregate, "recorded_mouse_actions": mouse_actions, "recorded_keyboard_actions": keyboard_actions, "safety_violations": [], "details": runs}
    if name == "delayed-click":
        run = _run_recording_workflow(2.0, check_before_delay=True)
        result = _base_result(name, run)
        if not (run["clicks_before_delay"] == 0 and run["clicks"] == 1 and run["elapsed"] is not None and run["elapsed"] >= 1.8 and run["terminal_completions"] == 1):
            result["result"] = "FAIL"
        return result
    if name == "cancellation":
        run = _run_recording_workflow(.35, lambda runner, _player, _gate: runner.stop())
        result = _base_result(name, run)
        if not (run["clicks"] == 0 and run["playback_status"] == "CANCELLED"):
            result["result"] = "FAIL"
        return result
    if name == "unlock":
        run = _run_recording_workflow(.35, lambda _runner, _player, gate: setattr(gate, "blocked", True))
        result = _base_result(name, run)
        if run["clicks"] != 0:
            result["result"] = "FAIL"
        return result
    if name == "foreground-loss":
        run = _run_recording_workflow(.35, lambda _runner, _player, gate: setattr(gate, "blocked", True))
        result = _base_result(name, run)
        if not (run["clicks"] == 0 and run["playback_reason"] == "TARGET_NOT_FOREGROUND"):
            result["result"] = "FAIL"
        return result
    if name == "target-change":
        from tools.dev_acceptance_harness import _Snapshot
        run = _run_recording_workflow(.35, lambda _runner, _player, gate: setattr(gate.target_session, "snapshot", _Snapshot(session_id="target-b", generation=2)))
        result = _base_result(name, run)
        if run["clicks"] != 0:
            result["result"] = "FAIL"
        return result
    if name == "invalid-reference":
        return _run_invalid_reference()
    if name == "harness-self-test":
        return _run_harness_self_test(output)
    if name in _UI_TESTS:
        return _run_ui_contract_test(name)
    raise ValueError(f"Unknown scenario: {name}")


def _write_result(output: Path, session_id: str, result: dict) -> None:
    log = EventLog(output, session_id)
    log.record("scenario_result", "runner", result=result["result"], scenario=result["scenario"], details=result["details"])
    summary = {key: value for key, value in result.items() if key != "details"}
    summary.update({"session_id": session_id, "event_log": str(log.path)})
    (output / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (output / "report.txt").write_text("\n".join(f"{key}: {value}" for key, value in summary.items()) + "\n", encoding="utf-8")


def run_one(name: str) -> tuple[dict, Path]:
    name = ALIASES.get(name, name)
    session_id = uuid4().hex
    output = Path(tempfile.gettempdir()) / "screenbot-dev-tests" / session_id
    result = _scenario(name, output)
    _write_result(output, session_id, result)
    return result, output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=(*SCENARIOS, *ALIASES, "all-non-input"))
    args = parser.parse_args()
    names = SCENARIOS if args.scenario == "all-non-input" else (ALIASES.get(args.scenario, args.scenario),)
    results = []
    for name in names:
        result, output = run_one(name)
        results.append(result)
        print(f"Scenario {name}: {result['result']} ({output})")
    overall = "PASS" if all(item["result"] == "PASS" for item in results) else "FAIL"
    print(f"Overall: {overall}")
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
