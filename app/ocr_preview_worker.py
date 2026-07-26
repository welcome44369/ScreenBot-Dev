import time
from datetime import datetime

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QImage


class OCRPreviewWorker(QObject):
    """Background worker for one-shot OCR preview."""

    completed = Signal(dict)
    failed = Signal(str)

    def __init__(self, text_detector, region):
        super().__init__()
        self.text_detector = text_detector
        self.region = dict(region)

    @Slot()
    def run(self):
        started = time.perf_counter()
        try:
            image = self.text_detector.capture_region(self.region)
            result = self.text_detector.recognize_image(image)
            text = result["clean_text"]

            image_rgba = image.convert("RGBA")
            raw = image_rgba.tobytes("raw", "RGBA")
            qimage = QImage(raw, image_rgba.width, image_rgba.height, QImage.Format_RGBA8888).copy()

            elapsed_ms = int(round((time.perf_counter() - started) * 1000.0))
            payload = {
                "image": qimage,
                "text": text,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "elapsed_ms": elapsed_ms,
                "image_width": image_rgba.width,
                "image_height": image_rgba.height,
                "candidates": result["candidates"],
                "confidence": result["confidence"],
                "pipeline_metadata": result["pipeline_metadata"],
            }
            self.completed.emit(payload)
        except Exception as exc:
            self.failed.emit(str(exc))
