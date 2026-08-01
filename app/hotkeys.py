import logging
import threading

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
        self._f8_down_handle = None
        self._f8_up_handle = None
        self._other_handles = []
        self._available = False
        self._registered, self._paused = False, False
        self._f8_latched = False
        self._latch_lock = threading.Lock()
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

    def _on_f8_down(self):
        with self._latch_lock:
            if self._paused or self._f8_latched:
                return
            self._f8_latched = True
        self.bridge.f8_pressed.emit()

    def _on_f8_up(self, _event=None):
        with self._latch_lock:
            self._f8_latched = False

    def _dispatch_if_active(self, signal):
        if not self._paused:
            signal.emit()

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
            self._f8_down_handle = self.keyboard.add_hotkey(
                "f8", self._on_f8_down
            )
            self._f8_up_handle = self.keyboard.on_release_key(
                "f8", self._on_f8_up, suppress=False
            )
        except Exception as exc:
            if self._f8_down_handle is not None:
                try:
                    self.keyboard.remove_hotkey(self._f8_down_handle)
                except Exception:
                    self.logger.exception("Failed to roll back F8 key-down hook")
            self._f8_down_handle = None
            self._f8_up_handle = None
            self.logger.error("HOTKEY_REGISTRATION_FAILED key=F8 error_type=%s message=%s", type(exc).__name__, exc)
            self._status("HOTKEY_REGISTRATION_FAILED", key="F8", error_type=type(exc).__name__, message=str(exc))
            return False
        self.logger.info("HOTKEY_REGISTERED key=F8")
        self._status("HOTKEY_REGISTERED", key="F8")
        for key, signal in (("f9", self.bridge.f9_pressed), (self.exit_key, self.bridge.esc_pressed)):
            try:
                self._other_handles.append(
                    self.keyboard.add_hotkey(
                        key,
                        lambda signal=signal: self._dispatch_if_active(signal),
                    )
                )
            except Exception as exc: self.logger.error("HOTKEY_REGISTRATION_FAILED key=%s error_type=%s message=%s", key.upper(), type(exc).__name__, exc)
        self._registered, self._paused = True, False
        self._on_f8_up()
        return True

    def unregister_all(self):
        if not self._registered: return False
        if self._f8_down_handle is not None:
            try:
                self.keyboard.remove_hotkey(self._f8_down_handle)
            except Exception:
                self.logger.exception("Failed to unregister F8 key-down hook")
        if self._f8_up_handle is not None:
            try:
                self.keyboard.unhook(self._f8_up_handle)
            except Exception:
                self.logger.exception("Failed to unregister F8 key-up hook")
        for handle in list(self._other_handles):
            try:
                self.keyboard.remove_hotkey(handle)
            except Exception:
                self.logger.exception("Failed to unregister hotkey")
        self._f8_down_handle = None
        self._f8_up_handle = None
        self._other_handles.clear()
        self._registered = False
        self._on_f8_up()
        self.logger.info("HOTKEY_UNREGISTERED key=F8")
        self._status("HOTKEY_UNREGISTERED", key="F8")
        return True

    def pause(self, owner="unknown"):
        if self._paused: return False
        self._paused = True
        self._on_f8_up()
        self.logger.info("HOTKEY_PAUSED key=F8 owner=%s", owner)
        return True

    def resume(self, owner="unknown"):
        if not self._paused: return False
        if not self._registered and not self.register_hotkeys():
            return False
        self._paused = False
        self._on_f8_up()
        self.logger.info("HOTKEY_RESUMED key=F8 owner=%s", owner)
        return True

    def is_available(self): return self._available
