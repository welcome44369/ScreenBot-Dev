import json
from pathlib import Path


DEFAULTS = {
    "bubble_position": [120, 120],
    "background_opacity": 0.35,
    "exit_hotkey": "esc",
    "record_mouse_moves": False,
    "record_interval_ms": 20,
}


class Settings:
    def __init__(self, root_path):
        self.root_path = Path(root_path)
        self.config_path = self.root_path / "config" / "settings.json"
        self.data = DEFAULTS.copy()
        self.load()

    def load(self):
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    self.data.update(data)
            except Exception:
                pass

    def save(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, ensure_ascii=False)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()
