import json
from pathlib import Path
from datetime import datetime


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
        if filename is None:
            safe_name = data.get("name", "script").strip().replace(" ", "_")
            timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            filename = f"{timestamp}_{safe_name}.json"
        path = self.scripts_dir / filename
        if path.exists():
            raise FileExistsError(path)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        return path.name

    def validate_script(self, data):
        if not isinstance(data, dict):
            raise ValueError("Invalid script format")
        # Accept either the legacy full script format or the new MVP recorder format
        # New format: must have 'name', 'version' and 'events' (list)
        if "events" in data and isinstance(data.get("events"), list) and "name" in data and "version" in data:
            return
        # Fallback to legacy validation
        for key in SCRIPT_REQUIRED_KEYS:
            if key not in data:
                raise ValueError(f"Missing script key: {key}")
        if not isinstance(data.get("actions"), list):
            raise ValueError("Script actions must be a list")
