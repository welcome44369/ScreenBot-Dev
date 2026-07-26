"""Regression test for TargetCaptureService backend selection and cleanup."""
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.capture_backends import CaptureBackendError, CaptureResult
from app.target_capture import TargetCaptureService


class _Target:
    hwnd = 123
    pid = 424242
    title = "Capture Cache Test"
    client_width = 32
    client_height = 32


class _Tracker:
    target = _Target()

    def refresh(self):
        return self.target


class _Backend:
    def __init__(self, name, *, fails=False):
        self.name = name
        self.fails = fails
        self.capture_calls = 0
        self.close_calls = 0

    def capture_client(self, hwnd):
        self.capture_calls += 1
        if self.fails:
            raise CaptureBackendError(self.name, "test", "expected failure")
        image = Image.new("RGB", (32, 32), "white")
        for coordinate in range(32):
            image.putpixel((coordinate, coordinate), (0, 0, 0))
        return CaptureResult(
            image=image,
            backend=self.name,
            hwnd=hwnd,
            client_size=image.size,
            capture_duration_ms=1,
        )

    def close(self):
        self.close_calls += 1


class CaptureServiceCacheTest(unittest.TestCase):
    def test_cache_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            service = TargetCaptureService(
                _Tracker(),
                diagnostic_log_path=Path(directory) / "capture.log",
            )
            service.DEFAULT_BACKEND_CHAIN = ("first", "second")
            first = _Backend("first", fails=True)
            second = _Backend("second")
            service._backend_instances = {"first": first, "second": second}
            service._log_environment_once = lambda: None
            service._log_target = lambda *args: None

            service.capture_target_client(123)
            service.capture_target_client(123)

            self.assertEqual(first.capture_calls, 1)
            self.assertEqual(second.capture_calls, 2)
            self.assertEqual(service._backend_cache[(123, 424242)], "second")

            service.invalidate_target(123)
            self.assertFalse(service._backend_cache)
            service.close()
            self.assertEqual(first.close_calls, 1)
            self.assertEqual(second.close_calls, 1)
            self.assertFalse(service._backend_cache)

    def test_desktop_fallback_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            service = TargetCaptureService(
                _Tracker(),
                diagnostic_log_path=Path(directory) / "capture.log",
            )
            with self.assertRaisesRegex(ValueError, "Desktop fallback is disabled"):
                service.capture_target_client(
                    123, allow_desktop_fallback=True
                )
            service.close()


if __name__ == "__main__":
    unittest.main()
