import logging
import traceback

from PySide6.QtCore import QObject, Signal


class HotkeyBridge(QObject):
    f8_pressed = Signal()
    f9_pressed = Signal()
    esc_pressed = Signal()


class HotkeyManager:
    def __init__(self, bridge: HotkeyBridge, logger: logging.Logger, exit_key: str = "esc"):
        self.bridge = bridge
        self.logger = logger
        self.exit_key = exit_key
        self.keyboard = None
        self._handles = []
        self._available = False
        self._init_keyboard()

    def _init_keyboard(self):
        try:
            import keyboard

            self.keyboard = keyboard
            self._available = True
            self.logger.info("keyboard module imported successfully")
        except Exception as exc:
            self.logger.exception("Failed to import keyboard module: %s", exc)
            self._available = False

    def register_hotkeys(self):
        if not self._available:
            self.logger.error("keyboard module not available, cannot register hotkeys")
            return
        try:
            # Keep the returned handles so they won't be GC'd and can be removed later
            h8 = self.keyboard.add_hotkey("f8", lambda: self.bridge.f8_pressed.emit())
            self._handles.append(h8)
            self.logger.info("F8 registered: %s", str(h8))
        except Exception:
            self.logger.exception("Failed to register F8 hotkey")

        try:
            h9 = self.keyboard.add_hotkey("f9", lambda: self.bridge.f9_pressed.emit())
            self._handles.append(h9)
            self.logger.info("F9 registered: %s", str(h9))
        except Exception:
            self.logger.exception("Failed to register F9 hotkey")

        try:
            hexit = self.keyboard.add_hotkey(self.exit_key, lambda: self.bridge.esc_pressed.emit())
            self._handles.append(hexit)
            self.logger.info("Exit hotkey registered (%s): %s", self.exit_key, str(hexit))
        except Exception:
            self.logger.exception("Failed to register exit hotkey: %s", self.exit_key)

    def unregister_all(self):
        if not self._available:
            self.logger.info("keyboard module not available, nothing to unregister")
            return
        try:
            # Remove by handles if possible
            for h in list(self._handles):
                try:
                    self.keyboard.remove_hotkey(h)
                    self.logger.info("Removed hotkey handle %s", str(h))
                except Exception:
                    # fallback: try to clear all
                    self.logger.debug("Failed remove handle %s", str(h))
            self._handles.clear()
        except Exception:
            self.logger.exception("Error while unregistering hotkeys")

    def is_available(self):
        return self._available
