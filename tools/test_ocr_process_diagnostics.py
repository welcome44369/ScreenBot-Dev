import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from PySide6.QtWidgets import QApplication

from app.floating_widget import FloatingWidget
from app.ocr_pipeline import OCRPipeline
from app.ocr_process_diagnostics import OcrProcessDiagnostics


class FakeWindowSnapshot:
    def __init__(self, hwnd):
        self.hwnd = int(hwnd or 0)

    def to_dict(self):
        return {
            "hwnd": self.hwnd,
            "pid": 0,
            "process_name": None,
            "title": None,
            "class_name": None,
            "root_hwnd": self.hwnd,
            "is_visible": bool(self.hwnd),
            "is_topmost": False,
            "is_foreground": self.hwnd == 900,
        }


class FakeWindowAdapter:
    def __init__(self):
        self.callback = None
        self.uninstalled = False

    def foreground_hwnd(self):
        return 900

    def snapshot_window(self, hwnd):
        return FakeWindowSnapshot(hwnd)

    def enumerate_windows(self):
        return [900, 700, 500]

    def install_win_event_hook(self, callback):
        self.callback = callback
        return (101,)

    def uninstall_win_event_hook(self, handles):
        self.uninstalled = tuple(handles or ()) == (101,)


class FakeNoActivateAdapter:
    def apply(self, _hwnd):
        return True

    def has_no_activate(self, _hwnd):
        return True

    def mouse_activate_result(self, _message):
        return None


class FakePipelineObserver:
    def __init__(self):
        self.active = True
        self.started = []
        self.finished = []

    def variant_started(self, variant_id, **metadata):
        token = f"token-{len(self.started) + 1}"
        self.started.append((token, variant_id, metadata))
        return token

    def variant_finished(self, token, error=None):
        self.finished.append((token, error))


class OcrProcessDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_disabled_diagnostic_creates_no_output(self):
        with tempfile.TemporaryDirectory() as root:
            diagnostics = OcrProcessDiagnostics(
                root,
                enabled=False,
                adapter=FakeWindowAdapter(),
            )
            self.assertFalse(diagnostics.enabled)
            self.assertFalse((Path(root) / "logs").exists())
            with self.assertRaises(RuntimeError):
                diagnostics.run_probe(
                    lambda: {},
                    target_snapshot=SimpleNamespace(root_hwnd=700),
                )
            self.assertFalse((Path(root) / "logs").exists())

    def test_probe_attributes_child_without_changing_launch_contract(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = FakeWindowAdapter()
            diagnostics = OcrProcessDiagnostics(
                root,
                enabled=True,
                adapter=adapter,
            )
            target = SimpleNamespace(
                root_hwnd=700,
                session_id="session-a",
                generation=4,
            )

            def operation():
                token = diagnostics.variant_started(
                    "test/get_languages",
                    call="subprocess.run",
                )
                completed = subprocess.run(
                    [sys.executable, "-c", "print('probe-child')"],
                    capture_output=True,
                    check=True,
                    shell=False,
                )
                diagnostics.variant_finished(token)
                return {"stdout": completed.stdout.decode().strip()}

            result = diagnostics.run_probe(
                operation,
                target_snapshot=target,
                compact_hwnd=500,
            )
            records = [
                json.loads(line)
                for line in Path(result["jsonl"]).read_text(encoding="utf-8").splitlines()
            ]
            launches = [
                record
                for record in records
                if record["event"] == "PROCESS_STARTED"
            ]
            self.assertEqual(len(launches), 1)
            launch = launches[0]
            self.assertEqual(launch["parent_pid"], os.getpid())
            self.assertFalse(launch["shell"])
            self.assertEqual(launch["creationflags"], 0)
            self.assertEqual(launch["variant_id"], "test/get_languages")
            self.assertIn("probe-child", result["result"]["stdout"])
            self.assertTrue(adapter.uninstalled)
            with self.assertRaisesRegex(RuntimeError, "already been consumed"):
                diagnostics.run_probe(
                    operation,
                    target_snapshot=target,
                    compact_hwnd=500,
                )

    def test_pipeline_marks_language_query_and_each_existing_variant(self):
        observer = FakePipelineObserver()
        pipeline = OCRPipeline(process_diagnostics=observer)
        empty_data = {
            "text": [],
            "conf": [],
            "left": [],
            "top": [],
            "width": [],
            "height": [],
            "block_num": [],
            "par_num": [],
            "line_num": [],
        }
        with patch("app.ocr_pipeline.pytesseract.get_languages", return_value=["eng"]):
            with patch(
                "app.ocr_pipeline.pytesseract.image_to_data",
                return_value=empty_data,
            ):
                pipeline.recognize_text(
                    Image.new("RGB", (40, 20), "black"),
                    languages="eng",
                )
        variant_ids = [item[1] for item in observer.started]
        self.assertEqual(variant_ids[0], "get_languages")
        self.assertEqual(
            variant_ids[1:],
            ["english/original/eng/psm6", "english/upscaled/eng/psm11"],
        )
        self.assertEqual(len(observer.finished), len(observer.started))
        self.assertTrue(all(error is None for _, error in observer.finished))

    def test_probe_control_exists_only_behind_environment_gate(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(
                os.environ,
                {"SCREENBOT_OCR_PROCESS_DIAGNOSTIC": "1"},
                clear=False,
            ):
                widget = FloatingWidget(
                    root,
                    native_no_activate_adapter=FakeNoActivateAdapter(),
                )
                try:
                    self.assertIsNotNone(widget.ocr_process_probe_button)
                finally:
                    widget.close()
            with patch.dict(
                os.environ,
                {"SCREENBOT_OCR_PROCESS_DIAGNOSTIC": "0"},
                clear=False,
            ):
                widget = FloatingWidget(
                    root,
                    native_no_activate_adapter=FakeNoActivateAdapter(),
                )
                try:
                    self.assertIsNone(widget.ocr_process_probe_button)
                finally:
                    widget.close()

    def test_diagnostic_source_does_not_apply_a_window_creation_repair(self):
        source = (
            Path(__file__).parents[1] / "app" / "ocr_process_diagnostics.py"
        ).read_text(encoding="utf-8-sig")
        forbidden = "CREATE_" + "NO_WINDOW"
        self.assertNotIn(forbidden, source)
        self.assertNotIn("SetForegroundWindow", source)
        self.assertNotIn("BringWindowToTop", source)


if __name__ == "__main__":
    unittest.main()
