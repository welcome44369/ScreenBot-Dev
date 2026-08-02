"""
ScreenBot Workflow Editor
Linear Text-Trigger Workflow Editor built with PySide6.

Architecture:
  - Maintains a _draft dict (working copy) — never mutates the running workflow.
  - Commits form → draft on step switch and before save/validate.
  - Writes to disk only on Save / Save As.
  - Unknown fields in loaded JSON are preserved on save.
  - Unsupported trigger types (non-text) are shown read-only; data preserved.
"""
import copy
import re
import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QComboBox, QSpinBox, QListWidget, QPushButton, QSplitter,
    QFormLayout, QGroupBox, QMessageBox, QInputDialog,
    QScrollArea, QSizePolicy, QPlainTextEdit, QCheckBox,
)

from app.ocr_region_selector import OCRRegionSelectorDialog, pil_image_to_qpixmap
from app.ocr_preview_worker import OCRPreviewWorker
from app.trigger_conditions import normalize_trigger_payload

VALID_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")
SUPPORTED_TRIGGER_TYPES = {"text"}
DEFAULT_REGION = {"x_ratio": 0.0, "y_ratio": 0.0, "width_ratio": 1.0, "height_ratio": 1.0}


class WorkflowEditor(QWidget):
    """Non-blocking editor window for linear Text-Trigger workflows."""

    workflow_saved = Signal(str)  # emits filename after successful save

    def __init__(
        self,
        workflow_store,
        script_store,
        get_running_filename=None,
        logger=None,
        window_tracker=None,
        text_detector=None,
        is_workflow_running=None,
        trigger_store=None,
    ):
        """
        Args:
            workflow_store: WorkflowStore instance (list / load / save).
            script_store:   ScriptStore instance (list macros).
            get_running_filename: Callable() → str|None; returns filename of
                                  the currently running workflow, if any.
            logger: Optional logger.
            trigger_store: TriggerStore instance for stop-condition selection.
        """
        super().__init__(None, Qt.Window)
        self.setWindowTitle("Workflow Editor")
        self.setMinimumSize(720, 520)
        self.resize(880, 620)

        self.workflow_store = workflow_store
        self.script_store = script_store
        self.trigger_store = trigger_store
        self.get_running_filename = get_running_filename or (lambda: None)
        self.logger = logger or logging.getLogger("ScreenBot")
        self.window_tracker = window_tracker
        self.text_detector = text_detector
        self.is_workflow_running = is_workflow_running or (lambda: False)

        self._draft: dict | None = None
        self._current_filename: str | None = None
        self._dirty = False
        self._current_step_index = -1
        self._ignoring_form = False  # suppress _mark_dirty during programmatic field updates
        self._current_region = dict(DEFAULT_REGION)
        self._preview_thread = None
        self._preview_worker = None

        self._setup_ui()
        self._refresh_macro_list()
        self._new_workflow()

    # ─── UI Construction ────────────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Top toolbar: file actions + workflow name
        toolbar = QHBoxLayout()
        self.btn_new     = QPushButton("New")
        self.btn_open    = QPushButton("Open…")
        self.btn_save    = QPushButton("Save")
        self.btn_save_as = QPushButton("Save As…")
        for btn in (self.btn_new, self.btn_open, self.btn_save, self.btn_save_as):
            btn.setFixedHeight(28)
            toolbar.addWidget(btn)
        toolbar.addSpacing(16)
        toolbar.addWidget(QLabel("Workflow Name:"))
        self.wf_name_edit = QLineEdit()
        self.wf_name_edit.setPlaceholderText("My Workflow")
        toolbar.addWidget(self.wf_name_edit, 1)
        root.addLayout(toolbar)

        # Status line (shows save success / error)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("font-size: 11px; color: #448844;")
        root.addWidget(self.status_label)

        # Workflow-level loop policy
        loop_group = QGroupBox("Loop Policy")
        loop_form = QFormLayout(loop_group)
        self.loop_enable_chk = QCheckBox("Enable Loop")
        self.loop_mode_combo = QComboBox()
        self.loop_mode_combo.addItem("直到手動停止", "manual_stop")
        self.loop_mode_combo.addItem("指定執行循環次數", "max_cycles")
        self.loop_mode_combo.addItem("直到停止條件成立", "stop_trigger")
        self.loop_restart_combo = QComboBox()
        self.loop_max_cycles = QSpinBox()
        self.loop_max_cycles.setRange(1, 1000000)
        self.loop_max_cycles.setValue(1)
        loop_form.addRow(self.loop_enable_chk)
        loop_form.addRow("Mode:", self.loop_mode_combo)
        loop_form.addRow("Restart Step:", self.loop_restart_combo)
        loop_form.addRow("Maximum Cycles:", self.loop_max_cycles)

        self.stop_group = QGroupBox("停止條件")
        stop_form = QFormLayout(self.stop_group)
        stop_hint = QLabel("僅偵測目前鎖定的目標視窗")
        stop_hint.setStyleSheet("font-size: 11px; color: #555;")
        self.stop_ref_combo = QComboBox()
        self.stop_ref_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.stop_ref_combo.addItem("（請選擇停止條件）", None)
        self.btn_refresh_stop = QPushButton("重新整理")
        self.btn_refresh_stop.setFixedHeight(24)
        stop_ref_row = QHBoxLayout()
        stop_ref_row.addWidget(self.stop_ref_combo, 1)
        stop_ref_row.addWidget(self.btn_refresh_stop)
        stop_form.addRow(stop_hint)
        stop_form.addRow("Trigger:", stop_ref_row)

        root.addWidget(loop_group)
        root.addWidget(self.stop_group)

        # Main content: step list (left) + step form (right)
        splitter = QSplitter(Qt.Horizontal)

        # ── Left: Step list ──────────────────────────────────────────────
        left_panel = QWidget()
        ll = QVBoxLayout(left_panel)
        ll.setContentsMargins(0, 0, 4, 0)
        ll.addWidget(QLabel("Steps (execution order):"))
        self.step_list = QListWidget()
        self.step_list.setMinimumWidth(160)
        ll.addWidget(self.step_list, 1)

        step_btns = QHBoxLayout()
        self.btn_add = QPushButton("+")
        self.btn_del = QPushButton("Del")
        self.btn_dup = QPushButton("Copy")
        self.btn_up  = QPushButton("↑")
        self.btn_dn  = QPushButton("↓")
        for b in (self.btn_add, self.btn_del, self.btn_dup, self.btn_up, self.btn_dn):
            b.setFixedHeight(26)
            step_btns.addWidget(b)
        ll.addLayout(step_btns)
        splitter.addWidget(left_panel)

        # ── Right: Step settings form ────────────────────────────────────
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_inner = QWidget()
        rl = QVBoxLayout(right_inner)
        rl.setContentsMargins(8, 4, 8, 4)
        rl.setSpacing(8)

        # Step ID
        grp_step = QGroupBox("Step")
        fs = QFormLayout(grp_step)
        self.fld_step_id = QLineEdit()
        self.fld_step_id.setPlaceholderText("wait_ready")
        fs.addRow("Step ID:", self.fld_step_id)
        rl.addWidget(grp_step)

        # Trigger settings
        grp_trig = QGroupBox("Trigger")
        ft = QFormLayout(grp_trig)
        self.fld_type = QComboBox()
        self.fld_type.addItem("text", "text")
        ft.addRow("Type:", self.fld_type)
        self.fld_event = QComboBox()
        self.fld_event.addItem("appear",    "appear")
        self.fld_event.addItem("disappear", "disappear")
        ft.addRow("Event:", self.fld_event)
        self.fld_text = QLineEdit()
        self.fld_text.setPlaceholderText("Text to detect…")
        ft.addRow("Text:", self.fld_text)
        self.fld_poll = QSpinBox()
        self.fld_poll.setRange(100, 10000)
        self.fld_poll.setSingleStep(100)
        self.fld_poll.setSuffix(" ms")
        self.fld_poll.setValue(500)
        ft.addRow("Poll Interval:", self.fld_poll)
        self.fld_confirm = QSpinBox()
        self.fld_confirm.setRange(1, 100)
        self.fld_confirm.setValue(2)
        ft.addRow("Confirm Frames:", self.fld_confirm)
        self.fld_cooldown = QSpinBox()
        self.fld_cooldown.setRange(0, 60000)
        self.fld_cooldown.setSingleStep(100)
        self.fld_cooldown.setSuffix(" ms")
        self.fld_cooldown.setValue(1000)
        ft.addRow("Cooldown:", self.fld_cooldown)
        rl.addWidget(grp_trig)

        # OCR region settings
        grp_region = QGroupBox("OCR Region")
        fr = QFormLayout(grp_region)
        self.region_x = QLineEdit()
        self.region_y = QLineEdit()
        self.region_w = QLineEdit()
        self.region_h = QLineEdit()
        for fld in (self.region_x, self.region_y, self.region_w, self.region_h):
            fld.setReadOnly(True)
            fld.setPlaceholderText("0.0000")
        fr.addRow("X:", self.region_x)
        fr.addRow("Y:", self.region_y)
        fr.addRow("Width:", self.region_w)
        fr.addRow("Height:", self.region_h)
        region_btn_row = QHBoxLayout()
        self.btn_select_region = QPushButton("Select Region")
        self.btn_full_region = QPushButton("Full Window")
        self.btn_preview_ocr = QPushButton("Preview OCR")
        region_btn_row.addWidget(self.btn_select_region)
        region_btn_row.addWidget(self.btn_full_region)
        region_btn_row.addWidget(self.btn_preview_ocr)
        fr.addRow(region_btn_row)
        rl.addWidget(grp_region)

        # OCR preview panel
        grp_preview = QGroupBox("Live OCR Preview")
        fp = QVBoxLayout(grp_preview)
        self.preview_hint_label = QLabel(
            "OCR uses visible screen pixels.\nKeep the target window visible and unobstructed."
        )
        self.preview_hint_label.setWordWrap(True)
        self.preview_hint_label.setStyleSheet("font-size: 11px; color: #555;")
        self.preview_foreground_warn = QLabel("")
        self.preview_foreground_warn.setWordWrap(True)
        self.preview_foreground_warn.setStyleSheet("font-size: 11px; color: orange;")
        self.preview_image_label = QLabel("No preview image")
        self.preview_image_label.setAlignment(Qt.AlignCenter)
        self.preview_image_label.setMinimumSize(280, 140)
        self.preview_image_label.setStyleSheet("border: 1px solid #888; background: #1f1f1f; color: #d0d0d0;")
        self.preview_text = QPlainTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setPlaceholderText("Detected text...")
        self.preview_text.setMaximumBlockCount(400)
        self.preview_status = QLabel("Status: Idle")
        self.preview_time = QLabel("Timestamp: N/A")
        fp.addWidget(self.preview_hint_label)
        fp.addWidget(self.preview_foreground_warn)
        fp.addWidget(self.preview_image_label)
        fp.addWidget(self.preview_text)
        fp.addWidget(self.preview_status)
        fp.addWidget(self.preview_time)
        preview_copy_row = QHBoxLayout()
        self.btn_copy_ocr_text = QPushButton("Copy OCR Text")
        self.btn_copy_region = QPushButton("Copy Region")
        preview_copy_row.addWidget(self.btn_copy_ocr_text)
        preview_copy_row.addWidget(self.btn_copy_region)
        preview_copy_row.addStretch()
        fp.addLayout(preview_copy_row)
        rl.addWidget(grp_preview)

        # Macro selection
        grp_macro = QGroupBox("Macro")
        fm = QFormLayout(grp_macro)
        macro_row = QHBoxLayout()
        self.fld_macro = QComboBox()
        self.fld_macro.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_refresh_macro = QPushButton("Refresh")
        self.btn_refresh_macro.setFixedHeight(24)
        macro_row.addWidget(self.fld_macro, 1)
        macro_row.addWidget(self.btn_refresh_macro)
        fm.addRow("Macro:", macro_row)
        rl.addWidget(grp_macro)

        # Warning for unsupported trigger types
        self.unsupported_label = QLabel(
            "⚠ This step uses an unsupported trigger type and cannot be edited here.\n"
            "Its original data will be preserved when you save."
        )
        self.unsupported_label.setStyleSheet("color: orange; font-size: 11px;")
        self.unsupported_label.setWordWrap(True)
        self.unsupported_label.setVisible(False)
        rl.addWidget(self.unsupported_label)

        rl.addStretch()
        right_scroll.setWidget(right_inner)
        splitter.addWidget(right_scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter, 1)

        # Validation error display
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: red; font-size: 11px;")
        self.error_label.setWordWrap(True)
        root.addWidget(self.error_label)

        # ── Signal connections ──────────────────────────────────────────
        self.btn_new.clicked.connect(self._on_new)
        self.btn_open.clicked.connect(self._on_open)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_save_as.clicked.connect(self._on_save_as)
        self.btn_add.clicked.connect(self._on_add_step)
        self.btn_del.clicked.connect(self._on_del_step)
        self.btn_dup.clicked.connect(self._on_dup_step)
        self.btn_up.clicked.connect(self._on_move_up)
        self.btn_dn.clicked.connect(self._on_move_down)
        self.btn_refresh_macro.clicked.connect(self._refresh_macro_list)
        self.btn_select_region.clicked.connect(self._on_select_region)
        self.btn_full_region.clicked.connect(self._on_set_full_window_region)
        self.btn_preview_ocr.clicked.connect(self._on_preview_ocr)
        self.btn_refresh_stop.clicked.connect(self._refresh_stop_triggers)
        self.btn_copy_ocr_text.clicked.connect(self._copy_preview_text)
        self.btn_copy_region.clicked.connect(self._copy_region_text)
        self.step_list.currentRowChanged.connect(self._on_step_row_changed)

        self.wf_name_edit.textChanged.connect(self._mark_dirty)
        for fld in (self.fld_step_id, self.fld_text):
            fld.textChanged.connect(self._mark_dirty)
        for fld in (self.fld_type, self.fld_event, self.fld_macro):
            fld.currentIndexChanged.connect(self._mark_dirty)
        for fld in (self.fld_poll, self.fld_confirm, self.fld_cooldown):
            fld.valueChanged.connect(self._mark_dirty)
        self.loop_enable_chk.toggled.connect(self._on_loop_controls_changed)
        self.loop_mode_combo.currentIndexChanged.connect(self._on_loop_controls_changed)
        self.loop_restart_combo.currentIndexChanged.connect(self._mark_dirty)
        self.loop_max_cycles.valueChanged.connect(self._mark_dirty)
        self.stop_ref_combo.currentIndexChanged.connect(self._mark_dirty)

        self._set_form_enabled(False)
        self._refresh_restart_step_options()
        self._refresh_stop_triggers()
        self._update_loop_visibility()
        self._ignoring_form = True
        self._on_loop_controls_changed()
        self._ignoring_form = False

    # ─── Helpers ────────────────────────────────────────────────────────────

    def _mark_dirty(self, *_):
        if self._ignoring_form:
            return
        self._dirty = True
        self._update_title()

    def _update_title(self):
        name = (self._draft or {}).get("name", "")
        fn   = self._current_filename or "(new)"
        star = " *" if self._dirty else ""
        self.setWindowTitle(f"Workflow Editor — {name or fn}{star}")

    def _set_status(self, msg, error=False):
        color = "red" if error else "#448844"
        self.status_label.setStyleSheet(f"font-size: 11px; color: {color};")
        self.status_label.setText(msg)

    def _set_form_enabled(self, enabled):
        for w in (self.fld_step_id, self.fld_type, self.fld_event, self.fld_text,
                  self.fld_poll, self.fld_confirm, self.fld_cooldown,
                  self.fld_macro, self.btn_refresh_macro,
                  self.btn_select_region, self.btn_full_region, self.btn_preview_ocr,
                  self.btn_copy_ocr_text, self.btn_copy_region):
            w.setEnabled(enabled)
        self.loop_enable_chk.setEnabled(enabled)
        self.loop_mode_combo.setEnabled(enabled and self.loop_enable_chk.isChecked())
        self.loop_restart_combo.setEnabled(enabled and self.loop_enable_chk.isChecked())
        self.loop_max_cycles.setEnabled(
            enabled and self.loop_enable_chk.isChecked() and self.loop_mode_combo.currentData() == "max_cycles"
        )
        self._update_loop_visibility()

    def _update_loop_visibility(self):
        """Show the stop-condition group only when in stop_trigger mode."""
        self.stop_group.setVisible(self._is_stop_trigger_mode())

    def _set_fields_readonly(self, readonly):
        """Used for unsupported trigger steps — show data but block editing."""
        for w in (self.fld_step_id, self.fld_text):
            w.setReadOnly(readonly)
        for w in (self.fld_type, self.fld_event, self.fld_macro):
            w.setEnabled(not readonly)
        for w in (self.fld_poll, self.fld_confirm, self.fld_cooldown):
            w.setReadOnly(readonly)
        self._set_region_controls_enabled(not readonly)

    def _set_region_controls_enabled(self, enabled):
        self.btn_select_region.setEnabled(enabled)
        self.btn_full_region.setEnabled(enabled)
        self.btn_preview_ocr.setEnabled(enabled and self._preview_thread is None)
        self.btn_copy_region.setEnabled(enabled)
        self.btn_copy_ocr_text.setEnabled(True)

    def _refresh_macro_list(self):
        try:
            scripts = self.script_store.list_scripts()
        except Exception:
            scripts = []
        current = self.fld_macro.currentData()
        self._ignoring_form = True
        self.fld_macro.clear()
        self.fld_macro.addItem("(select macro)", None)
        for filename, display in scripts:
            label = f"{display}  [{filename}]" if display != filename else filename
            self.fld_macro.addItem(label, filename)
        if current:
            idx = self.fld_macro.findData(current)
            if idx >= 0:
                self.fld_macro.setCurrentIndex(idx)
        self._ignoring_form = False

    def _refresh_stop_triggers(self):
        """Reload the stop condition trigger list from TriggerStore (OCR text triggers only)."""
        if self.trigger_store is None:
            return
        current = self.stop_ref_combo.currentData()
        self._ignoring_form = True
        self.stop_ref_combo.clear()
        self.stop_ref_combo.addItem("（請選擇停止條件）", None)
        for item in self.trigger_store.list_triggers():
            try:
                trigger = self.trigger_store.load_trigger(item["id"])
                if trigger.get("type", "text") == "text":
                    self.stop_ref_combo.addItem(item["name"], item["id"])
            except Exception:
                pass
        if current:
            idx = self.stop_ref_combo.findData(current)
            if idx >= 0:
                self.stop_ref_combo.setCurrentIndex(idx)
        self._ignoring_form = False

    @staticmethod
    def _normalize_region(region):
        if not isinstance(region, dict):
            return dict(DEFAULT_REGION)
        try:
            x = float(region.get("x_ratio", 0.0))
            y = float(region.get("y_ratio", 0.0))
            w = float(region.get("width_ratio", 1.0))
            h = float(region.get("height_ratio", 1.0))
        except Exception:
            return dict(DEFAULT_REGION)
        x = max(0.0, min(1.0, x))
        y = max(0.0, min(1.0, y))
        w = max(0.000001, min(1.0, w))
        h = max(0.000001, min(1.0, h))
        if x + w > 1.0:
            w = max(0.000001, 1.0 - x)
        if y + h > 1.0:
            h = max(0.000001, 1.0 - y)
        return {
            "x_ratio": round(x, 6),
            "y_ratio": round(y, 6),
            "width_ratio": round(w, 6),
            "height_ratio": round(h, 6),
        }

    @staticmethod
    def _int_or_default(value, default):
        try:
            return int(value)
        except Exception:
            return default

    def _set_region(self, region, mark_dirty=True):
        self._current_region = self._normalize_region(region)
        self.region_x.setText(f"{self._current_region['x_ratio']:.6f}")
        self.region_y.setText(f"{self._current_region['y_ratio']:.6f}")
        self.region_w.setText(f"{self._current_region['width_ratio']:.6f}")
        self.region_h.setText(f"{self._current_region['height_ratio']:.6f}")
        if mark_dirty:
            self._mark_dirty()

    # ─── Draft management ────────────────────────────────────────────────────

    @staticmethod
    def _default_step(step_id="step_1"):
        return {
            "id": step_id,
            "trigger": {
                "type": "text",
                "event": "appear",
                "text": "",
                "region": dict(DEFAULT_REGION),
                "poll_interval_ms": 500,
                "confirm_frames": 2,
                "cooldown_ms": 1000,
            },
            "macro": None,
        }

    def _new_workflow(self):
        self._draft = {
            "name": "New Workflow",
            "steps": [self._default_step("step_1")],
        }
        self._current_filename = None
        self._dirty = False
        self._current_step_index = -1
        self._ignoring_form = True
        self.wf_name_edit.setText("New Workflow")
        self._ignoring_form = False
        self._load_loop_settings()
        self._rebuild_step_list()
        self.step_list.setCurrentRow(0)
        self._dirty = False
        self._update_title()
        self.error_label.setText("")
        self.preview_text.clear()
        self.preview_image_label.setText("No preview image")
        self.preview_status.setText("Status: Idle")
        self.preview_time.setText("Timestamp: N/A")

    def _load_draft(self, data, filename):
        self._draft = copy.deepcopy(data)
        self._current_filename = filename
        self._dirty = False
        self._current_step_index = -1
        self._ignoring_form = True
        self.wf_name_edit.setText(data.get("name", ""))
        self._ignoring_form = False
        self._load_loop_settings()
        self._rebuild_step_list()
        if self._draft.get("steps"):
            self.step_list.setCurrentRow(0)
        else:
            self._set_form_enabled(False)
        self._dirty = False
        self._update_title()
        self.error_label.setText("")
        self.preview_text.clear()
        self.preview_image_label.setText("No preview image")
        self.preview_status.setText("Status: Idle")
        self.preview_time.setText("Timestamp: N/A")

    # ─── Step list ───────────────────────────────────────────────────────────

    def _rebuild_step_list(self):
        self.step_list.blockSignals(True)
        self.step_list.clear()
        for step in (self._draft or {}).get("steps", []):
            self.step_list.addItem(step.get("id", "(unnamed)"))
        self.step_list.blockSignals(False)
        self._refresh_restart_step_options()
        self._update_step_buttons()

    def _on_step_row_changed(self, row):
        if self._ignoring_form:
            return
        self._commit_current_step()
        self._current_step_index = row
        self._load_step_into_form(row)
        self._update_step_buttons()

    def _load_step_into_form(self, row):
        steps = (self._draft or {}).get("steps", [])
        if row < 0 or row >= len(steps):
            self._set_form_enabled(False)
            return
        self._set_form_enabled(True)
        step    = steps[row]
        trigger = step.get("trigger") or {}
        ttype   = trigger.get("type", "text")
        is_supported = ttype in SUPPORTED_TRIGGER_TYPES

        self._ignoring_form = True
        self.fld_step_id.setText(step.get("id", ""))
        # Trigger type
        tidx = self.fld_type.findData(ttype)
        if tidx < 0:
            self.fld_type.addItem(ttype, ttype)
            tidx = self.fld_type.count() - 1
        self.fld_type.setCurrentIndex(tidx)
        # Trigger event
        eidx = self.fld_event.findData(trigger.get("event", "appear"))
        self.fld_event.setCurrentIndex(max(0, eidx))
        self.fld_text.setText(trigger.get("text", ""))
        self.fld_poll.setValue(int(trigger.get("poll_interval_ms", 500)))
        self.fld_confirm.setValue(max(1, int(trigger.get("confirm_frames", 2))))
        self.fld_cooldown.setValue(max(0, int(trigger.get("cooldown_ms", 1000))))
        self._set_region(trigger.get("region", DEFAULT_REGION), mark_dirty=False)
        # Macro
        macro_file = step.get("macro") or ""
        midx = self.fld_macro.findData(macro_file)
        self.fld_macro.setCurrentIndex(max(0, midx))
        self._ignoring_form = False

        self.unsupported_label.setVisible(not is_supported)
        self._set_fields_readonly(not is_supported)
        self._update_preview_foreground_warning()

    def _commit_current_step(self):
        """Flush current form values back to _draft[_current_step_index].
        No-op for unsupported trigger types (preserves original data)."""
        idx   = self._current_step_index
        steps = (self._draft or {}).get("steps", [])
        if idx < 0 or idx >= len(steps):
            return
        step    = steps[idx]
        trigger = step.setdefault("trigger", {})
        ttype   = self.fld_type.currentData() or "text"
        if ttype not in SUPPORTED_TRIGGER_TYPES:
            return  # leave original data intact

        old_step_id = step.get("id", "")
        new_step_id = self.fld_step_id.text().strip()
        step["id"]                     = new_step_id
        trigger["type"]                = ttype
        trigger["event"]               = self.fld_event.currentData() or "appear"
        trigger["text"]                = self.fld_text.text().strip()
        trigger["poll_interval_ms"]    = self.fld_poll.value()
        trigger["confirm_frames"]      = self.fld_confirm.value()
        trigger["cooldown_ms"]         = self.fld_cooldown.value()
        trigger["region"]              = self._normalize_region(self._current_region)
        normalized_trigger = normalize_trigger_payload(
            trigger, legacy_event_authoritative=True
        )
        trigger.clear()
        trigger.update(normalized_trigger)
        macro_file = self.fld_macro.currentData()
        step["macro"] = macro_file or None

        item = self.step_list.item(idx)
        if item:
            item.setText(step["id"] or "(unnamed)")
        if old_step_id != new_step_id:
            self._refresh_restart_step_options(old_step_id=old_step_id, new_step_id=new_step_id)

    def _update_step_buttons(self):
        steps = (self._draft or {}).get("steps", [])
        count = len(steps)
        row   = self._current_step_index
        self.btn_del.setEnabled(row >= 0 and count > 0)
        self.btn_dup.setEnabled(row >= 0)
        self.btn_up.setEnabled(row > 0)
        self.btn_dn.setEnabled(row >= 0 and row < count - 1)

    def _load_loop_settings(self):
        loop = (self._draft or {}).get("loop") or {}
        mode = loop.get("mode")
        restart_step = loop.get("restart_step")
        max_cycles = loop.get("max_cycles", 1)
        stop_trigger_ref = loop.get("stop_trigger_ref")
        # If no explicit mode but a stop_trigger_ref is present (legacy one-shot
        # with stop pattern), treat as stop_trigger mode in the editor.
        if mode is None and stop_trigger_ref:
            mode = "stop_trigger"
        elif mode is None:
            mode = "manual_stop"
        self._ignoring_form = True
        self.loop_enable_chk.setChecked(bool(loop))
        mode_idx = self.loop_mode_combo.findData(mode)
        self.loop_mode_combo.setCurrentIndex(max(0, mode_idx))
        self.loop_max_cycles.setValue(max(1, self._int_or_default(max_cycles, 1)))
        self._refresh_restart_step_options(selected=restart_step)
        if stop_trigger_ref:
            idx = self.stop_ref_combo.findData(stop_trigger_ref)
            self.stop_ref_combo.setCurrentIndex(max(0, idx))
        else:
            self.stop_ref_combo.setCurrentIndex(0)
        self._ignoring_form = False
        self._update_loop_visibility()

    def _refresh_restart_step_options(self, selected=None, old_step_id=None, new_step_id=None):
        steps = (self._draft or {}).get("steps", [])
        step_ids = [str(step.get("id", "")).strip() for step in steps if str(step.get("id", "")).strip()]
        if selected is None:
            selected = self.loop_restart_combo.currentData()
        if old_step_id and new_step_id and selected == old_step_id:
            selected = new_step_id
        self.loop_restart_combo.blockSignals(True)
        self.loop_restart_combo.clear()
        for step_id in step_ids:
            self.loop_restart_combo.addItem(step_id, step_id)
        if step_ids:
            if selected in step_ids:
                self.loop_restart_combo.setCurrentIndex(step_ids.index(selected))
            else:
                self.loop_restart_combo.setCurrentIndex(0)
                selected = step_ids[0]
        self.loop_restart_combo.blockSignals(False)
        if old_step_id and new_step_id:
            loop = (self._draft or {}).get("loop")
            if isinstance(loop, dict) and loop.get("restart_step") == old_step_id:
                loop["restart_step"] = selected
                self._mark_dirty()

    def _is_stop_trigger_mode(self):
        return self.loop_enable_chk.isChecked() and self.loop_mode_combo.currentData() == "stop_trigger"

    def _on_loop_controls_changed(self, *_):
        if self._ignoring_form:
            return
        self.loop_mode_combo.setEnabled(self.loop_enable_chk.isChecked())
        self.loop_restart_combo.setEnabled(self.loop_enable_chk.isChecked())
        self.loop_max_cycles.setEnabled(
            self.loop_enable_chk.isChecked() and self.loop_mode_combo.currentData() == "max_cycles"
        )
        self._update_loop_visibility()
        self._mark_dirty()

    def _commit_loop_settings(self):
        if not self._draft:
            return
        if not self.loop_enable_chk.isChecked():
            self._draft.pop("loop", None)
            return
        mode = self.loop_mode_combo.currentData() or "manual_stop"
        restart_step = self.loop_restart_combo.currentData()
        loop = {
            "mode": mode,
            "restart_step": restart_step,
        }
        if mode == "max_cycles":
            loop["max_cycles"] = self.loop_max_cycles.value()
        if mode == "stop_trigger":
            ref = self.stop_ref_combo.currentData()
            if ref:
                loop["stop_trigger_ref"] = ref
        self._draft["loop"] = loop

    def _on_add_step(self):
        steps  = self._draft.setdefault("steps", [])
        new_id = f"step_{len(steps) + 1}"
        steps.append(self._default_step(new_id))
        self._rebuild_step_list()
        self.step_list.setCurrentRow(len(steps) - 1)
        self._mark_dirty()

    def _on_del_step(self):
        row   = self._current_step_index
        steps = (self._draft or {}).get("steps", [])
        if row < 0 or row >= len(steps):
            return
        label = steps[row].get("id", f"Step {row + 1}")
        ans   = QMessageBox.question(self, "Delete Step",
            f"Delete step '{label}'?", QMessageBox.Yes | QMessageBox.No)
        if ans != QMessageBox.Yes:
            return
        removed_step_id = (steps[row].get("id") or "").strip()
        steps.pop(row)
        loop = (self._draft or {}).get("loop")
        if isinstance(loop, dict) and loop.get("restart_step") == removed_step_id:
            remaining_ids = [str(step.get("id", "")).strip() for step in steps if str(step.get("id", "")).strip()]
            loop["restart_step"] = remaining_ids[0] if remaining_ids else ""
        self._current_step_index = -1
        self._rebuild_step_list()
        new_row = min(row, len(steps) - 1)
        if new_row >= 0:
            self.step_list.setCurrentRow(new_row)
        else:
            self._set_form_enabled(False)
        self._mark_dirty()

    def _on_dup_step(self):
        row   = self._current_step_index
        steps = (self._draft or {}).get("steps", [])
        if row < 0 or row >= len(steps):
            return
        self._commit_current_step()
        dup     = copy.deepcopy(steps[row])
        dup["id"] = dup.get("id", "step") + "_copy"
        steps.insert(row + 1, dup)
        self._rebuild_step_list()
        self.step_list.setCurrentRow(row + 1)
        self._mark_dirty()

    def _on_move_up(self):
        row = self._current_step_index
        if row <= 0:
            return
        self._commit_current_step()
        steps = self._draft["steps"]
        steps[row - 1], steps[row] = steps[row], steps[row - 1]
        self._current_step_index = -1
        self._rebuild_step_list()
        self.step_list.setCurrentRow(row - 1)
        self._mark_dirty()

    def _on_move_down(self):
        row   = self._current_step_index
        steps = (self._draft or {}).get("steps", [])
        if row < 0 or row >= len(steps) - 1:
            return
        self._commit_current_step()
        steps[row], steps[row + 1] = steps[row + 1], steps[row]
        self._current_step_index = -1
        self._rebuild_step_list()
        self.step_list.setCurrentRow(row + 1)
        self._mark_dirty()

    def _current_step_supported_text_trigger(self):
        steps = (self._draft or {}).get("steps", [])
        idx = self._current_step_index
        if idx < 0 or idx >= len(steps):
            return False
        step = steps[idx]
        trigger = step.get("trigger") or {}
        return (trigger.get("type", "text") in SUPPORTED_TRIGGER_TYPES)

    def _validate_target_window(self):
        if self.window_tracker is None:
            raise RuntimeError("Window tracker is not available in editor.")
        if self.window_tracker.target is None:
            raise RuntimeError(
                "No target window is locked.\nLock a target window before selecting an OCR region."
            )
        target = self.window_tracker.refresh()
        if target.client_width <= 0 or target.client_height <= 0:
            raise RuntimeError("Target window has invalid client dimensions.")
        return target

    def _update_preview_foreground_warning(self):
        if self.window_tracker is None or self.window_tracker.target is None:
            self.preview_foreground_warn.setText("")
            return
        try:
            import ctypes

            fg = ctypes.windll.user32.GetForegroundWindow()
            if fg != self.window_tracker.target.hwnd:
                self.preview_foreground_warn.setText(
                    "The target window is not currently in the foreground.\n"
                    "Preview may contain pixels from another window."
                )
            else:
                self.preview_foreground_warn.setText("")
        except Exception:
            self.preview_foreground_warn.setText("")

    def _on_set_full_window_region(self):
        if not self._current_step_supported_text_trigger():
            return
        self._set_region(DEFAULT_REGION, mark_dirty=True)
        self._set_status("Region set to full window.")

    def _on_select_region(self):
        if not self._current_step_supported_text_trigger():
            return
        self._select_region_for()

    def _select_region_for(self):
        if self.text_detector is None:
            QMessageBox.warning(self, "OCR unavailable", "TextDetector is not available.")
            return
        try:
            target = self._validate_target_window()
            full_image = self.text_detector.capture_region(DEFAULT_REGION)
            pixmap = pil_image_to_qpixmap(full_image)
            dialog = OCRRegionSelectorDialog(pixmap, min_width_px=20, min_height_px=12, parent=self)
            dialog.setGeometry(target.client_left, target.client_top, target.client_width, target.client_height)
            accepted = dialog.exec() == dialog.Accepted
            if not accepted:
                return
            region = dialog.selected_region()
            rect = dialog.selected_pixel_rect()
            if rect["width"] < 20 or rect["height"] < 12:
                QMessageBox.warning(self, "Region too small", "The selected OCR region is too small.")
                return
            self._set_region(region, mark_dirty=True)
            self._set_status(
                f"Region selected: {rect['width']}x{rect['height']} px "
                f"({region['x_ratio']:.4f}, {region['y_ratio']:.4f}, {region['width_ratio']:.4f}, {region['height_ratio']:.4f})"
            )
        except Exception as exc:
            QMessageBox.warning(self, "Select Region failed", str(exc))

    def _set_preview_busy(self, busy):
        self.btn_preview_ocr.setEnabled(not busy and self._current_step_supported_text_trigger())
        self.btn_select_region.setEnabled(not busy and self._current_step_supported_text_trigger())
        self.btn_full_region.setEnabled(not busy and self._current_step_supported_text_trigger())
        if busy:
            self.preview_status.setText("Status: Processing...")

    def _cleanup_preview_thread(self, wait_ms=0):
        thread = self._preview_thread
        if thread is None:
            return
        if thread.isRunning():
            thread.quit()
            if wait_ms > 0:
                thread.wait(wait_ms)
        self._preview_thread = None
        self._preview_worker = None

    def _on_preview_ocr(self):
        if not self._current_step_supported_text_trigger():
            return
        self._start_preview_for_region()

    def _start_preview_for_region(self):
        if self._preview_thread is not None:
            return
        if self.is_workflow_running():
            QMessageBox.information(
                self,
                "Preview unavailable",
                "OCR Preview is unavailable while a workflow is running.",
            )
            return
        if self.text_detector is None:
            QMessageBox.warning(self, "OCR unavailable", "TextDetector is not available.")
            return
        try:
            self._validate_target_window()
        except Exception as exc:
            QMessageBox.warning(self, "Preview unavailable", str(exc))
            return

        region = self._normalize_region(self._current_region)
        self._set_preview_busy(True)
        self._update_preview_foreground_warning()

        self._preview_thread = QThread(self)
        self._preview_worker = OCRPreviewWorker(self.text_detector, region)
        self._preview_worker.moveToThread(self._preview_thread)
        self._preview_thread.started.connect(self._preview_worker.run)
        self._preview_worker.completed.connect(self._on_preview_completed)
        self._preview_worker.failed.connect(self._on_preview_failed)
        self._preview_worker.completed.connect(self._preview_thread.quit)
        self._preview_worker.failed.connect(self._preview_thread.quit)
        self._preview_thread.finished.connect(self._on_preview_thread_finished)
        self._preview_thread.start()

    def _on_preview_completed(self, payload):
        qimage = payload.get("image")
        if qimage is not None:
            pixmap = QPixmap.fromImage(qimage)
            scaled = pixmap.scaled(
                self.preview_image_label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            self.preview_image_label.setPixmap(scaled)
        text = payload.get("text", "")
        alnum_count = sum(1 for ch in text if ch.isalnum())
        has_meaningful_text = bool(text) and alnum_count >= 3
        if has_meaningful_text:
            self.preview_text.setPlainText(text)
            self.preview_status.setText(
                f"Status: Success ({payload.get('elapsed_ms', 0)} ms, "
                f"{payload.get('image_width')}x{payload.get('image_height')})"
            )
        else:
            self.preview_text.setPlainText("")
            self.preview_status.setText(
                f"Status: No text detected ({payload.get('elapsed_ms', 0)} ms, "
                f"{payload.get('image_width')}x{payload.get('image_height')})"
            )
        self.preview_time.setText(f"Timestamp: {payload.get('timestamp', 'N/A')}")

    def _on_preview_failed(self, message):
        self.preview_status.setText("Status: Failed")
        self.preview_time.setText("Timestamp: N/A")
        QMessageBox.warning(self, "Preview OCR failed", message)

    def _on_preview_thread_finished(self):
        self._cleanup_preview_thread()
        self._set_preview_busy(False)

    def _copy_preview_text(self):
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.preview_text.toPlainText())

    def _copy_region_text(self):
        region = self._normalize_region(self._current_region)
        text = (
            "{"
            f"\"x_ratio\": {region['x_ratio']:.6f}, "
            f"\"y_ratio\": {region['y_ratio']:.6f}, "
            f"\"width_ratio\": {region['width_ratio']:.6f}, "
            f"\"height_ratio\": {region['height_ratio']:.6f}"
            "}"
        )
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)

    # ─── File operations ─────────────────────────────────────────────────────

    def _on_new(self):
        if not self._confirm_discard():
            return
        self._new_workflow()

    def _on_open(self):
        if not self._confirm_discard():
            return
        items = self.workflow_store.list_workflows()
        if not items:
            QMessageBox.information(self, "No Workflows",
                "No workflows found in the workflows/ folder.")
            return
        choices = [f"{item['name']}  [{item['filename']}]" for item in items]
        text, ok = QInputDialog.getItem(
            self, "Open Workflow", "Select a workflow:", choices, 0, False)
        if not ok:
            return
        idx      = choices.index(text)
        filename = items[idx]["filename"]
        try:
            data = self.workflow_store.load_workflow(filename)
            self._load_draft(data, filename)
            self._set_status(f"Opened: {filename}")
        except Exception as exc:
            QMessageBox.critical(self, "Load Error",
                f"Could not load workflow:\n{exc}")

    def _on_save(self):
        self._commit_current_step()
        self._commit_loop_settings()
        self._draft["name"] = self.wf_name_edit.text().strip()
        err = self._validate()
        if err:
            self.error_label.setText(err)
            return
        self.error_label.setText("")
        if self._current_filename is None:
            self._on_save_as()
            return
        if self._current_filename == self.get_running_filename():
            QMessageBox.warning(self, "Workflow Running",
                "This workflow is currently running.\n"
                "Stop it before saving changes.")
            return
        try:
            self.workflow_store.save_workflow(self._draft, self._current_filename)
            self._dirty = False
            self._update_title()
            self._set_status(f"Saved: {self._current_filename}")
            self.workflow_saved.emit(self._current_filename)
            self.logger.info("Workflow saved: %s", self._current_filename)
        except Exception as exc:
            self.logger.exception("Workflow save failed")
            self._set_status(f"Save failed: {exc}", error=True)
            QMessageBox.critical(self, "Save Error",
                f"Could not save workflow:\n{exc}")

    def _on_save_as(self):
        self._commit_current_step()
        self._commit_loop_settings()
        self._draft["name"] = self.wf_name_edit.text().strip()
        err = self._validate()
        if err:
            self.error_label.setText(err)
            return
        self.error_label.setText("")
        suggested = re.sub(r"[^a-zA-Z0-9_\- ]", "", self._draft["name"]
                           ).strip().replace(" ", "_") or "workflow"
        text, ok = QInputDialog.getText(
            self, "Save As", "Filename (without .json):", text=suggested)
        if not ok or not text.strip():
            return
        safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", text.strip())
        if not safe:
            QMessageBox.warning(self, "Invalid Filename",
                "The filename contains no valid characters.")
            return
        filename = f"{safe}.json"
        path     = self.workflow_store.workflows_dir / filename
        if path.exists():
            ans = QMessageBox.question(self, "File Exists",
                f"'{filename}' already exists. Overwrite?",
                QMessageBox.Yes | QMessageBox.No)
            if ans != QMessageBox.Yes:
                return
        if filename == self.get_running_filename():
            QMessageBox.warning(self, "Workflow Running",
                "This workflow is currently running.\n"
                "Stop it before overwriting.")
            return
        try:
            self.workflow_store.save_workflow(self._draft, filename)
            self._current_filename = filename
            self._dirty = False
            self._update_title()
            self._set_status(f"Saved as: {filename}")
            self.workflow_saved.emit(filename)
            self.logger.info("Workflow saved as: %s", filename)
        except Exception as exc:
            self.logger.exception("Workflow save-as failed")
            self._set_status(f"Save failed: {exc}", error=True)
            QMessageBox.critical(self, "Save Error",
                f"Could not save workflow:\n{exc}")

    # ─── Validation ──────────────────────────────────────────────────────────

    def _validate(self):
        """Return an error string describing the first validation failure, or None."""
        if not self._draft:
            return "No workflow loaded."
        name = self._draft.get("name", "").strip()
        if not name:
            return "Workflow Name cannot be empty."
        steps = self._draft.get("steps", [])
        if not steps:
            return "Workflow must have at least one Step."
        seen_ids: dict[str, int] = {}
        for i, step in enumerate(steps):
            n   = i + 1
            sid = (step.get("id") or "").strip()
            if not sid:
                return f"Step {n}: Step ID cannot be empty."
            if not VALID_ID_RE.match(sid):
                return (f"Step {n}: Step ID '{sid}' has invalid characters. "
                        "Use letters, digits, underscore, or hyphen only.")
            if sid in seen_ids:
                return (f"Step {n}: Step ID '{sid}' is already used by "
                        f"Step {seen_ids[sid] + 1}.")
            seen_ids[sid] = i
            trigger = step.get("trigger") or {}
            ttype   = trigger.get("type", "")
            if ttype not in SUPPORTED_TRIGGER_TYPES:
                continue  # unsupported steps bypass field validation
            event = trigger.get("event", "")
            if event not in ("appear", "disappear"):
                return f"Step {n}: Trigger Event must be 'appear' or 'disappear'."
            if not (trigger.get("text") or "").strip():
                return f"Step {n}: Trigger Text cannot be empty."
            poll = trigger.get("poll_interval_ms")
            if not isinstance(poll, int) or poll < 100:
                return f"Step {n}: Poll Interval must be an integer >= 100 ms."
            confirm = trigger.get("confirm_frames")
            if not isinstance(confirm, int) or confirm < 1:
                return f"Step {n}: Confirm Frames must be an integer >= 1."
            cooldown = trigger.get("cooldown_ms")
            if not isinstance(cooldown, int) or cooldown < 0:
                return f"Step {n}: Cooldown must be an integer >= 0 ms."
            region = trigger.get("region")
            if not isinstance(region, dict):
                return f"Step {n}: OCR region is missing."
            for key in ("x_ratio", "y_ratio", "width_ratio", "height_ratio"):
                value = region.get(key)
                if not isinstance(value, (int, float)):
                    return f"Step {n}: OCR region '{key}' must be numeric."
            norm = self._normalize_region(region)
            if norm["width_ratio"] <= 0 or norm["height_ratio"] <= 0:
                return f"Step {n}: OCR region width/height must be > 0."
            macro = step.get("macro")
            if not macro:
                return f"Step {n}: A Macro must be selected."
            macro_path = Path(self.workflow_store.root_path) / "scripts" / macro
            if not macro_path.exists():
                return f"Step {n}: Macro file '{macro}' was not found in scripts/."
        loop = self._draft.get("loop")
        if loop is not None:
            if not isinstance(loop, dict):
                return "Loop must be an object."
            mode = loop.get("mode")
            if mode not in ("manual_stop", "max_cycles", "stop_trigger"):
                return "Loop mode must be manual_stop, max_cycles, or stop_trigger."
            restart_step = (loop.get("restart_step") or "").strip()
            if not restart_step:
                return "Loop restart step cannot be empty."
            if restart_step not in seen_ids:
                return f"Loop restart step does not exist: {restart_step}"
            if mode == "max_cycles":
                max_cycles = loop.get("max_cycles")
                if not isinstance(max_cycles, int) or max_cycles < 1:
                    return "Loop max cycles must be an integer >= 1."
            if mode == "stop_trigger":
                stop_ref = loop.get("stop_trigger_ref")
                if not isinstance(stop_ref, str) or not stop_ref.strip():
                    return "請選擇停止條件。"
        return None

    # ─── Discard guard ───────────────────────────────────────────────────────

    def _confirm_discard(self):
        """Returns True if safe to proceed (no dirty state, or user confirmed)."""
        if not self._dirty:
            return True
        ans = QMessageBox.question(
            self, "Unsaved Changes",
            "You have unsaved changes. What would you like to do?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
        )
        if ans == QMessageBox.Save:
            self._on_save()
            return not self._dirty   # True only if save succeeded
        if ans == QMessageBox.Discard:
            return True
        return False   # Cancel

    def closeEvent(self, event):
        self._cleanup_preview_thread(wait_ms=5000)
        if self._dirty:
            ans = QMessageBox.question(
                self, "Unsaved Changes",
                "Save changes before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            )
            if ans == QMessageBox.Save:
                self._on_save()
                if self._dirty:      # save failed or was cancelled
                    event.ignore()
                    return
            elif ans == QMessageBox.Cancel:
                event.ignore()
                return
        event.accept()
