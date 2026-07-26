"""Unit regression for WGC session reuse without accessing a real HWND."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.capture_backends as capture_backends


class _FakeSession:
    created = 0

    def __init__(self, _backend, hwnd, client_size):
        type(self).created += 1
        self.hwnd = hwnd
        self.client_size = client_size
        self.session_id = f"session-{type(self).created}"
        self.metadata = {"capture_session_id": self.session_id}
        self.closed = False

    def start(self, _timeout):
        return None

    def wait_for_image(self, _timeout):
        return Image.new("RGB", self.client_size, (20, 80, 140))

    def close(self):
        self.closed = True


class WgcSessionReuseTest(unittest.TestCase):
    def test_same_hwnd_reuses_session_until_invalidated(self):
        _FakeSession.created = 0
        backend = capture_backends.WinsdkWgcBackend(timeout_seconds=0.01)
        with (
            patch.object(capture_backends.user32, "IsWindow", return_value=True),
            patch.object(capture_backends, "get_client_size", return_value=(4, 3)),
            patch.object(
                capture_backends.WinsdkWgcBackend,
                "_crop_to_client",
                staticmethod(lambda _hwnd, image: (image, {"client_crop": "not_required"})),
            ),
            patch.object(capture_backends, "_WinsdkWgcSession", _FakeSession),
        ):
            first = backend.capture_client(123)
            second = backend.capture_client(123)
            self.assertEqual(_FakeSession.created, 1)
            self.assertTrue(first.metadata["session_recreated"])
            self.assertFalse(second.metadata["session_recreated"])
            self.assertEqual(first.metadata["capture_session_id"], second.metadata["capture_session_id"])
            backend.invalidate_target(123)


if __name__ == "__main__":
    unittest.main()
