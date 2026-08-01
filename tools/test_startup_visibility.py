"""Focused startup presentation contracts for the compact ScreenBot UI."""
import unittest

from PySide6.QtCore import QPoint, QRect, QSize

from app.application import ScreenBotApp


class StartupVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.size = QSize(420, 90)
        self.primary = QRect(0, 0, 1920, 1080)

    def test_valid_saved_geometry_is_preserved(self):
        self.assertEqual(
            ScreenBotApp._safe_startup_position([120, 120], self.size, [self.primary]),
            QPoint(120, 120),
        )

    def test_offscreen_geometry_is_clamped(self):
        position = ScreenBotApp._safe_startup_position([-5000, 120], self.size, [self.primary])
        self.assertGreaterEqual(position.x(), 24)
        self.assertGreaterEqual(position.y(), 24)

    def test_bottom_edge_sliver_is_clamped(self):
        position = ScreenBotApp._safe_startup_position([100, 1070], self.size, [self.primary])
        visible = QRect(position, self.size).intersected(self.primary)
        self.assertGreaterEqual(visible.height(), 64)

    def test_no_work_area_preserves_position_without_target_side_effects(self):
        self.assertEqual(
            ScreenBotApp._safe_startup_position([77, 88], self.size, []), QPoint(77, 88)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
