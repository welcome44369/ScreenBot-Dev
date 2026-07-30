"""Persistent, runtime-agnostic storage for workflow and OCR text triggers."""
import json
from pathlib import Path
from uuid import uuid4


DEFAULT_REGION = {"x_ratio": 0.0, "y_ratio": 0.0, "width_ratio": 1.0, "height_ratio": 1.0}


class TriggerStore:
    def __init__(self, root_path):
        self.root_path = Path(root_path)
        self.triggers_dir = self.root_path / "triggers"
        self.triggers_dir.mkdir(parents=True, exist_ok=True)

    def list_triggers(self):
        result = []
        for path in sorted(self.triggers_dir.glob("*.json")):
            try:
                data = self.load_trigger(path.stem)
                result.append({"id": data["id"], "name": data["name"], "filename": path.name})
            except (OSError, ValueError):
                continue
        return result

    def load_trigger(self, trigger_id):
        path = self._path(trigger_id)
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        missing_id = not isinstance(data.get("id"), str) or not data["id"].strip()
        normalized = self.validate_trigger(self._ensure_id(data))
        # Preserve legacy IDs exactly. Only a genuinely ID-less legacy file is
        # amended once so it can participate in reference-based workflows.
        if missing_id:
            generated_path = self._path(normalized["id"])
            self._write(generated_path, normalized)
            if generated_path != path:
                path.unlink()
        return normalized

    def save_trigger(self, data, trigger_id=None):
        normalized = self.validate_trigger(self._ensure_id(data))
        trigger_id = trigger_id or normalized["id"]
        if trigger_id != normalized["id"]:
            raise ValueError("trigger_id must match trigger.id")
        path = self._path(trigger_id)
        if path.exists():
            raise FileExistsError(path)
        self._write(path, normalized)
        return trigger_id

    def update_trigger(self, trigger_id, data):
        payload = dict(data)
        payload["id"] = trigger_id
        normalized = self.validate_trigger(payload)
        if normalized["id"] != trigger_id:
            raise ValueError("Trigger ID cannot change during update")
        path = self._path(trigger_id)
        if not path.exists():
            raise FileNotFoundError(path)
        self._write(path, normalized)
        return trigger_id

    def delete_trigger(self, trigger_id, workflow_store=None):
        references = self.find_workflow_references(trigger_id, workflow_store)
        if references:
            raise ValueError(f"Trigger '{trigger_id}' is referenced by: {', '.join(references)}")
        path = self._path(trigger_id)
        if not path.exists():
            raise FileNotFoundError(path)
        path.unlink()

    def find_workflow_references(self, trigger_id, workflow_store=None):
        directory = (workflow_store.workflows_dir if workflow_store else self.root_path / "workflows")
        references = []
        for path in Path(directory).glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    workflow = json.load(handle)
            except (OSError, ValueError):
                continue
            for step in workflow.get("steps", []):
                if step.get("trigger_ref") == trigger_id:
                    references.append(workflow.get("name", path.stem))
                    break
            if (workflow.get("loop") or {}).get("stop_trigger_ref") == trigger_id:
                if workflow.get("name", path.stem) not in references:
                    references.append(workflow.get("name", path.stem))
        return references

    def validate_trigger(self, data):
        if not isinstance(data, dict):
            raise ValueError("Trigger must be an object")
        for key in ("id", "name"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ValueError(f"Trigger {key} must be non-empty")
        # ID-less/type-less persisted text triggers predate the resource model.
        # Preserve their established text compatibility while rejecting unknown
        # explicit types.
        trigger_type = data.get("type", "text")
        if trigger_type not in {"text", "workflow_start"}:
            raise ValueError("Trigger type must be 'text' or 'workflow_start'")
        if trigger_type == "workflow_start":
            # This trigger is consumed only by an explicit workflow Start. It
            # intentionally has neither observation nor timing configuration.
            forbidden = {
                "event", "text", "texts", "region", "poll_interval_ms",
                "confirm_frames", "cooldown_ms", "min_absent_duration_ms",
                "match_mode", "observation", "confidence", "image_path",
                "retry_count", "timeout_polling", "auto_start", "repeat",
                "poll", "delay", "retry", "run_on_lock", "run_on_focus",
            }
            present = sorted(key for key in forbidden if key in data)
            if present:
                raise ValueError(
                    "workflow_start trigger must not contain: " + ", ".join(present)
                )
            result = dict(data)
            result["version"] = int(data.get("version", 1))
            result["type"] = "workflow_start"
            return result
        if data.get("event") not in {"appear", "disappear"}:
            raise ValueError("Trigger event must be 'appear' or 'disappear'")
        texts = data.get("texts")
        if texts is None: texts = [data.get("text", "")]
        if not isinstance(texts, list) or not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Trigger texts must contain at least one non-empty string")
        defaults = {
            "poll_interval_ms": 500,
            "confirm_frames": 2,
            "cooldown_ms": 0,
            "min_absent_duration_ms": 5000,
        }
        for key, minimum in (("poll_interval_ms", 1), ("confirm_frames", 1), ("cooldown_ms", 0), ("min_absent_duration_ms", 0)):
            value = data.get(key, defaults[key])
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise ValueError(f"Trigger {key} must be an integer >= {minimum}")
        region = data.get("region")
        if not isinstance(region, dict):
            raise ValueError("Trigger region must be an object")
        normalized_region = {}
        for key in ("x_ratio", "y_ratio", "width_ratio", "height_ratio"):
            value = region.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
                raise ValueError(f"Trigger region {key} must be between 0 and 1")
            normalized_region[key] = value
        if normalized_region["width_ratio"] <= 0 or normalized_region["height_ratio"] <= 0:
            raise ValueError("Trigger region width_ratio and height_ratio must be > 0")
        if normalized_region["x_ratio"] + normalized_region["width_ratio"] > 1:
            raise ValueError("Trigger region x + width must be <= 1")
        if normalized_region["y_ratio"] + normalized_region["height_ratio"] > 1:
            raise ValueError("Trigger region y + height must be <= 1")
        result = dict(data)
        result["version"] = int(data.get("version", 1))
        result["type"] = "text"
        result["texts"] = list(dict.fromkeys(text.strip() for text in texts))
        result["text"] = result["texts"][0]
        if result.get("match_mode", "any") != "any":
            raise ValueError("Trigger match_mode must be 'any'")
        result["match_mode"] = "any"
        result["region"] = normalized_region
        for key, default in (("poll_interval_ms", 500), ("confirm_frames", 2), ("cooldown_ms", 0), ("min_absent_duration_ms", 5000)):
            result[key] = data.get(key, default)
        return result

    def _ensure_id(self, data):
        result = dict(data)
        trigger_id = result.get("id")
        if not isinstance(trigger_id, str) or not trigger_id.strip():
            result["id"] = self._new_id()
        return result

    def _new_id(self):
        while True:
            trigger_id = f"trigger_{uuid4().hex}"
            if not self._path(trigger_id).exists():
                return trigger_id

    def _path(self, trigger_id):
        if not isinstance(trigger_id, str) or not trigger_id.strip() or Path(trigger_id).name != trigger_id:
            raise ValueError("Invalid trigger ID")
        return self.triggers_dir / f"{trigger_id}.json"

    @staticmethod
    def _write(path, data):
        with path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
