import logging

from PySide6.QtCore import QObject, Signal


class HotkeyBridge(QObject):
    f8_pressed = Signal()
    f9_pressed = Signal()
    esc_pressed = Signal()


class HotkeyManager:
    """Single-owner lifecycle for physical hotkey registrations."""
    def __init__(self, bridge: HotkeyBridge, logger: logging.Logger, exit_key="esc", on_status=None):
        self.bridge, self.logger, self.exit_key = bridge, logger, exit_key
        self.keyboard = None
        self._handles, self._available = [], False
        self._registered, self._paused = False, False
        self._on_status = on_status
        self._init_keyboard()

    def _init_keyboard(self):
        try:
            import keyboard
            self.keyboard, self._available = keyboard, True
            self.logger.info("keyboard module imported successfully")
        except Exception as exc:
            self.logger.exception("Failed to import keyboard module: %s", exc)

    def _status(self, event, **data):
        if callable(self._on_status): self._on_status(event, **data)

    def register_hotkeys(self):
        if self._registered:
            return True
        if not self._available:
            message = "keyboard module not available"
            self.logger.error("HOTKEY_REGISTRATION_FAILED key=F8 error_type=Unavailable message=%s", message)
            self._status("HOTKEY_REGISTRATION_FAILED", key="F8", error_type="Unavailable", message=message)
            return False
        try:
            self.logger.info("HOTKEY_REGISTER_ATTEMPT key=F8")
            f8 = self.keyboard.add_hotkey("f8", lambda: self.bridge.f8_pressed.emit())
        except Exception as exc:
            self.logger.error("HOTKEY_REGISTRATION_FAILED key=F8 error_type=%s message=%s", type(exc).__name__, exc)
            self._status("HOTKEY_REGISTRATION_FAILED", key="F8", error_type=type(exc).__name__, message=str(exc))
            return False
        self._handles = [f8]
        self.logger.info("HOTKEY_REGISTERED key=F8")
        self._status("HOTKEY_REGISTERED", key="F8")
        for key, signal in (("f9", self.bridge.f9_pressed), (self.exit_key, self.bridge.esc_pressed)):
            try: self._handles.append(self.keyboard.add_hotkey(key, lambda signal=signal: signal.emit()))
            except Exception as exc: self.logger.error("HOTKEY_REGISTRATION_FAILED key=%s error_type=%s message=%s", key.upper(), type(exc).__name__, exc)
        self._registered, self._paused = True, False
        return True

    def unregister_all(self):
        if not self._registered: return False
        for handle in list(self._handles):
            try: self.keyboard.remove_hotkey(handle)
            except Exception: self.logger.exception("Failed to unregister hotkey")
        self._handles.clear(); self._registered = False
        self.logger.info("HOTKEY_UNREGISTERED key=F8")
        self._status("HOTKEY_UNREGISTERED", key="F8")
        return True

    def pause(self, owner="unknown"):
        if self._paused: return False
        self.unregister_all(); self._paused = True
        self.logger.info("HOTKEY_PAUSED key=F8 owner=%s", owner)
        return True

    def resume(self, owner="unknown"):
        if not self._paused: return False
        if self.register_hotkeys():
            self.logger.info("HOTKEY_RESUMED key=F8 owner=%s", owner)
            return True
        return False

    def is_available(self): return self._available
