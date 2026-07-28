"""Focused checks for the compact no-activate and one-click control contract."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication

from app.floating_widget import FloatingWidget
from app.windows_no_activate import MA_NOACTIVATE


class _NoActivateAdapter:
    def __init__(self):
        self.applied = []

    def apply(self, hwnd):
        self.applied.append(hwnd)
        return True

    def has_no_activate(self, hwnd):
        return hwnd in self.applied

    def mouse_activate_result(self, _message):
        return MA_NOACTIVATE


class OneClickControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_compact_widget_reapplies_no_activate_and_returns_ma_noactivate(self):
        adapter = _NoActivateAdapter()
        with tempfile.TemporaryDirectory() as directory:
            widget = FloatingWidget(Path(directory), native_no_activate_adapter=adapter)
            widget._apply_native_no_activate()
            widget._apply_native_no_activate()

            self.assertGreaterEqual(len(adapter.applied), 2)
            self.assertTrue(widget.has_native_no_activate())
            handled, result = widget.nativeEvent(b"windows_generic_MSG", 1)
            self.assertTrue(handled)
            self.assertEqual(result, MA_NOACTIVATE)
            widget.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
