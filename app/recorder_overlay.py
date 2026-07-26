import ctypes
import time

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

user32 = ctypes.windll.user32


def _virtual_geometry():
    screens = QApplication.screens()
    if not screens:
        return QRect(0, 0, 1, 1)
    geometry = screens[0].geometry()
    for screen in screens[1:]:
        geometry = geometry.united(screen.geometry())
    return geometry


class CoordinateTooltip(QWidget):
    def __init__(self):
        super().__init__(
            None,
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)

        self.x_label = QLabel("X 0.0000")
        self.y_label = QLabel("Y 0.0000")
        for label in (self.x_label, self.y_label):
            label.setStyleSheet("color: #f5fbff; background: transparent; font-size: 11px;")
            layout.addWidget(label)

        self.setStyleSheet(
            "QWidget {"
            "background: rgba(22, 30, 40, 0.82);"
            "border: 1px solid rgba(255, 255, 255, 0.22);"
            "border-radius: 10px;"
            "}"
        )
        self.hide()

    def show_ratio(self, cursor_pos, x_ratio, y_ratio):
        self.x_label.setText(f"X {x_ratio:.4f}")
        self.y_label.setText(f"Y {y_ratio:.4f}")
        self.adjustSize()
        self.move(cursor_pos + QPoint(18, 18))
        if not self.isVisible():
            self.show()
        self.raise_()

    def hide_tooltip(self):
        self.hide()


class RippleOverlay(QWidget):
    START_RADIUS = 12.0
    END_RADIUS = 56.0
    DURATION_S = 0.38

    def __init__(self):
        super().__init__(
            None,
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self._ripples = []
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._refresh_geometry()
        self.hide()

    def _refresh_geometry(self):
        geometry = _virtual_geometry()
        if self.geometry() != geometry:
            self.setGeometry(geometry)

    def show_ripple(self, screen_pos, color, is_double):
        self._refresh_geometry()
        self._ripples.append(
            {
                "pos": QPoint(screen_pos),
                "color": QColor(color),
                "double": bool(is_double),
                "started": time.monotonic(),
            }
        )
        if not self._timer.isActive():
            self._timer.start()
        if not self.isVisible():
            self.show()
        self.update()

    def clear_ripples(self):
        self._ripples.clear()
        self._timer.stop()
        self.hide()
        self.update()

    def _tick(self):
        now = time.monotonic()
        self._ripples = [ripple for ripple in self._ripples if now - ripple["started"] < self.DURATION_S]
        if not self._ripples:
            self.clear_ripples()
            return
        self.update()

    def paintEvent(self, event):
        if not self._ripples:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        origin = self.geometry().topLeft()
        now = time.monotonic()
        for ripple in self._ripples:
            progress = min(1.0, max(0.0, (now - ripple["started"]) / self.DURATION_S))
            radius = self.START_RADIUS + (self.END_RADIUS - self.START_RADIUS) * progress
            alpha = int(round(220 * (1.0 - progress)))
            color = QColor(ripple["color"])
            color.setAlpha(max(0, alpha))
            pen = QPen(color)
            pen.setWidthF(2.0)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            center = QPointF(ripple["pos"] - origin)
            painter.drawEllipse(center, radius, radius)
            if ripple["double"]:
                inner_radius = max(8.0, radius - 12.0)
                painter.drawEllipse(center, inner_radius, inner_radius)


class RecorderOverlayController(QObject):
    def __init__(self, window_tracker, logger, parent=None):
        super().__init__(parent)
        self.window_tracker = window_tracker
        self.logger = logger
        self._active = False
        self._tooltip = None
        self._ripple_overlay = None
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(33)
        self._refresh_timer.timeout.connect(self._update_tooltip)

    def start(self):
        if self._active:
            return
        self._ensure_widgets()
        self._active = True
        self._tooltip.hide_tooltip()
        self._ripple_overlay.clear_ripples()
        self._refresh_timer.start()
        self.logger.info("Recorder overlay started")

    def stop(self):
        if not self._active:
            return
        self._active = False
        self._refresh_timer.stop()
        if self._tooltip is not None:
            self._tooltip.hide_tooltip()
        if self._ripple_overlay is not None:
            self._ripple_overlay.clear_ripples()
        self.logger.info("Recorder overlay stopped")

    def destroy(self):
        self.stop()
        if self._tooltip is not None:
            self._tooltip.close()
            self._tooltip.deleteLater()
            self._tooltip = None
        if self._ripple_overlay is not None:
            self._ripple_overlay.close()
            self._ripple_overlay.deleteLater()
            self._ripple_overlay = None

    def show_click_ripple(self, payload):
        if not self._active:
            return
        if payload.get("type") not in {"click", "double_click"}:
            return
        self._ensure_widgets()
        screen_x = payload.get("screen_x")
        screen_y = payload.get("screen_y")
        if screen_x is None or screen_y is None:
            target = self.window_tracker.target
            if target is None:
                return
            screen_x, screen_y = target.from_client_ratio(payload["x_ratio"], payload["y_ratio"])
        if payload.get("button") == "right":
            color = QColor(255, 190, 122, 220)
        else:
            color = QColor(160, 219, 255, 220)
        self._ripple_overlay.show_ripple(QPoint(int(screen_x), int(screen_y)), color, payload.get("type") == "double_click")
        self.logger.debug("Click ripple shown")

    def _ensure_widgets(self):
        if self._tooltip is None:
            self._tooltip = CoordinateTooltip()
        if self._ripple_overlay is None:
            self._ripple_overlay = RippleOverlay()

    def _is_target_foreground(self):
        target = self.window_tracker.target
        if target is None:
            return False
        return user32.GetForegroundWindow() == int(target.hwnd)

    def _update_tooltip(self):
        if not self._active:
            return
        self._ensure_widgets()
        target = self.window_tracker.target
        if target is None or not target.is_valid() or not self._is_target_foreground():
            self._tooltip.hide_tooltip()
            return
        cursor_pos = QCursor.pos()
        if not target.contains_point(cursor_pos.x(), cursor_pos.y()):
            self._tooltip.hide_tooltip()
            return
        x_ratio, y_ratio = target.to_client_ratio(cursor_pos.x(), cursor_pos.y())
        self._tooltip.show_ratio(cursor_pos, x_ratio, y_ratio)
