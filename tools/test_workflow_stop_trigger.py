"""Focused no-I/O regression checks for workflow-level text stop triggers."""
from __future__ import annotations

import sys
import tempfile
import os
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


def observation(state, text=""):
    return ObservationResult(
        state=state,
        exact_match=state == "PRESENT",
        text_similarity=1.0 if state == "PRESENT" else 0.0,
        readability_score=.9,
        visual_similarity=None,
        presence_score=.9 if state == "PRESENT" else .1,
        observation_valid=state != "INVALID",
        reason=state,
        recognized_text=text,
        target_text="金錢不足",
    )


class Detector:
    def __init__(self, states):
        self.states = list(states)
        self.invalidated = 0

    def observe_text(self, *_args, **_kwargs):
        state = self.states.pop(0)
        return observation(state, "金錢不足" if state == "PRESENT" else "")

    def invalidate_capture_target(self):
        self.invalidated += 1

    def get_capture_runtime_diagnostics(self):
        return {}


class Player:
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


def make_runner(states, condition, frames=2, player=None):
    cfg = {
        "id": "trigger_money",
        "name": "金錢不足停止",
        "type": "text",
        "text": "金錢不足",
        "condition": condition,
        "region": {"x_ratio": 0, "y_ratio": 0, "width_ratio": 1, "height_ratio": 1},
        "poll_interval_ms": 1,
        "confirm_frames": frames,
        "cooldown_ms": 0,
        "min_absent_duration_ms": 0,
    }
    runner = WorkflowRunner(Detector(states), object(), player or Player())
    runner.workflow = {"name": "Stop test", "steps": [{"id": "step_1", "trigger": cfg, "macro": "macro.json"}]}
    runner.loop_mode = "stop_trigger"
    runner.stop_trigger_enabled = True
    runner.stop_trigger_config = cfg
    runner.stop_trigger_id = cfg["id"]
    runner.stop_trigger_name = cfg["name"]
    runner.state = WorkflowState.WAIT_TRIGGER
    runner._stop_text_trigger = TextTrigger(
        cfg["text"],
        condition=cfg["condition"],
        condition_memory=runner._condition_memory_for("stop", cfg["id"], cfg),
        confirm_frames=frames,
        cooldown_ms=0,
        min_absent_duration_ms=0,
    )
    return runner


def poll(runner, count):
    return [runner._check_stop_trigger(force=True) for _ in range(count)]


def main():
    # Pure 0->1 stop: confirmed initial ABSENT arms, confirmed PRESENT stops once.
    runner = make_runner(["ABSENT", "ABSENT", "PRESENT", "PRESENT"], {"mode": "edge", "desired_state": "present"})
    assert poll(runner, 3) == [False, False, False]
    assert poll(runner, 1) == [True]
    assert runner.pending_stop and runner.stop_source == "stop_trigger"
    runner._perform_pending_stop()
    assert runner.state == WorkflowState.STOPPED and runner.finish_reason == "stop_trigger"
    assert runner.terminal_claimed and runner.new_work_blocked
    assert runner._check_stop_trigger(force=True) is False

    # Initial PRESENT is only a baseline for the new edge-present condition.
    runner = make_runner(["PRESENT", "PRESENT"], {"mode": "edge", "desired_state": "present"})
    assert poll(runner, 2) == [False, False]
    assert not runner.pending_stop

    # State-present intentionally matches on the first confirmed PRESENT.
    runner = make_runner(["PRESENT", "PRESENT"], {"mode": "state", "desired_state": "present"})
    assert poll(runner, 2) == [False, True]

    # UNCERTAIN/INVALID never complete a stop condition.
    runner = make_runner(["ABSENT", "UNCERTAIN", "INVALID", "PRESENT"], {"mode": "edge", "desired_state": "present"})
    assert not any(poll(runner, 4))
    assert not runner.pending_stop

    # A macro is cancelled by the existing safe player stop interface and no
    # next cycle is permitted once the first stop source wins.
    player = Player(active=True)
    runner = make_runner(["PRESENT"], {"mode": "state", "desired_state": "present"}, frames=1, player=player)
    assert poll(runner, 1) == [True]
    runner.state = WorkflowState.RUNNING_MACRO
    runner._perform_pending_stop()
    assert player.stop_requests == 1
    assert runner.state == WorkflowState.STOPPED

    # Manual and global stops use the same immediate terminal-claim gate.
    runner = make_runner(["ABSENT"], {"mode": "edge", "desired_state": "present"})
    assert runner.request_immediate_stop("manual_stop", {"origin": "test"}) is True
    assert runner.stop_source == "manual_stop"
    assert runner.request_immediate_stop("stop_trigger") is False

    # Resource-reference workflows retain six-condition configuration and a
    # missing reference is rejected by the resolver before a run can start.
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "triggers").mkdir()
        trigger_id = "trigger_money"
        (root / "triggers" / f"{trigger_id}.json").write_text(
            '''{"id":"trigger_money","name":"Money stop","type":"text","text":"金錢不足","condition":{"mode":"edge","desired_state":"present"},"region":{"x_ratio":0,"y_ratio":0,"width_ratio":1,"height_ratio":1},"poll_interval_ms":500,"confirm_frames":2,"cooldown_ms":0,"min_absent_duration_ms":0}''',
            encoding="utf-8",
        )
        (root / "scripts").mkdir()
        (root / "scripts" / "macro.json").write_text('{"name":"test","version":2,"events":[]}', encoding="utf-8")
        from app.trigger_store import TriggerStore
        from app.script_store import ScriptStore
        trigger_store = TriggerStore(root)
        store = WorkflowStore(root)
        workflow = {
            "id": "workflow_stop", "name": "Reference stop", "steps": [
                {"id": "step_1", "trigger_ref": trigger_id, "macro_ref": "macro"}
            ],
            "loop": {"mode": "stop_trigger", "restart_step": "step_1", "stop_trigger_ref": trigger_id},
        }
        store.save_workflow(workflow)
        resolved = WorkflowResolver(trigger_store, ScriptStore(root)).resolve(store.load_workflow("workflow_stop.json"))
        assert resolved["loop"]["stop_trigger"]["id"] == trigger_id
        assert resolved["loop"]["stop_trigger"]["condition"] == {"mode": "edge", "desired_state": "present"}

        # Global stop is independent from normal looping.  A loop object with
        # only the canonical reference is an explicit one-shot workflow.
        one_shot = {
            "id": "workflow_one_shot_stop", "name": "One-shot stop", "steps": [
                {"id": "step_1", "trigger_ref": trigger_id, "macro_ref": "macro"}
            ],
            "loop": {"stop_trigger_ref": trigger_id},
        }
        store.save_workflow(one_shot)
        one_shot_resolved = WorkflowResolver(trigger_store, ScriptStore(root)).resolve(
            store.load_workflow("workflow_one_shot_stop.json")
        )
        assert one_shot_resolved["loop"]["stop_trigger"]["id"] == trigger_id
        loaded_runner = WorkflowRunner(Detector(["ABSENT"]), object(), Player())
        loaded_runner.load_workflow(one_shot_resolved)
        assert loaded_runner.loop_mode is None
        assert loaded_runner.stop_trigger_enabled is True

        for mode, extra in (
            ("manual_stop", {}),
            ("max_cycles", {"max_cycles": 2}),
        ):
            loop = {"mode": mode, "restart_step": "step_1", "stop_trigger_ref": trigger_id, **extra}
            candidate = {**workflow, "id": f"workflow_{mode}_stop", "loop": loop}
            store.save_workflow(candidate)
            candidate_resolved = WorkflowResolver(trigger_store, ScriptStore(root)).resolve(
                store.load_workflow(f"workflow_{mode}_stop.json")
            )
            candidate_runner = WorkflowRunner(Detector(["ABSENT"]), object(), Player())
            candidate_runner.load_workflow(candidate_resolved)
            assert candidate_runner.stop_trigger_enabled is True
        from PySide6.QtWidgets import QApplication
        from app.workflow_editor import WorkflowEditor
        from app.workflow_manager import WorkflowManager
        qt_app = QApplication.instance() or QApplication([])
        manager = WorkflowManager(store, trigger_store, ScriptStore(root))
        for mode in (None, "manual_stop", "max_cycles"):
            manager.loop_combo.setCurrentIndex(manager.loop_combo.findData(mode))
            assert manager.stop_combo.isEnabled()
        manager.name_edit.setText("Manager one-shot stop")
        manager.trigger_combo.setCurrentIndex(manager.trigger_combo.findData(trigger_id))
        manager.macro_combo.setCurrentIndex(manager.macro_combo.findData("macro"))
        manager.stop_combo.setCurrentIndex(manager.stop_combo.findData(trigger_id))
        manager.loop_combo.setCurrentIndex(manager.loop_combo.findData(None))
        manager._save()
        manager_saved = next(item for item in store.list_workflows() if item["name"] == "Manager one-shot stop")
        assert store.load_workflow(manager_saved["filename"])["loop"] == {"stop_trigger_ref": trigger_id}
        manager.name_edit.setText("Manager manual no stop")
        manager.stop_combo.setCurrentIndex(manager.stop_combo.findData(None))
        manager.loop_combo.setCurrentIndex(manager.loop_combo.findData("manual_stop"))
        manager._save()
        manager_cleared = next(item for item in store.list_workflows() if item["name"] == "Manager manual no stop")
        assert "stop_trigger_ref" not in store.load_workflow(manager_cleared["filename"])["loop"]
        manager.close()
        editor = WorkflowEditor(store, ScriptStore(root), trigger_store=trigger_store)
        assert not editor.stop_group.isVisible()
        editor._draft = one_shot
        editor._load_loop_settings()
        editor._commit_loop_settings()
        assert editor._draft["loop"]["stop_trigger_ref"] == trigger_id
        assert "mode" not in editor._draft["loop"]
        editor.close()
        del qt_app

    print("WORKFLOW_STOP_TRIGGER_OK")


if __name__ == "__main__":
    main()
