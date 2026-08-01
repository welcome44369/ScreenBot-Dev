import json
import logging
from pathlib import Path
from uuid import uuid4


class WorkflowStore:
    def __init__(self, root_path, logger=None):
        self.root_path = Path(root_path)
        self.workflows_dir = self.root_path / "workflows"
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger or logging.getLogger("ScreenBot")

    def list_workflows(self):
        items = []
        for path in sorted(self.workflows_dir.glob("*.json")):
            try:
                data = self._load_json(path)
                self._validate_workflow(data, path.name)
                items.append({"filename": path.name, "name": data.get("name", path.stem)})
            except Exception as exc:
                self.logger.warning("Invalid workflow json skipped: %s (%s)", path.name, exc)
        return items

    def load_workflow(self, filename):
        path = self.workflows_dir / filename
        if not path.exists():
            raise FileNotFoundError(path)
        data = self._load_json(path)
        data = self._migrate_legacy_loop(data)
        self._validate_workflow(data, filename)
        return data

    @staticmethod
    def _migrate_legacy_loop(data):
        """Remap legacy loop modes to current canonical representations."""
        loop = data.get("loop")
        if not isinstance(loop, dict):
            return data
        mode = loop.get("mode")
        if mode in ("once", "執行一次"):
            migrated = dict(data)
            migrated["loop"] = {
                **loop,
                "mode": "max_cycles",
                "max_cycles": loop.get("max_cycles") if isinstance(loop.get("max_cycles"), int) else 1,
            }
            return migrated
        return data

    def _load_json(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def save_workflow(self, data, filename=None):
        """Write workflow data to workflows/filename (creates or overwrites the file)."""
        data = self._ensure_id(data)
        filename = filename or f"{data['id']}.json"
        self._validate_workflow(data, filename)
        path = self.workflows_dir / filename
        with open(path, "w", encoding="utf-8") as fh:
            import json as _json
            _json.dump(data, fh, indent=2, ensure_ascii=False)
        self.logger.info("Workflow saved: %s", filename)
        return filename

    def _ensure_id(self, data):
        result = dict(data)
        workflow_id = result.get("id")
        if not isinstance(workflow_id, str) or not workflow_id.strip():
            result["id"] = self._new_id()
        return result

    def _new_id(self):
        while True:
            workflow_id = f"workflow_{uuid4().hex}"
            if not (self.workflows_dir / f"{workflow_id}.json").exists():
                return workflow_id

    def _validate_workflow(self, data, filename):
        if not isinstance(data, dict):
            raise ValueError(f"{filename}: workflow must be an object")
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{filename}: missing workflow name")
        steps = data.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"{filename}: workflow steps must be a non-empty array")

        step_ids = set()
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"{filename}: step {index} must be an object")
            step_id = step.get("id")
            if not isinstance(step_id, str) or not step_id.strip():
                raise ValueError(f"{filename}: step {index} missing id")
            if step_id in step_ids:
                raise ValueError(f"{filename}: duplicate step id: {step_id}")
            step_ids.add(step_id)

        # Workflows without this optional block are legacy single-run files and
        # intentionally remain valid.
        loop = data.get("loop")
        if loop is None:
            return
        if not isinstance(loop, dict):
            raise ValueError(f"{filename}: loop must be an object")
        mode = loop.get("mode")
        # A loop with only stop_trigger_ref and no mode is a valid legacy
        # one-shot-with-stop pattern.
        if mode is None:
            if not isinstance(loop.get("stop_trigger_ref"), str) or not loop["stop_trigger_ref"].strip():
                raise ValueError(f"{filename}: loop without mode must have a non-empty stop_trigger_ref")
            return
        if mode not in {"manual_stop", "max_cycles", "stop_trigger"}:
            raise ValueError(f"{filename}: unsupported loop mode: {mode}")
        restart_step = loop.get("restart_step")
        if not isinstance(restart_step, str) or restart_step not in step_ids:
            raise ValueError(f"{filename}: loop restart step does not exist: {restart_step}")
        if mode == "max_cycles":
            max_cycles = loop.get("max_cycles")
            if not isinstance(max_cycles, int) or isinstance(max_cycles, bool) or max_cycles < 1:
                raise ValueError(f"{filename}: loop max_cycles must be an integer >= 1")
        if mode == "stop_trigger":
            stop_ref = loop.get("stop_trigger_ref")
            stop_trigger = loop.get("stop_trigger")
            if not isinstance(stop_ref, str) and not isinstance(stop_trigger, dict):
                raise ValueError(f"{filename}: stop_trigger mode requires stop_trigger_ref or stop_trigger")
            if isinstance(stop_ref, str) and not stop_ref.strip():
                raise ValueError(f"{filename}: loop stop_trigger_ref cannot be empty")
            if isinstance(stop_trigger, dict):
                if stop_trigger.get("type", "text") != "text":
                    raise ValueError(f"{filename}: stop_trigger supports only text type")
                if stop_trigger.get("event") not in {"appear", "disappear"}:
                    raise ValueError(f"{filename}: stop_trigger event must be appear/disappear")
                if not isinstance(stop_trigger.get("text"), str) or not stop_trigger["text"].strip():
                    raise ValueError(f"{filename}: stop_trigger text must be non-empty")
                region = stop_trigger.get("region")
                if not isinstance(region, dict) or any(
                    not isinstance(region.get(key), (int, float))
                    for key in ("x_ratio", "y_ratio", "width_ratio", "height_ratio")
                ):
                    raise ValueError(f"{filename}: stop_trigger region must contain numeric ratios")
                for key, minimum in (("poll_interval_ms", 1), ("confirm_frames", 1), ("cooldown_ms", 0)):
                    value = stop_trigger.get(key, 500 if key == "poll_interval_ms" else (2 if key == "confirm_frames" else 0))
                    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                        raise ValueError(f"{filename}: stop_trigger {key} is invalid")
