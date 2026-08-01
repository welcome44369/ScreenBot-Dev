"""Single direct-HWND capture service shared by creation and runtime polling."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import re
import sys
import time
import traceback

from PIL import ImageStat
from PySide6.QtWidgets import QApplication

from app.capture_backends import CaptureBackendError, CaptureResult, create_backend


user32 = ctypes.WinDLL("user32", use_last_error=True)


class TargetCaptureService:
    """Validate a locked target and select a direct capture backend."""

    DEFAULT_BACKEND_CHAIN = ("winsdk-wgc", "printwindow", "bitblt")

    def __init__(self, window_tracker, logger=None, diagnostic_log_path=None):
        self.window_tracker = window_tracker
        self.logger = logger or logging.getLogger("ScreenBot.TargetCapture")
        self._environment_logged = False
        self._diagnostic_handler = None
        self.diagnostic_log_path = Path(
            diagnostic_log_path or Path.cwd() / "logs" / "TargetCapture.log"
        )
        self._backend_instances = {}
        self._backend_cache = {}
        self._runtime_capture_count = 0
        self._runtime_session_recreate_count = 0
        self._runtime_last_backend = None
        self._runtime_last_session_id = None
        # Capture code deliberately has no permission to mutate any Qt/Win32
        # window state.  These counters remain zero unless a future caller
        # explicitly reports an exceptional UI mutation through this service.
        self._runtime_ui_mutations = {
            "window_visibility_change_count": 0,
            "foreground_change_count": 0,
            "overlay_create_count": 0,
            "overlay_show_count": 0,
            "preview_window_recreated": 0,
        }
        self._ensure_diagnostic_log()

    def _ensure_diagnostic_log(self):
        self.diagnostic_log_path.parent.mkdir(parents=True, exist_ok=True)
        resolved = str(self.diagnostic_log_path.resolve())
        for handler in self.logger.handlers:
            if getattr(handler, "_screenbot_target_capture_path", None) == resolved:
                return
        handler = RotatingFileHandler(
            resolved, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler._screenbot_target_capture_path = resolved
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.DEBUG)
        self._diagnostic_handler = handler

    def _backend(self, name):
        backend = self._backend_instances.get(name)
        if backend is None:
            backend = create_backend(name, self.logger)
            self._backend_instances[name] = backend
        return backend

    def capture_target_client(
        self, target_hwnd, *, allow_desktop_fallback=False, force_backend=None
    ):
        return self.capture_target_client_result(
            target_hwnd,
            allow_desktop_fallback=allow_desktop_fallback,
            force_backend=force_backend,
        ).image

    def capture_target_client_result(
        self, target_hwnd, *, allow_desktop_fallback=False, force_backend=None
    ) -> CaptureResult:
        if allow_desktop_fallback:
            raise ValueError("Desktop fallback is disabled for target OCR capture")
        if not isinstance(target_hwnd, int) or not target_hwnd:
            raise ValueError("A locked target HWND is required for capture")
        target = self.window_tracker.refresh()
        hwnd = int(target_hwnd)
        if hwnd != int(target.hwnd):
            raise RuntimeError("Capture HWND does not match the immutable locked target")
        screenbot_hwnds = {
            int(widget.winId())
            for widget in QApplication.topLevelWidgets()
            if widget is not None
        }
        is_screenbot = target.pid == os.getpid() or hwnd in screenbot_hwnds
        self._log_environment_once()
        self._log_target(target, hwnd, is_screenbot)
        if is_screenbot:
            raise RuntimeError(
                "The locked target is a ScreenBot window. "
                "Lock an external target window first."
            )
        if target.client_width <= 1 or target.client_height <= 1:
            raise RuntimeError("Locked target has an invalid client size")

        key = (hwnd, int(target.pid))
        force_backend = force_backend or os.environ.get("SCREENBOT_CAPTURE_BACKEND")
        if force_backend:
            names = (self._normalize_backend_name(force_backend),)
            self.logger.info("TARGET_CAPTURE_FORCE_BACKEND %s", names[0])
        else:
            cached = self._backend_cache.get(key)
            if cached:
                self.logger.info(
                    "TARGET_CAPTURE_CACHE_HIT hwnd=0x%08X pid=%s backend=%s",
                    hwnd,
                    target.pid,
                    cached,
                )
            names = tuple(
                dict.fromkeys(
                    ([cached] if cached else []) + list(self.DEFAULT_BACKEND_CHAIN)
                )
            )
            self.logger.info(
                "TARGET_CAPTURE_BACKEND_CHAIN %s cached=%s",
                names,
                cached,
            )

        failures = []
        self.logger.info("TARGET_CAPTURE_START hwnd=0x%08X", hwnd)
        for name in names:
            try:
                backend = self._backend(name)
                self.logger.info("TARGET_CAPTURE_BACKEND_ATTEMPT %s", name)
                result = backend.capture_client(hwnd)
                valid, reason = self._validate_result(
                    result, target.client_width, target.client_height
                )
                if not valid:
                    raise CaptureBackendError(
                        name, "image_validation", reason
                    )
                self._backend_cache[key] = name
                self._runtime_capture_count += 1
                frame_sequence = result.metadata.get("frame_sequence")
                capture_component = (
                    frame_sequence
                    if frame_sequence is not None
                    else self._runtime_capture_count
                )
                result.metadata.setdefault(
                    "capture_id",
                    f"{result.backend}:{result.metadata.get('capture_session_id') or hwnd}:{capture_component}",
                )
                result.metadata.setdefault("captured_monotonic", time.monotonic())
                self._runtime_last_backend = result.backend
                self._runtime_last_session_id = result.metadata.get("capture_session_id")
                if result.metadata.get("session_recreated"):
                    self._runtime_session_recreate_count += 1
                self.logger.info(
                    "TARGET_CAPTURE_BACKEND %s duration_ms=%s metadata=%s",
                    name,
                    result.capture_duration_ms,
                    result.metadata,
                )
                self.logger.info(
                    "TARGET_CAPTURE_RESULT width=%s height=%s",
                    result.image.width,
                    result.image.height,
                )
                self.logger.info(
                    "TARGET_CAPTURE_VALID true backend=%s", name
                )
                return result
            except Exception as exc:
                if self._backend_cache.get(key) == name:
                    self._backend_cache.pop(key, None)
                    self.logger.warning(
                        "TARGET_CAPTURE_CACHE_INVALIDATED hwnd=0x%08X "
                        "pid=%s backend=%s",
                        hwnd,
                        target.pid,
                        name,
                    )
                diagnostic = self._log_backend_exception(name, exc)
                failures.append(f"{name}: {diagnostic}")

        self.logger.error("TARGET_CAPTURE_VALID false")
        self.logger.error("TARGET_CAPTURE_ABORT_SELECTOR true")
        detail = "\n".join(failures)
        raise RuntimeError(
            "此目標視窗目前無法直接擷取。\n\n"
            f"目標：{target.title}\n"
            f"HWND：0x{hwnd:08X}\n"
            f"Client Size：{target.client_width}x{target.client_height}\n"
            f"Backends：\n{detail}\n\n"
            f"詳細資訊已寫入：{self.diagnostic_log_path}\n"
            "為避免截入其他視窗，不會使用桌面截圖替代。"
        )

    @staticmethod
    def _normalize_backend_name(name):
        aliases = {
            "print_window_ctypes": "printwindow",
            "print_window": "printwindow",
            "windows_graphics_capture": "winsdk-wgc",
            "wgc": "winsdk-wgc",
            "client_dc_bitblt": "bitblt",
        }
        normalized = aliases.get(name.lower(), name.lower())
        if normalized not in ("winsdk-wgc", "printwindow", "bitblt", "windows-cap"):
            raise ValueError(
                "force_backend must be winsdk-wgc, printwindow, bitblt, "
                "or diagnostic windows-cap"
            )
        return normalized

    def _validate_result(self, result: CaptureResult, width, height):
        image = result.image
        if image is None:
            return False, "no image returned"
        if result.hwnd <= 0:
            return False, "backend did not preserve target HWND"
        if image.width != width or image.height != height:
            return (
                False,
                f"image size {image.width}x{image.height} does not match "
                f"client {width}x{height}",
            )
        gray = image.convert("L")
        minimum, maximum = gray.getextrema()
        stats = ImageStat.Stat(gray)
        mean, deviation = stats.mean[0], stats.stddev[0]
        spread = maximum - minimum
        self.logger.info(
            "TARGET_CAPTURE_IMAGE_STATS backend=%s min=%s max=%s "
            "mean=%.2f stddev=%.2f spread=%s",
            result.backend,
            minimum,
            maximum,
            mean,
            deviation,
            spread,
        )
        if spread <= 8 or deviation < 1.5:
            return False, "near-uniform image (white/black/solid-color capture)"
        if maximum <= 8:
            return False, "near-black image"
        if minimum >= 247:
            return False, "near-white image"
        return True, ""

    def _log_environment_once(self):
        if self._environment_logged:
            return
        self._environment_logged = True
        self.logger.info(
            "TARGET_CAPTURE_ENV windows=%s release=%s version=%s build=%s",
            platform.system(),
            platform.release(),
            platform.version(),
            platform.win32_ver()[1],
        )
        self.logger.info(
            "TARGET_CAPTURE_ENV python=%s", sys.version.replace("\n", " ")
        )
        for package, module_name, fallback_version in (
            ("pywinrt", "winrt.runtime", "3.2.1"),
            ("windows-cap", "windows_cap", "0.1.2"),
        ):
            try:
                module = __import__(module_name)
                version = getattr(module, "__version__", fallback_version)
                self.logger.info(
                    "TARGET_CAPTURE_ENV package=%s version=%s available=true",
                    package,
                    version,
                )
            except Exception as exc:
                self.logger.info(
                    "TARGET_CAPTURE_ENV package=%s available=false error=%s",
                    package,
                    exc,
                )

    def _log_backend_exception(self, backend, exc):
        original = getattr(exc, "original", None) or exc
        hresult = getattr(original, "hresult", None)
        winerror = getattr(original, "winerror", None)
        errno = getattr(original, "errno", None)
        match = re.search(r"0x([0-9A-Fa-f]{8})", str(original))
        parsed_code = int(match.group(1), 16) if match else None
        code = (
            winerror
            if winerror is not None
            else hresult
            if hresult is not None
            else parsed_code
            if parsed_code is not None
            else errno
        )
        code_hex = (
            f"0x{int(code) & 0xFFFFFFFF:08X}"
            if isinstance(code, int)
            else "unavailable"
        )
        stack = traceback.format_exc()
        self.logger.error(
            "TARGET_CAPTURE_BACKEND_FAILED backend=%s stage=%s "
            "exception_type=%s exception_message=%s exception_repr=%r "
            "exception_args=%r hresult=%s win32_error=%s errno=%s "
            "code_hex=%s",
            backend,
            getattr(exc, "stage", None),
            type(original).__name__,
            str(original),
            original,
            getattr(original, "args", ()),
            hresult,
            winerror,
            errno,
            code_hex,
        )
        self.logger.error(
            "TARGET_CAPTURE_STACK backend=%s\n%s", backend, stack
        )
        return (
            f"{type(original).__name__}: {original} "
            f"(stage={getattr(exc, 'stage', None)}, HRESULT={hresult}, "
            f"Win32={winerror}, code={code_hex})"
        )

    @staticmethod
    def _window_cloaked(hwnd):
        value = wintypes.DWORD()
        result = ctypes.WinDLL("dwmapi").DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            14,  # DWMWA_CLOAKED
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        return bool(value.value), result

    def _log_target(self, target, hwnd, is_screenbot):
        import psutil
        import win32api
        import win32con
        import win32gui

        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER)
        parent = win32gui.GetParent(hwnd)
        root = user32.GetAncestor(wintypes.HWND(hwnd), 2)
        try:
            cloaked, cloaked_result = self._window_cloaked(hwnd)
        except Exception as exc:
            cloaked, cloaked_result = False, f"unavailable: {exc}"
        get_dpi = getattr(user32, "GetDpiForWindow", None)
        dpi = int(get_dpi(wintypes.HWND(hwnd))) if get_dpi else 0
        self.logger.info("TARGET_HWND 0x%08X", hwnd)
        self.logger.info("TARGET_PID %s", target.pid)
        try:
            target_user = psutil.Process(target.pid).username()
        except Exception:
            target_user = "unknown"
        self.logger.info(
            "TARGET_SECURITY_CONTEXT capture_user=%s target_user=%s",
            win32api.GetUserName(),
            target_user,
        )
        self.logger.info(
            "TARGET_PROCESS_NAME %s",
            getattr(target, "process_name", "unknown"),
        )
        self.logger.info("TARGET_WINDOW_CLASS %s", win32gui.GetClassName(hwnd))
        self.logger.info(
            "TARGET_CLIENT_SIZE width=%s height=%s",
            target.client_width,
            target.client_height,
        )
        self.logger.info(
            "TARGET_HWND_STATE valid=%s foreground=%s visible=%s "
            "minimized=%s cloaked=%s cloaked_result=%s top_level=%s "
            "owner=0x%X parent=0x%X child=%s layered=%s dpi=%s "
            "screenbot=%s",
            bool(user32.IsWindow(wintypes.HWND(hwnd))),
            user32.GetForegroundWindow() == hwnd,
            bool(user32.IsWindowVisible(wintypes.HWND(hwnd))),
            bool(user32.IsIconic(wintypes.HWND(hwnd))),
            cloaked,
            cloaked_result,
            int(root) == hwnd,
            int(owner or 0),
            int(parent or 0),
            bool(style & win32con.WS_CHILD),
            bool(exstyle & win32con.WS_EX_LAYERED),
            dpi,
            is_screenbot,
        )

    def invalidate_target(self, hwnd=None):
        if hwnd is None:
            self._backend_cache.clear()
        else:
            hwnd = int(hwnd)
            for key in tuple(self._backend_cache):
                if key[0] == hwnd:
                    self._backend_cache.pop(key, None)
        for backend in self._backend_instances.values():
            invalidate = getattr(backend, "invalidate_target", None)
            if invalidate is not None:
                invalidate(hwnd)
        self.logger.info(
            "TARGET_CAPTURE_CACHE_CLEARED hwnd=%s",
            f"0x{int(hwnd):08X}" if hwnd is not None else "all",
        )

    def record_runtime_ui_mutation(self, kind, *, source="unknown"):
        """Record an unexpected Runtime UI mutation for diagnostics only."""
        if kind not in self._runtime_ui_mutations:
            raise ValueError(f"Unknown runtime UI mutation: {kind}")
        self._runtime_ui_mutations[kind] += 1
        self.logger.warning("RUNTIME_UI_MUTATION kind=%s source=%s", kind, source)

    def get_runtime_diagnostics(self):
        return {
            "capture_backend": self._runtime_last_backend,
            "capture_session_id": self._runtime_last_session_id,
            "capture_session_recreate_count": self._runtime_session_recreate_count,
            "capture_count": self._runtime_capture_count,
            "screenbot_visibility_changed": self._runtime_ui_mutations["window_visibility_change_count"] > 0,
            "target_visibility_changed": self._runtime_ui_mutations["window_visibility_change_count"] > 0,
            "foreground_window_changed": self._runtime_ui_mutations["foreground_change_count"] > 0,
            "overlay_active": False,
            **self._runtime_ui_mutations,
        }

    def close(self):
        for backend in self._backend_instances.values():
            try:
                backend.close()
            except Exception:
                self.logger.exception(
                    "TARGET_CAPTURE_BACKEND_CLOSE_FAILED backend=%s",
                    getattr(backend, "name", type(backend).__name__),
                )
        self._backend_instances.clear()
        self._backend_cache.clear()
        if self._diagnostic_handler is not None:
            self.logger.removeHandler(self._diagnostic_handler)
            self._diagnostic_handler.close()
            self._diagnostic_handler = None
        self.logger.info("TARGET_CAPTURE_SERVICE_CLOSED")
