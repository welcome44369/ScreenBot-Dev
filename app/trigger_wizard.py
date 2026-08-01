"""OCR Trigger creation wizard using a single target-client screenshot."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import re

from PIL import ImageFilter, ImageOps
import pytesseract
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from app.dialog_utils import show_warning
from app.ocr_correction_store import OCRCorrectionStore
from app.ocr_region_selector import OCRRegionSelectorDialog, pil_image_to_qpixmap
from app.selection_session import SelectionSession
from app.trigger_conditions import (
    LEGACY_APPEAR,
    LEGACY_DISAPPEAR,
    add_condition_items,
    apply_condition_to_payload,
    condition_code,
    condition_from_code,
    ui_code_from_trigger,
)
from app.trigger_store import DEFAULT_REGION


class TriggerWizard(QDialog):
    """Create or edit a Trigger from OCR candidates; candidates cannot be invented."""

    def __init__(self, trigger_store, window_tracker, text_detector, parent=None, data=None):
        super().__init__(parent)
        self.store = trigger_store
        self.tracker = window_tracker
        self.detector = text_detector
        self.logger = logging.getLogger("ScreenBot.OCRWizard")
        self.correction_store = OCRCorrectionStore(self.store.root_path)

        self.setWindowTitle("新增 OCR 偵測觸發點")
        self.resize(460, 420)
        self.region = dict(DEFAULT_REGION)
        self.ocr_text = ""
        self._screenshot = None
        self._last_crop = None
        self._last_pipeline_result = None
        self._selector = None
        self._selection_session = None
        self._editing_trigger_id = None
        self._editing_trigger_data = None

        form = QFormLayout(self)
        self.name_edit = QLineEdit()
        self.event = QComboBox()
        self._initialize_event_combo()
        self.raw_ocr = QPlainTextEdit()
        self.raw_ocr.setReadOnly(True)
        self.raw_ocr.setMaximumHeight(90)
        self.candidates = QListWidget()
        self.status = QLineEdit()
        self.status.setReadOnly(True)
        self.poll = QSpinBox()
        self.poll.setRange(1, 60000)
        self.poll.setValue(500)
        self.confirm = QSpinBox()
        self.confirm.setRange(1, 100)
        self.confirm.setValue(2)
        self.cooldown = QSpinBox()
        self.cooldown.setRange(0, 60000)

        form.addRow("名稱", self.name_edit)
        form.addRow("事件", self.event)
        form.addRow("原始 OCR", self.raw_ocr)
        form.addRow("監測文字（可多選）", self.candidates)

        buttons = QVBoxLayout()
        capture = QPushButton("截圖並框選 OCR Region")
        reocr = QPushButton("重新 OCR")
        edit = QPushButton("編輯文字")
        delete = QPushButton("刪除文字")
        save = QPushButton("儲存")
        cancel = QPushButton("取消")
        for button in (capture, reocr, edit, delete, save, cancel):
            buttons.addWidget(button)
        form.addRow("Region / OCR", buttons)
        form.addRow("Poll Interval", self.poll)
        form.addRow("Confirm Frames", self.confirm)
        form.addRow("Cooldown", self.cooldown)
        form.addRow("狀態", self.status)

        capture.clicked.connect(self.capture)
        reocr.clicked.connect(self.run_ocr)
        edit.clicked.connect(self.edit_text)
        delete.clicked.connect(self.delete_text)
        save.clicked.connect(self.save)
        cancel.clicked.connect(self.reject)

        if data is not None:
            self._populate_from_data(data)

    @staticmethod
    def _normalized(value):
        return re.sub(r"\s+", "", (value or "").strip()).casefold()

    def _item_metadata(self, item):
        return item.data(Qt.ItemDataRole.UserRole) or {
            "source": "ocr",
            "text": item.text(),
            "original_text": item.text(),
        }

    def _initialize_event_combo(self):
        add_condition_items(self.event)
        if self.event.count() > 0:
            self.event.setCurrentIndex(0)

    def _set_condition_code(self, code):
        if not code:
            return
        for index in range(self.event.count()):
            if self.event.itemData(index) == code:
                self.event.setCurrentIndex(index)
                return

    def _condition_code_for_trigger(self, data):
        if not isinstance(data, dict):
            return None
        condition = data.get("condition")
        code = condition_code(condition)
        if code:
            return code
        ui_code = ui_code_from_trigger(data)
        if ui_code == LEGACY_APPEAR:
            return "edge_present"
        if ui_code == LEGACY_DISAPPEAR:
            return "edge_absent"
        return None

    def _populate_from_data(self, data):
        if not isinstance(data, dict):
            return
        self._editing_trigger_id = data.get("id")
        self._editing_trigger_data = dict(data)
        self.name_edit.setText(str(data.get("name", "")))
        self._set_condition_code(self._condition_code_for_trigger(data))
        self.poll.setValue(int(data.get("poll_interval_ms", 500)))
        self.confirm.setValue(int(data.get("confirm_frames", 2)))
        self.cooldown.setValue(int(data.get("cooldown_ms", 0)))
        texts = data.get("texts") or [data.get("text", "")]
        if not isinstance(texts, list):
            texts = [str(texts)]
        self.candidates.clear()
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                continue
            self._add_candidate(
                {
                    "source": "existing",
                    "text": text.strip(),
                    "original_text": text.strip(),
                },
                checked=True,
            )
        region = data.get("region") or DEFAULT_REGION
        if isinstance(region, dict):
            self.region = {
                "x_ratio": float(region.get("x_ratio", DEFAULT_REGION["x_ratio"])),
                "y_ratio": float(region.get("y_ratio", DEFAULT_REGION["y_ratio"])),
                "width_ratio": float(region.get("width_ratio", DEFAULT_REGION["width_ratio"])),
                "height_ratio": float(region.get("height_ratio", DEFAULT_REGION["height_ratio"])),
            }
        else:
            self.region = dict(DEFAULT_REGION)
        self.status.setText("已載入現有觸發點設定")

    def _add_candidate(self, metadata, checked=False):
        text = metadata["text"].strip()
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        )
        item.setData(Qt.ItemDataRole.UserRole, metadata)
        self.candidates.addItem(item)

    def capture(self):
        if getattr(self.tracker, "target", None) is None:
            show_warning(self, "無目標", "請先鎖定要偵測的目標視窗。")
            return
        try:
            self.logger.info("SELECTION_CAPTURE_START")
            locked_target = self.tracker.target
            image = self.detector.capture_client_image(
                int(locked_target.hwnd), allow_desktop_fallback=False
            )
            self._screenshot = image
            debug_path = Path(self.store.root_path) / "logs" / "ocr-debug"
            debug_path.mkdir(parents=True, exist_ok=True)
            image.save(debug_path / "last-target-client.png")
            (debug_path / "last-target-client.json").write_text(
                json.dumps(
                    {
                        "requested_hwnd": f"0x{locked_target.hwnd:08X}",
                        "requested_title": locked_target.title,
                        "requested_pid": locked_target.pid,
                        "backend": "target_capture_service",
                        "fallback_used": False,
                        "captured_size": [image.width, image.height],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self.logger.info("SELECTION_FROZEN_IMAGE_SOURCE target_client")
            self._selection_session = SelectionSession(self.logger)
            self._selection_session.begin()
            QTimer.singleShot(0, self._show_selector_from_frozen_image)
        except Exception as exc:
            self.logger.error("TARGET_CAPTURE_ABORT_SELECTOR true")
            if self._selection_session:
                self._selection_session.end(self)
                self._selection_session = None
            show_warning(self, "截圖失敗", str(exc))

    def _show_selector_from_frozen_image(self):
        try:
            if self._screenshot is None:
                raise RuntimeError("OCR 定格圖片不存在。")
            self._selector = OCRRegionSelectorDialog(
                pil_image_to_qpixmap(self._screenshot), parent=None
            )
            self._selector.setWindowModality(Qt.WindowModality.NonModal)
            self._selector.finished.connect(self._region_finished)
            self._selector.show()
            self._selector.raise_()
            self._selector.activateWindow()
            self.logger.info("SELECTION_SELECTOR_SHOWN")
        except Exception as exc:
            self.logger.exception("SELECTION_SESSION_ERROR %s", exc)
            if self._selection_session:
                self._selection_session.end(self)
                self._selection_session = None
            show_warning(self, "截圖失敗", str(exc))

    def run_ocr(self):
        if not self.region or self._screenshot is None:
            return
        try:
            width, height = self._screenshot.width, self._screenshot.height
            box = (
                round(self.region["x_ratio"] * width),
                round(self.region["y_ratio"] * height),
                round(
                    (self.region["x_ratio"] + self.region["width_ratio"])
                    * width
                ),
                round(
                    (self.region["y_ratio"] + self.region["height_ratio"])
                    * height
                ),
            )
            box = (max(0, box[0]), max(0, box[1]), min(width, box[2]), min(height, box[3]))
            if box[2] <= box[0] or box[3] <= box[1]:
                raise ValueError(f"OCR 選區無效：{box[2] - box[0]}×{box[3] - box[1]} px")
            image = self._screenshot.crop(box)
            if image.width < 2 or image.height < 2:
                raise ValueError(f"OCR 選區無效：{image.width}×{image.height} px")
            language = getattr(self.detector, "lang", "eng+chi_tra") or "eng+chi_tra"
            if "chi_tra" in language.split("+") and "chi_tra" not in pytesseract.get_languages(config=""):
                raise RuntimeError("缺少 Tesseract 繁體中文語言資料 chi_tra。")
            self.logger.info("OCR_SELECTION x=%s y=%s w=%s h=%s", box[0], box[1], image.width, image.height)
            debug_path = Path(self.store.root_path) / "logs" / "ocr-debug"
            debug_path.mkdir(parents=True, exist_ok=True)
            image.save(debug_path / "last-crop.png")
            image.save(debug_path / "last-crop-original.png")
            upscaled = ImageOps.autocontrast(
                ImageOps.grayscale(image).resize((image.width * 3, image.height * 3))
            ).filter(ImageFilter.SHARPEN)
            upscaled.save(debug_path / "last-crop-upscaled.png")
            upscaled.point(lambda value: 255 if value > 175 else 0).save(
                debug_path / "last-crop-threshold.png"
            )
            result = self.detector.recognize_image(image, layout_hint="multi_line")
            self._last_crop = image.copy()
            self._last_pipeline_result = result
            self.ocr_text = result["clean_text"]
            self.raw_ocr.setPlainText(self.ocr_text or "No text detected.")
            self._merge_ocr_candidates(result)
            self.status.setText(self.ocr_text or "No text detected.")
        except Exception as exc:
            self.logger.exception("OCR_ERROR %s", exc)
            self.status.setText(f"OCR_ERROR {exc}")
            self.raw_ocr.setPlainText(f"OCR_ERROR {exc}")

    def _merge_ocr_candidates(self, result):
        corrected = []
        checked = set()
        for index in range(self.candidates.count()):
            item = self.candidates.item(index)
            metadata = self._item_metadata(item)
            if item.checkState() == Qt.CheckState.Checked:
                checked.add(self._normalized(item.text()))
            if metadata.get("source") == "user_corrected":
                corrected.append((item.text(), metadata, item.checkState() == Qt.CheckState.Checked))

        by_text = {
            self._normalized(item[0]): item
            for item in corrected
        }
        self.candidates.clear()
        for pipeline_metadata in result.get("candidate_metadata", []):
            text = pipeline_metadata["text"].strip()
            key = self._normalized(text)
            if key in by_text:
                _, metadata, was_checked = by_text.pop(key)
                metadata = dict(metadata)
                metadata["pipeline_metadata"] = pipeline_metadata
                self._add_candidate(metadata, was_checked)
                continue
            self._add_candidate(
                {
                    "source": "ocr",
                    "text": text,
                    "original_text": text,
                    "pipeline_metadata": pipeline_metadata,
                },
                key in checked,
            )
        for text, metadata, was_checked in corrected:
            if self._normalized(text) not in {
                self._normalized(self.candidates.item(index).text())
                for index in range(self.candidates.count())
            }:
                self._add_candidate(metadata, was_checked)

    def edit_text(self):
        item = self.candidates.currentItem()
        if item is None:
            return
        text, ok = QInputDialog.getText(
            self, "編輯文字", "文字：", text=item.text()
        )
        if not ok or not text.strip():
            return
        metadata = dict(self._item_metadata(item))
        metadata["source"] = "user_corrected"
        metadata["original_text"] = metadata.get("original_text") or item.text()
        metadata["text"] = text.strip()
        item.setText(text.strip())
        item.setData(Qt.ItemDataRole.UserRole, metadata)

    def delete_text(self):
        row = self.candidates.currentRow()
        if row >= 0:
            self.candidates.takeItem(row)

    def save(self):
        selected = []
        corrections = []
        for index in range(self.candidates.count()):
            item = self.candidates.item(index)
            if item.checkState() != Qt.CheckState.Checked or not item.text().strip():
                continue
            metadata = dict(self._item_metadata(item))
            selected.append(item.text().strip())
            if metadata.get("source") == "user_corrected":
                corrections.append(
                    {
                        "source": "user_corrected",
                        "original_text": metadata.get("original_text"),
                        "text": item.text().strip(),
                        "pipeline_metadata": metadata.get("pipeline_metadata"),
                    }
                )
        if not selected or not self.name_edit.text().strip() or not self.region:
            show_warning(self, "資料不足", "請完成名稱、Region 並至少選擇一個 OCR 文字。")
            return
        try:
            selected_code = self.event.currentData() or "initial_absent"
            payload = {
                "version": 1,
                "name": self.name_edit.text().strip(),
                "type": "text",
                "texts": selected,
                "match_mode": "any",
                "region": self.region,
                "poll_interval_ms": self.poll.value(),
                "confirm_frames": self.confirm.value(),
                "cooldown_ms": self.cooldown.value(),
            }
            payload = apply_condition_to_payload(payload, selected_code)
            payload["event"] = "appear" if selected_code.endswith("_present") else "disappear"
            if self._editing_trigger_id is None:
                trigger_id = self.store.save_trigger(payload)
            else:
                trigger_id = self.store.update_trigger(self._editing_trigger_id, payload)
            if self._last_crop is not None and self._last_pipeline_result is not None:
                self.correction_store.record(
                    crop_image=self._last_crop,
                    pipeline_result=self._last_pipeline_result,
                    corrections=corrections,
                    trigger_id=trigger_id,
                )
            self.accept()
        except Exception as exc:
            show_warning(self, "儲存失敗", str(exc))

    def _region_finished(self, result):
        selector = self._selector
        self._selector = None
        if self._selection_session:
            self._selection_session.end(self)
            self._selection_session = None
        if result == QDialog.Accepted and selector is not None:
            self.logger.info("SELECTION_CONFIRMED")
            self.region = selector.selected_region()
            self.run_ocr()
        else:
            self.logger.info("SELECTION_CANCELLED")

    def closeEvent(self, event):
        if self._selection_session is not None:
            self._selection_session.end(self)
            self._selection_session = None
        super().closeEvent(event)
