"""Shared non-modal window behaviour for ScreenBot resource managers."""
import ctypes
import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog


class ManagerWindowBase(QDialog):
    """A single manager window stays usable beside the Floating Widget."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowModality(Qt.WindowModality.NonModal)

    def bring_to_front(self):
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        QTimer.singleShot(0, self.raise_)
        QTimer.singleShot(0, self.activateWindow)
        if os.name == "nt":
            try:
                ctypes.windll.user32.SetForegroundWindow(int(self.winId()))
            except Exception:
                pass
