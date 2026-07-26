from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
import ctypes
import os
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget, QMessageBox


def pil_image_to_qpixmap(image):
    image_rgba = image.convert("RGBA")
    raw = image_rgba.tobytes("raw", "RGBA")
    from PySide6.QtGui import QImage

    qimg = QImage(raw, image_rgba.width, image_rgba.height, QImage.Format_RGBA8888).copy()
    return QPixmap.fromImage(qimg)


class _SelectionCanvas(QWidget):
    def __init__(self, pixmap):
        super().__init__()
        self._pixmap = pixmap
        self._start = None
        self._end = None
        self._selection = QRect()
        self._dragging = False
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFixedSize(pixmap.size())

    @property
    def selection(self):
        return QRect(self._selection)

    def clear_selection(self):
        self._selection = QRect()
        self._start = None
        self._end = None
        self._dragging = False
        self.update()

    def _clamp_point(self, point):
        x = max(0, min(self.width() - 1, point.x()))
        y = max(0, min(self.height() - 1, point.y()))
        return QPoint(x, y)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            p = self._clamp_point(event.position().toPoint())
            self._start = p
            self._end = p
            self._selection = QRect(p, p)
            self._dragging = True
            self.update()

    def mouseMoveEvent(self, event):
        if self._dragging and self._start is not None:
            self._end = self._clamp_point(event.position().toPoint())
            self._selection = QRect(self._start, self._end).normalized()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self._end = self._clamp_point(event.position().toPoint())
            self._selection = QRect(self._start, self._end).normalized()
            self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._pixmap)

        # Dim entire area
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))

        # Undim selected area
        if not self._selection.isNull() and self._selection.width() > 0 and self._selection.height() > 0:
            source = self._selection
            painter.drawPixmap(source, self._pixmap, source)
            painter.setPen(QPen(QColor(60, 170, 255), 2))
            painter.drawRect(self._selection)
            size_text = f"{self._selection.width()} x {self._selection.height()} px"
            text_rect = QRect(self._selection.left(), max(0, self._selection.top() - 24), 200, 20)
            painter.fillRect(text_rect, QColor(0, 0, 0, 160))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(text_rect.adjusted(6, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft, size_text)

        # Help text
        hint = "Left-drag: select  |  Enter: confirm  |  Esc: cancel"
        hint_rect = QRect(8, 8, max(250, self.width() - 16), 24)
        painter.fillRect(hint_rect, QColor(0, 0, 0, 170))
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(hint_rect.adjusted(8, 0, -8, 0), Qt.AlignVCenter | Qt.AlignLeft, hint)


class OCRRegionSelectorDialog(QDialog):
    """Static-screenshot region selector over the target client area."""

    def __init__(self, screenshot_pixmap, min_width_px=20, min_height_px=12, parent=None):
        super().__init__(parent)
        self.setWindowTitle("OCR Region Selector")
        self.setModal(False)
        self.setWindowModality(Qt.NonModal)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self._min_width_px = min_width_px
        self._min_height_px = min_height_px
        self._region = None
        self._too_small = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = _SelectionCanvas(screenshot_pixmap)
        layout.addWidget(self.canvas)
        self.setFixedSize(screenshot_pixmap.size())
        self.setFocusPolicy(Qt.StrongFocus)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.reject()
            event.accept()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._confirm_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def _confirm_selection(self):
        selection = self.canvas.selection
        if selection.isNull() or selection.width() <= 0 or selection.height() <= 0:
            self.reject()
            return
        if selection.width() < self._min_width_px or selection.height() < self._min_height_px:
            # Keep dialog open; caller can choose again.
            self._too_small = True
            QMessageBox.warning(self, "Region too small", "The selected OCR region is too small.")
            return
        self._too_small = False

        width = float(self.canvas.width())
        height = float(self.canvas.height())

        x_ratio = max(0.0, min(1.0, selection.left() / width))
        y_ratio = max(0.0, min(1.0, selection.top() / height))
        w_ratio = max(0.0, min(1.0, selection.width() / width))
        h_ratio = max(0.0, min(1.0, selection.height() / height))

        # Clamp to bounds after floating-point division.
        if x_ratio + w_ratio > 1.0:
            w_ratio = max(0.0, 1.0 - x_ratio)
        if y_ratio + h_ratio > 1.0:
            h_ratio = max(0.0, 1.0 - y_ratio)

        self._region = {
            "x_ratio": round(x_ratio, 6),
            "y_ratio": round(y_ratio, 6),
            "width_ratio": round(w_ratio, 6),
            "height_ratio": round(h_ratio, 6),
        }
        self.region_selected.emit({"pixel_rect": self.selected_pixel_rect(), "ratio_rect": dict(self._region)})
        self.accept()

    def showEvent(self, event):
        super().showEvent(event)
        self.setFocus(Qt.ActiveWindowFocusReason)
        self.activateWindow(); self.raise_()
        try: self.grabKeyboard()
        except Exception: pass
        QTimer.singleShot(0, self.raise_)
        QTimer.singleShot(0, self.activateWindow)
        QTimer.singleShot(0, lambda: self.setFocus(Qt.ActiveWindowFocusReason))
        if os.name == "nt":
            try:
                hwnd = int(self.winId())
                ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            except Exception:
                pass

    def reject(self):
        try: self.releaseKeyboard()
        except Exception: pass
        super().reject()

    def closeEvent(self, event):
        try: self.releaseKeyboard()
        except Exception: pass
        if os.name == "nt":
            try:
                ctypes.windll.user32.SetWindowPos(int(self.winId()), -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            except Exception:
                pass
        super().closeEvent(event)

    def selected_region(self):
        return dict(self._region) if self._region else None

    def selected_pixel_rect(self):
        rect = self.canvas.selection
        return {
            "left": int(rect.left()),
            "top": int(rect.top()),
            "width": int(rect.width()),
            "height": int(rect.height()),
        }

    @property
    def too_small(self):
        return self._too_small
    region_selected = Signal(dict)
