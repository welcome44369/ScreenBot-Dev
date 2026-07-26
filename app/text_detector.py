import logging

import pytesseract
from app.target_capture import TargetCaptureService
from app.ocr_pipeline import OCRPipeline
from app.observation_engine import ObservationEngine


class TextDetector:
    def __init__(self, window_tracker, logger=None, tesseract_cmd=None, lang="eng", ocr_config="--psm 7"):
        self.window_tracker = window_tracker
        self.logger = logger or logging.getLogger("ScreenBot")
        self.lang = lang
        self.ocr_config = ocr_config
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        self.capture_service = TargetCaptureService(window_tracker, self.logger)
        self.ocr_pipeline = OCRPipeline(self.logger)

    def capture_client_image(self, target_hwnd, *, allow_desktop_fallback=False, force_backend=None):
        return self.capture_service.capture_target_client(
            target_hwnd,
            allow_desktop_fallback=allow_desktop_fallback,
            force_backend=force_backend,
        )

    def capture_region(self, region):
        target = self.window_tracker.target
        if target is None:
            raise RuntimeError("No locked target window")
        image = self.capture_client_image(int(target.hwnd), allow_desktop_fallback=False)
        left = int(round(region["x_ratio"] * image.width))
        top = int(round(region["y_ratio"] * image.height))
        right = int(round((region["x_ratio"] + region["width_ratio"]) * image.width))
        bottom = int(round((region["y_ratio"] + region["height_ratio"]) * image.height))
        if right <= left or bottom <= top:
            raise ValueError("Invalid OCR region dimensions")
        return image.crop((left, top, right, bottom))

    def detect_text(self, region):
        image = self.capture_region(region)
        return self.recognize_image(image)["clean_text"]

    def observe_text(self, region, target_text, observation_config=None):
        """Capture once, run formal OCR once, then return a four-state result."""
        engine = ObservationEngine(observation_config)
        try:
            image = self.capture_region(region)
            recognized = self.recognize_image(image)["clean_text"]
            return engine.observe(target_text, recognized, image)
        except Exception as exc:
            self.logger.warning("OBSERVATION_INVALID reason=%s", exc)
            return engine.observe(target_text, "", None, valid=False, reason=str(exc))

    def recognize_image(self, image, *, layout_hint="multi_line"):
        """Use the same confidence-aware OCR path as the Trigger Wizard."""
        return self.ocr_pipeline.recognize_text(image, languages=self.lang, layout_hint=layout_hint)

    def close(self):
        self.capture_service.close()

    def invalidate_capture_target(self, hwnd=None):
        self.capture_service.invalidate_target(hwnd)

    def get_capture_runtime_diagnostics(self):
        return self.capture_service.get_runtime_diagnostics()
