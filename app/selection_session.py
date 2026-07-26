"""Temporarily removes ScreenBot UI from the desktop during OCR selection."""
import logging
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QWidget


class SelectionSession:
    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger("ScreenBot.SelectionSession")
        self._windows = []
        self._active = False

    def begin(self):
        if self._active:
            raise RuntimeError("Selection session is already active")
        self._active = True
        self.logger.info("SELECTION_SESSION_BEGIN")
        for window in QApplication.topLevelWidgets():
            if not isinstance(window, QWidget) or not window.isVisible():
                continue
            module = type(window).__module__
            if not module.startswith("app."):
                continue
            entry = {"window": window, "name": type(window).__name__, "geometry": window.geometry(), "visible": True}
            self._windows.append(entry)
            self.logger.info("SELECTION_WINDOW_HIDE name=%s visible=true", entry["name"])
            window.hide()
        QCoreApplication.processEvents()

    def end(self, preferred_focus=None):
        if not self._active:
            return
        try:
            # Restore non-focus windows first so the wizard ends up on top.
            ordered = [item for item in self._windows if item["window"] is not preferred_focus]
            ordered += [item for item in self._windows if item["window"] is preferred_focus]
            for item in ordered:
                window = item["window"]
                if window is None:
                    continue
                window.setGeometry(item["geometry"])
                window.show()
                self.logger.info("SELECTION_WINDOW_RESTORE name=%s", item["name"])
            if preferred_focus is not None:
                preferred_focus.raise_(); preferred_focus.activateWindow()
        finally:
            self._windows.clear()
            self._active = False
            self.logger.info("SELECTION_SESSION_END")
