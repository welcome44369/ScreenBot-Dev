"""Independent HWND capture diagnostic utility.

This tool does not import the ScreenBot UI, OCR, Trigger, or Workflow layers.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import traceback

from PIL import ImageStat
import psutil
import win32api
import win32con
import win32gui
import win32process


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.capture_backends import (
    BACKEND_TYPES,
    CaptureBackendError,
    attach_input_desktop,
    close_desktop,
    create_backend,
)


def _window_row(hwnd):
    import ctypes
    from ctypes import wintypes

    title = win32gui.GetWindowText(hwnd)
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    try:
        process = psutil.Process(pid)
        process_name = process.name()
        process_user = process.username()
    except Exception:
        process_name = "unknown"
        process_user = "unknown"
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER)
    parent = win32gui.GetParent(hwnd)
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    cloaked = wintypes.DWORD()
    try:
        ctypes.WinDLL("dwmapi").DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            14,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
    except Exception:
        pass
    return {
        "hwnd": f"0x{hwnd:08X}",
        "title": title,
        "class": win32gui.GetClassName(hwnd),
        "pid": pid,
        "process": process_name,
        "process_user": process_user,
        "client_size": [right - left, bottom - top],
        "visible": bool(win32gui.IsWindowVisible(hwnd)),
        "minimized": bool(win32gui.IsIconic(hwnd)),
        "cloaked": bool(cloaked.value),
        "owner": f"0x{int(owner or 0):08X}",
        "parent": f"0x{int(parent or 0):08X}",
        "top_level": not bool(style & win32con.WS_CHILD),
        "child": bool(style & win32con.WS_CHILD),
        "layered": bool(exstyle & win32con.WS_EX_LAYERED),
    }


def list_windows():
    rows = []

    def visit(hwnd, _):
        try:
            title = win32gui.GetWindowText(hwnd)
            if not win32gui.IsWindowVisible(hwnd) or not title:
                return True
            rows.append(_window_row(hwnd))
        except Exception:
            # A window can be destroyed between EnumWindows and inspection.
            return True
        return True

    try:
        win32gui.EnumWindows(visit, None)
    except Exception as exc:
        # A non-interactive service desktop may report ERROR_INVALID_HANDLE
        # instead of an empty list.  Preserve any rows already enumerated.
        if not rows and getattr(exc, "winerror", None) not in (None, 6):
            raise
    return rows


def list_child_windows(parent):
    rows = []

    def visit(hwnd, _):
        try:
            rows.append(_window_row(hwnd))
        except Exception:
            return True
        return True

    win32gui.EnumChildWindows(parent, visit, None)
    return rows


def resolve_title(value: str) -> int:
    needle = value.casefold()
    matches = [
        row
        for row in list_windows()
        if needle in row["title"].casefold()
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one visible title match for {value!r}; got "
            f"{json.dumps(matches, ensure_ascii=False)}"
        )
    return int(matches[0]["hwnd"], 0)


def image_validation(image):
    gray = image.convert("L")
    minimum, maximum = gray.getextrema()
    stats = ImageStat.Stat(gray)
    mean, deviation = stats.mean[0], stats.stddev[0]
    spread = maximum - minimum
    valid = (
        image.width > 1
        and image.height > 1
        and spread > 8
        and deviation >= 1.5
        and maximum > 8
        and minimum < 247
    )
    return {
        "valid": valid,
        "minimum": minimum,
        "maximum": maximum,
        "mean": mean,
        "stddev": deviation,
        "spread": spread,
    }


def error_payload(exc):
    original = getattr(exc, "original", None) or exc
    values = {
        "exception": type(exc).__name__,
        "message": str(exc),
        "backend": getattr(exc, "backend", None),
        "stage": getattr(exc, "stage", None),
        "hresult": getattr(original, "hresult", None),
        "winerror": getattr(original, "winerror", None),
        "errno": getattr(original, "errno", None),
        "args": repr(getattr(original, "args", ())),
        "stack": traceback.format_exc(),
    }
    match = re.search(r"0x([0-9A-Fa-f]{8})", str(original))
    parsed_code = int(match.group(1), 16) if match else None
    code = next(
        (
            value
            for value in (
                values["winerror"],
                values["hresult"],
                parsed_code,
                values["errno"],
            )
            if value is not None
        ),
        None,
    )
    values["hex_error"] = (
        f"0x{int(code) & 0xFFFFFFFF:08X}" if isinstance(code, int) else None
    )
    return values


def create_self_test_window():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QLabel, QMainWindow

    app = QApplication.instance() or QApplication([])
    window = QMainWindow()
    window.setWindowTitle("ScreenBot WGC Isolated Test")
    window.resize(640, 360)
    label = QLabel("ScreenBot WGC Isolated Test", window)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setStyleSheet("background: white; color: black; font-size: 28px;")
    window.setCentralWidget(label)
    window.show()
    app.processEvents()
    return (app, window), int(window.winId())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--children-of", type=lambda value: int(value, 0))
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--hwnd", type=lambda value: int(value, 0))
    selector.add_argument("--title")
    selector.add_argument("--self-test-window", action="store_true")
    parser.add_argument(
        "--backend",
        choices=sorted(BACKEND_TYPES),
        default="windows-cap",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    input_desktop = attach_input_desktop()

    owner = None
    window = None
    try:
        if args.list:
            print(json.dumps(list_windows(), ensure_ascii=False, indent=2))
            return 0
        if args.children_of:
            print(
                json.dumps(
                    list_child_windows(args.children_of),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if not (args.hwnd or args.title or args.self_test_window):
            parser.error("--hwnd, --title, or --self-test-window is required")
        if args.self_test_window:
            owner, hwnd = create_self_test_window()
        else:
            hwnd = args.hwnd or resolve_title(args.title)
        window = next(
            (
                row
                for row in list_windows()
                if int(row["hwnd"], 0) == hwnd
            ),
            None,
        )
        if window is None:
            raise RuntimeError(f"HWND 0x{hwnd:08X} is not a visible top-level window")

        backend = create_backend(args.backend)
        result = backend.capture_client(hwnd)
        validation = image_validation(result.image)
        output = args.output or (
            PROJECT_ROOT
            / "logs"
            / "capture-tests"
            / f"{args.backend}-{hwnd:08X}.png"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        result.image.save(output)
        payload = {
            "success": validation["valid"],
            "execution_user": win32api.GetUserName(),
            "window": window,
            "backend": result.backend,
            "duration_ms": result.capture_duration_ms,
            "capture_size": list(result.image.size),
            "validation": validation,
            "metadata": result.metadata,
            "output": str(output.resolve()),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        backend.close()
        return 0 if validation["valid"] else 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "success": False,
                    "execution_user": win32api.GetUserName(),
                    "window": window,
                    "error": error_payload(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    finally:
        if owner is not None:
            _, window = owner
            window.close()
        close_desktop(input_desktop)


if __name__ == "__main__":
    raise SystemExit(main())
