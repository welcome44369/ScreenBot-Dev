import ctypes
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from app.application import ScreenBotApp

MUTEX_NAME = "ScreenBot_SingleInstance_Mutex"


def _enable_per_monitor_dpi_awareness():
    """Keep pynput screen coordinates aligned with Qt/Win32 client ratios."""
    try:
        # PER_MONITOR_AWARE_V2 must be set before QApplication is created.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _create_mutex():
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    last_error = kernel32.GetLastError()
    if mutex == 0 or last_error == 183:
        return None
    return mutex


def _show_already_running():
    app = QApplication([])
    QMessageBox.warning(None, "ScreenBot", "ScreenBot 已經在執行中。")
    sys.exit(0)


def main():
    _enable_per_monitor_dpi_awareness()
    mutex = _create_mutex()
    if mutex is None:
        _show_already_running()
    if getattr(sys, "frozen", False):
        app_dir = Path(sys.argv[0]).resolve().parent
    else:
        app_dir = Path(__file__).resolve().parent
    screen_bot = ScreenBotApp(app_dir)
    screen_bot.run()


if __name__ == "__main__":
    main()
