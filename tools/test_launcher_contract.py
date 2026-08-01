import sys
from pathlib import Path
import unittest


class LauncherContractTests(unittest.TestCase):
    def test_launcher_sets_project_root(self):
        launcher_path = Path(__file__).resolve().parents[1] / "launcher.bat"
        content = launcher_path.read_text(encoding="utf-8")
        self.assertIn('set "SCREENBOT_ROOT=%~dp0"', content)
        self.assertIn('cd /d "%SCREENBOT_ROOT%"', content)

    def test_launcher_targets_dev_main(self):
        launcher_path = Path(__file__).resolve().parents[1] / "launcher.bat"
        content = launcher_path.read_text(encoding="utf-8")
        self.assertIn('set "MAIN_PY=%SCREENBOT_ROOT%main.py"', content)
        self.assertIn('start "" "%PYTHON_EXE%" "%MAIN_PY%"', content)

    def test_launcher_does_not_reference_legacy_paths(self):
        launcher_path = Path(__file__).resolve().parents[1] / "launcher.bat"
        content = launcher_path.read_text(encoding="utf-8")
        self.assertNotIn("F:\\ScreenBot", content)
        self.assertNotIn("ScreenBot_stable", content)
        self.assertNotIn("pythonw main.py", content)
        self.assertNotIn("python main.py", content)
        self.assertNotIn(".venv\\Scripts\\pythonw.exe", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
