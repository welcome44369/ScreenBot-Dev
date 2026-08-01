"""Focused tests for R0d-1: loop mode options, field enable/disable state,
legacy migration, and runtime enforcement of max_cycles / stop_condition."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.observation_engine import ObservationResult
from app.text_trigger import TextTrigger
from app.workflow_runner import WorkflowRunner, WorkflowState
from app.workflow_resolver import WorkflowResolver
from app.workflow_store import WorkflowStore


# ─── Minimal doubles ────────────────────────────────────────────────────────

def _obs(state):
    return ObservationResult(
        state=state,
        exact_match=state == "PRESENT",
        text_similarity=1.0 if state == "PRESENT" else 0.0,
        readability_score=0.9,
        visual_similarity=None,
        presence_score=0.9 if state == "PRESENT" else 0.1,
        observation_valid=state != "INVALID",
        reason=state,
        recognized_text="stop" if state == "PRESENT" else "",
        target_text="stop",
    )


class _Detector:
    def __init__(self, states):
        self.states = list(states)
        self.invalidated = 0

    def observe_text(self, *_args, **_kwargs):
        return _obs(self.states.pop(0))

    def invalidate_capture_target(self):
        self.invalidated += 1

    def get_capture_runtime_diagnostics(self):
        return {}


class _Player:
    def __init__(self, active=False):
        self.active = active
        self.stop_requests = 0

    def is_active(self):
        return self.active

    def stop(self):
        self.stop_requests += 1
        self.active = False

    def request_stop(self):
        self.stop_requests += 1
        self.active = False


def _make_stop_trigger_cfg():
    return {
        "id": "t_stop",
        "name": "Stop condition",
        "type": "text",
        "text": "stop",
        "event": "appear",
        "condition": {"mode": "edge", "desired_state": "present"},
        "region": {"x_ratio": 0, "y_ratio": 0, "width_ratio": 1, "height_ratio": 1},
        "poll_interval_ms": 1,
        "confirm_frames": 1,
        "cooldown_ms": 0,
        "min_absent_duration_ms": 0,
    }


def _make_runner(loop_mode, states, max_cycles=None, with_stop_trigger=False):
    cfg = _make_stop_trigger_cfg()
    runner = WorkflowRunner(_Detector(states), object(), _Player())
    runner.workflow = {"name": "T", "steps": [{"id": "s1", "trigger": cfg, "macro": "m.json"}]}
    runner.loop_mode = loop_mode
    runner.max_cycles = max_cycles
    runner.stop_trigger_enabled = with_stop_trigger
    if with_stop_trigger:
        runner.stop_trigger_config = cfg
        runner.stop_trigger_id = cfg["id"]
        runner.stop_trigger_name = cfg["name"]
        runner._stop_text_trigger = TextTrigger(
            cfg["text"],
            condition=cfg["condition"],
            condition_memory=runner._condition_memory_for("stop", cfg["id"], cfg),
            confirm_frames=1,
            cooldown_ms=0,
            min_absent_duration_ms=0,
        )
    runner.state = WorkflowState.WAIT_TRIGGER
    return runner


# ─── Test: "執行一次" removed from WorkflowManager ──────────────────────────

def test_once_mode_removed_from_workflow_editor(qt_app):
    """WorkflowManager combo must not contain any item with data matching
    the legacy "once" or "執行一次" identifiers."""
    import tempfile
    from pathlib import Path
    from app.workflow_manager import WorkflowManager

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "triggers").mkdir()
        (root / "scripts").mkdir()
        from app.trigger_store import TriggerStore
        from app.script_store import ScriptStore
        store = WorkflowStore(root)
        manager = WorkflowManager(store, TriggerStore(root), ScriptStore(root))
        data_values = {
            manager.loop_combo.itemData(i)
            for i in range(manager.loop_combo.count())
        }
        assert None not in data_values, f"Legacy 'once' (None) still in combo: {data_values}"
        assert "once" not in data_values, f"'once' still in combo: {data_values}"
        assert "執行一次" not in data_values, f"'執行一次' still in combo: {data_values}"
        expected = {"manual_stop", "max_cycles", "stop_trigger"}
        assert data_values == expected, f"Unexpected combo data: {data_values}"
        manager.close()


# ─── Test: legacy "once" migrated to max_cycles=1 ───────────────────────────

def test_legacy_once_maps_to_max_cycles_one():
    """WorkflowStore.load_workflow must remap 'once' / '執行一次' loop mode to
    max_cycles=1 before validation, without writing back the file."""
    import json

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = WorkflowStore(root)
        step = {"id": "s1", "trigger": {"type": "text", "event": "appear", "text": "t",
                                         "region": {"x_ratio":0,"y_ratio":0,"width_ratio":1,"height_ratio":1},
                                         "poll_interval_ms":500,"confirm_frames":2,"cooldown_ms":0},
                "macro": "m.json"}
        for legacy_mode in ("once", "執行一次"):
            wf = {
                "id": f"wf_{legacy_mode}",
                "name": f"Once {legacy_mode}",
                "steps": [step],
                "loop": {"mode": legacy_mode, "restart_step": "s1"},
            }
            path = store.workflows_dir / f"wf_{legacy_mode}.json"
            path.write_text(json.dumps(wf, ensure_ascii=False), encoding="utf-8")
            loaded = store.load_workflow(path.name)
            assert loaded["loop"]["mode"] == "max_cycles", \
                f"mode not migrated for {legacy_mode!r}: {loaded['loop']}"
            assert loaded["loop"]["max_cycles"] == 1, \
                f"max_cycles not defaulted to 1 for {legacy_mode!r}: {loaded['loop']}"
        # Original file must not have been modified (migration is in-memory only).
        original = json.loads((store.workflows_dir / "wf_once.json").read_text(encoding="utf-8"))
        assert original["loop"]["mode"] == "once"


# ─── Test: max_cycles control only enabled in max_cycles mode ───────────────

def test_max_cycles_enabled_only_for_max_cycles_mode(qt_app):
    """In WorkflowManager, the max_cycles spinbox must be enabled only when the
    loop mode is 'max_cycles'."""
    from app.workflow_manager import WorkflowManager

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "triggers").mkdir(); (root / "scripts").mkdir()
        from app.trigger_store import TriggerStore
        from app.script_store import ScriptStore
        store = WorkflowStore(root)
        manager = WorkflowManager(store, TriggerStore(root), ScriptStore(root))
        for mode in ("manual_stop", "stop_trigger"):
            manager.loop_combo.setCurrentIndex(manager.loop_combo.findData(mode))
            assert not manager.max_cycles.isEnabled(), \
                f"max_cycles should be disabled for mode={mode}"
        manager.loop_combo.setCurrentIndex(manager.loop_combo.findData("max_cycles"))
        assert manager.max_cycles.isEnabled(), "max_cycles should be enabled for max_cycles mode"
        manager.close()


# ─── Test: stop_condition control only enabled in stop_trigger mode ──────────

def test_stop_condition_enabled_only_for_stop_condition_mode(qt_app):
    """In WorkflowManager, stop_combo must only be enabled in 'stop_trigger' mode.
    In WorkflowEditor, stop_group must only be visible in 'stop_trigger' mode."""
    from app.workflow_manager import WorkflowManager
    from app.workflow_editor import WorkflowEditor

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "triggers").mkdir(); (root / "scripts").mkdir()
        from app.trigger_store import TriggerStore
        from app.script_store import ScriptStore
        ts = TriggerStore(root)
        ss = ScriptStore(root)
        store = WorkflowStore(root)
        manager = WorkflowManager(store, ts, ss)
        for mode in ("manual_stop", "max_cycles"):
            manager.loop_combo.setCurrentIndex(manager.loop_combo.findData(mode))
            assert not manager.stop_combo.isEnabled(), \
                f"stop_combo should be disabled for mode={mode}"
        manager.loop_combo.setCurrentIndex(manager.loop_combo.findData("stop_trigger"))
        assert manager.stop_combo.isEnabled(), "stop_combo should be enabled for stop_trigger"
        manager.close()
        editor = WorkflowEditor(store, ss, trigger_store=ts)
        assert editor.stop_group.isHidden(), "stop_group should start hidden"
        # Simulate selecting stop_trigger mode via setting loop_enable + mode.
        editor.loop_enable_chk.setChecked(True)
        editor.loop_mode_combo.setCurrentIndex(editor.loop_mode_combo.findData("stop_trigger"))
        editor._update_loop_visibility()
        assert not editor.stop_group.isHidden(), "stop_group should be visible for stop_trigger mode"
        editor.loop_mode_combo.setCurrentIndex(editor.loop_mode_combo.findData("manual_stop"))
        editor._update_loop_visibility()
        assert editor.stop_group.isHidden(), "stop_group should be hidden for manual_stop mode"
        editor._dirty = False
        editor.close()


# ─── Test: stop condition required in stop_trigger mode ─────────────────────

def test_stop_condition_required_in_stop_condition_mode(qt_app):
    """WorkflowEditor._validate must return an error when mode is stop_trigger
    but no stop_trigger_ref is selected."""
    import json
    from app.workflow_editor import WorkflowEditor

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "triggers").mkdir(); (root / "scripts").mkdir()
        from app.trigger_store import TriggerStore
        from app.script_store import ScriptStore
        ts = TriggerStore(root)
        ss = ScriptStore(root)
        store = WorkflowStore(root)
        step = {"id": "s1",
                "trigger": {"type":"text","event":"appear","text":"t",
                            "region":{"x_ratio":0,"y_ratio":0,"width_ratio":1,"height_ratio":1},
                            "poll_interval_ms":500,"confirm_frames":2,"cooldown_ms":0},
                "macro": "m.json"}
        editor = WorkflowEditor(store, ss, trigger_store=ts)
        # Draft with a valid macro path but no stop_trigger_ref.
        (root / "scripts" / "m.json").write_text('{"name":"m","version":2,"events":[]}', encoding="utf-8")
        editor._draft = {"id":"wf_v","name":"Validate test","steps":[step],
                         "loop":{"mode":"stop_trigger","restart_step":"s1","stop_trigger_ref":""}}
        editor._load_loop_settings()
        error = editor._validate()
        assert error is not None and ("stop" in error.lower() or "停止" in error), \
            f"Expected stop condition validation error, got: {error!r}"
        editor._dirty = False
        editor.close()


# ─── Test: manual_stop ignores max_cycles ────────────────────────────────────

def test_manual_stop_ignores_max_cycles():
    """A workflow with loop_mode='manual_stop' must not terminate due to max_cycles."""
    runner = _make_runner("manual_stop", [], max_cycles=1)
    runner.completed_cycles = 100
    runner._restart_step_index = 0
    # _on_cycle_boundary in manual_stop mode must call _restart_cycle, not _finish.
    called_restart = []
    called_finish = []
    runner._restart_cycle = lambda: called_restart.append(1)
    runner._finish = lambda *a, **kw: called_finish.append(a)
    runner._on_cycle_boundary()
    assert called_restart, "manual_stop should restart, not finish"
    assert not called_finish, "manual_stop must not finish based on max_cycles"


# ─── Test: max_cycles=1 runs exactly once ────────────────────────────────────

def test_max_cycles_one_runs_exactly_once():
    """A workflow with loop_mode='max_cycles' and max_cycles=1 must finish
    after the first completed cycle."""
    runner = _make_runner("max_cycles", [], max_cycles=1)
    runner._restart_step_index = 0
    runner.completed_cycles = 0
    finished = []
    runner._finish = lambda *a, **kw: finished.append(a)
    runner._restart_cycle = lambda: (_ for _ in ()).throw(AssertionError("Should not restart"))
    runner._on_cycle_boundary()
    assert finished, "Expected workflow to finish after cycle 1"
    assert finished[0][1] == "max_cycles_reached"


# ─── Test: max_cycles=N runs exactly N times ─────────────────────────────────

def test_max_cycles_n_runs_exactly_n_times():
    """WorkflowRunner must restart for cycles 1..N-1 and finish on cycle N."""
    for n in (2, 3, 5):
        runner = _make_runner("max_cycles", [], max_cycles=n)
        runner._restart_step_index = 0
        runner.completed_cycles = 0
        restarts = []
        finishes = []
        runner._restart_cycle = lambda: restarts.append(1)
        runner._finish = lambda *a, **kw: finishes.append(a)
        for _ in range(n - 1):
            runner._on_cycle_boundary()
        assert len(restarts) == n - 1, f"Expected {n-1} restarts for max_cycles={n}"
        assert not finishes, f"Should not have finished yet after {n-1} cycles"
        runner._on_cycle_boundary()
        assert finishes, f"Expected finish after cycle {n}"
        assert finishes[0][1] == "max_cycles_reached"


# ─── Test: stop_trigger_ref resolves via WorkflowResolver ────────────────────

def test_stop_trigger_reference_resolves():
    """WorkflowResolver must expand stop_trigger_ref into an inline stop_trigger
    object that contains id, name, condition, and text fields."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "triggers").mkdir(); (root / "scripts").mkdir()
        cfg = _make_stop_trigger_cfg()
        import json
        (root / "triggers" / f"{cfg['id']}.json").write_text(
            json.dumps(cfg), encoding="utf-8"
        )
        (root / "scripts" / "m.json").write_text(
            '{"name":"m","version":2,"events":[]}', encoding="utf-8"
        )
        from app.trigger_store import TriggerStore
        from app.script_store import ScriptStore
        ts = TriggerStore(root)
        ss = ScriptStore(root)
        store = WorkflowStore(root)
        wf = {
            "id": "wf_resolve",
            "name": "Resolve stop",
            "steps": [{"id": "s1", "trigger_ref": cfg["id"], "macro_ref": "m"}],
            "loop": {"mode": "stop_trigger", "restart_step": "s1",
                     "stop_trigger_ref": cfg["id"]},
        }
        store.save_workflow(wf)
        resolved = WorkflowResolver(ts, ss).resolve(store.load_workflow("wf_resolve.json"))
        st = resolved["loop"]["stop_trigger"]
        assert st.get("id") == cfg["id"]
        assert st.get("condition") == cfg["condition"]
        assert st.get("text") == cfg["text"]


# ─── Test: stop condition stops the current workflow ─────────────────────────

def test_stop_condition_stops_current_workflow():
    """When the stop trigger condition is met, the runner must set
    pending_stop and transition to STOPPED state via _perform_pending_stop."""
    runner = _make_runner("stop_trigger", ["ABSENT", "PRESENT"], with_stop_trigger=True)
    runner._restart_step_index = 0
    runner._check_stop_trigger(force=True)   # ABSENT — arms baseline
    result = runner._check_stop_trigger(force=True)  # PRESENT — matches
    assert result is True
    assert runner.pending_stop
    assert runner.stop_source == "stop_trigger"
    runner._perform_pending_stop()
    assert runner.state == WorkflowState.STOPPED
    assert runner.finish_reason == "stop_trigger"
    assert runner.terminal_claimed


# ─── Test: stop condition does NOT start the trigger's own macro ─────────────

def test_stop_condition_does_not_start_trigger_macro():
    """A matched stop trigger must not cause the player to run any macro
    or the runner to enter RUNNING_MACRO state."""
    player = _Player()
    runner = _make_runner("stop_trigger", ["ABSENT", "PRESENT"], with_stop_trigger=True)
    runner.player = player
    runner._restart_step_index = 0
    runner._check_stop_trigger(force=True)
    runner._check_stop_trigger(force=True)
    runner._perform_pending_stop()
    assert player.stop_requests >= 0  # stop may be called to cancel ongoing play
    # The workflow must NOT have transitioned through RUNNING_MACRO due to stop.
    assert runner.state in {WorkflowState.STOPPED, WorkflowState.STOPPING}
    # The runner's "macro started" flag must not have been set by the stop path.
    assert not runner._step_macro_started


# ─── Test: stop condition uses frozen TargetSession ─────────────────────────

def test_stop_condition_uses_frozen_target_session():
    """The WorkflowRunner stores an expected_target_session at load time and
    must not re-evaluate it during stop trigger detection."""
    runner = _make_runner("stop_trigger", ["ABSENT", "PRESENT"], with_stop_trigger=True)
    # Simulate a frozen target session being set before the run.
    sentinel = object()
    runner._expected_target_session = sentinel
    runner._restart_step_index = 0
    runner._check_stop_trigger(force=True)
    runner._check_stop_trigger(force=True)
    # The session reference must not have changed during polling.
    assert runner._expected_target_session is sentinel


# ─── Test: workflow_start trigger excluded from stop condition list ───────────

def test_workflow_start_trigger_excluded_from_stop_condition_list(qt_app):
    """WorkflowEditor._refresh_stop_triggers must not include any trigger of type
    'workflow_start' in the stop condition combo-box."""
    import json
    from app.workflow_editor import WorkflowEditor

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "triggers").mkdir(); (root / "scripts").mkdir()
        from app.trigger_store import TriggerStore
        from app.script_store import ScriptStore
        ts = TriggerStore(root)
        ss = ScriptStore(root)
        store = WorkflowStore(root)
        # Write one valid OCR trigger and one workflow_start trigger.
        ocr_t = _make_stop_trigger_cfg()
        ocr_t["id"] = "t_ocr"
        ocr_t["name"] = "OCR trigger"
        (root / "triggers" / "t_ocr.json").write_text(
            json.dumps(ocr_t), encoding="utf-8"
        )
        ws_t = {"id": "t_ws", "name": "Workflow start", "type": "workflow_start"}
        (root / "triggers" / "t_ws.json").write_text(
            json.dumps(ws_t), encoding="utf-8"
        )
        editor = WorkflowEditor(store, ss, trigger_store=ts)
        # Collect all item data values in the stop_ref_combo.
        combo_data = {
            editor.stop_ref_combo.itemData(i)
            for i in range(editor.stop_ref_combo.count())
        }
        assert "t_ws" not in combo_data, \
            "workflow_start trigger must not appear in stop condition combo"
        assert "t_ocr" in combo_data, \
            "OCR text trigger must be available as stop condition"
        editor._dirty = False
        editor.close()


# ─── Runner ──────────────────────────────────────────────────────────────────

def main():
    from PySide6.QtWidgets import QApplication
    qt_app = QApplication.instance() or QApplication([])

    qt_tests = {
        test_once_mode_removed_from_workflow_editor,
        test_max_cycles_enabled_only_for_max_cycles_mode,
        test_stop_condition_enabled_only_for_stop_condition_mode,
        test_stop_condition_required_in_stop_condition_mode,
        test_workflow_start_trigger_excluded_from_stop_condition_list,
    }

    tests = [
        test_once_mode_removed_from_workflow_editor,
        test_legacy_once_maps_to_max_cycles_one,
        test_max_cycles_enabled_only_for_max_cycles_mode,
        test_stop_condition_enabled_only_for_stop_condition_mode,
        test_stop_condition_required_in_stop_condition_mode,
        test_manual_stop_ignores_max_cycles,
        test_max_cycles_one_runs_exactly_once,
        test_max_cycles_n_runs_exactly_n_times,
        test_stop_trigger_reference_resolves,
        test_stop_condition_stops_current_workflow,
        test_stop_condition_does_not_start_trigger_macro,
        test_stop_condition_uses_frozen_target_session,
        test_workflow_start_trigger_excluded_from_stop_condition_list,
    ]
    for fn in tests:
        if fn in qt_tests:
            fn(qt_app)
        else:
            fn()
        print(f"  PASS  {fn.__name__}")
    print("WORKFLOW_LOOP_MODES_OK")


if __name__ == "__main__":
    main()
