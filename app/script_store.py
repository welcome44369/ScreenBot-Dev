import json
import math
from pathlib import Path
from uuid import uuid4


SCRIPT_REQUIRED_KEYS = ["name", "version", "created_at", "target_window", "scan_interval", "actions", "metadata"]
# New recorder format (MVP) uses 'events' list and minimal keys. validate_script accepts either format.


class ScriptStore:
    def __init__(self, root_path):
        self.root_path = Path(root_path)
        self.scripts_dir = self.root_path / "scripts"
        self.scripts_dir.mkdir(parents=True, exist_ok=True)

    def list_scripts(self):
        scripts = []
        for path in sorted(self.scripts_dir.glob("*.json")):
            try:
                data = self.load_script(path.name)
                scripts.append((path.name, data.get("name", path.stem)))
            except Exception:
                scripts.append((path.name, path.stem))
        return scripts

    def load_script(self, filename):
        path = self.scripts_dir / filename
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.validate_script(data)
        return data

    def save_script(self, data, filename=None):
        data = self._ensure_id(data)
        self.validate_script(data)
        if filename is None:
            filename = f"{data['id']}.json"
        path = self.scripts_dir / filename
        if path.exists():
            raise FileExistsError(path)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        return path.name

    def validate_script(self, data):
        if not isinstance(data, dict):
            raise ValueError("Invalid script format")
        script_id = data.get("id")
        if script_id is not None and (
            not isinstance(script_id, str) or not script_id.strip() or Path(script_id).name != script_id
        ):
            raise ValueError("Invalid script ID")
        # Accept either the legacy full script format or the new MVP recorder format
        # New format: must have 'name', 'version' and 'events' (list)
        if "events" in data and isinstance(data.get("events"), list) and "name" in data and "version" in data:
            self._validate_relative_actions(data["events"], "x_ratio", "y_ratio")
            return
        # Fallback to legacy validation
        for key in SCRIPT_REQUIRED_KEYS:
            if key not in data:
                raise ValueError(f"Missing script key: {key}")
        if not isinstance(data.get("actions"), list):
            raise ValueError("Script actions must be a list")
        self._validate_relative_actions(data["actions"], "ratio_x", "ratio_y")

    def _ensure_id(self, data):
        result = dict(data)
        script_id = result.get("id")
        if not isinstance(script_id, str) or not script_id.strip():
            result["id"] = self._new_id()
        return result

    def _new_id(self):
        while True:
            script_id = f"script_{uuid4().hex}"
            if not (self.scripts_dir / f"{script_id}.json").exists():
                return script_id

    @staticmethod
    def _validate_relative_actions(actions, x_key, y_key):
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                raise ValueError(f"Script action {index} must be an object")
            if action.get("type") not in {"click", "double_click", "mouse_click", "mouse_double"}:
                continue
            if any(key in action for key in ("x", "y", "screen_x", "screen_y", "desktop_x", "desktop_y")):
                raise ValueError(f"Script action {index} must not contain desktop coordinates")
            for key in (x_key, y_key):
                value = action.get(key)
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                    or not 0.0 <= value <= 1.0
                ):
                    raise ValueError(
                        f"Script action {index} requires {key} between 0 and 1"
                    )
