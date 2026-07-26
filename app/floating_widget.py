from pathlib import Path
from PySide6.QtCore import Qt, QPoint, Signal, QUrl
from PySide6.QtGui import QAction, QCursor, QColor
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QMenu,
    QSizePolicy,
    QGraphicsDropShadowEffect,
)
from PySide6.QtGui import QDesktopServices

STATE_COLORS = {
    "IDLE": "#808080",
    "COUNTDOWN": "#f0c040",
    "RUNNING": "#40a040",
    "PAUSED": "#f08020",
    "ERROR": "#c02020",
    "RECORDING": "#c02020",
}


class FloatingWidget(QWidget):
    script_selected = Signal(str)
    start_recording = Signal()
    stop_recording = Signal()
    save_recording = Signal()
    open_scripts_folder = Signal()
    exit_requested = Signal()
    background_opacity_changed = Signal(float)
    workflow_selected = Signal(str)
    start_workflow = Signal()
    stop_workflow = Signal()
    refresh_workflows = Signal()
    export_runtime_log = Signal()
    open_runtime_log_folder = Signal()
    open_workflow_editor = Signal()
    open_runtime_debug_panel = Signal()
    open_trigger_manager = Signal()
    open_macro_manager = Signal()
    open_workflow_manager = Signal()

    def __init__(self, root_path):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlag(Qt.WindowDoesNotAcceptFocus)
        self.setFocusPolicy(Qt.NoFocus)
        self.root_path = Path(root_path)
        self.drag_position = None
        self.drag_started = False
        self.expanded = False
        self.runtime_debug_expanded = False
        self._setup_ui()
        self.resize(420, 90)
        self._set_window_style(0.35)

    def _setup_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(6, 6, 6, 6)
        self.main_layout.setSpacing(4)

        self.header = QHBoxLayout()
        self.state_badge = QLabel()
        self.state_badge.setFixedSize(16, 16)
        self.state_badge.setStyleSheet("border-radius: 8px; background: #808080;")
        self.status_label = QLabel("ScreenBot")
        self.status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.header.addWidget(self.state_badge)
        self.header.addWidget(self.status_label)
        self.main_layout.addLayout(self.header)

        self.primary_actions = QHBoxLayout()
        self.trigger_action_button = QPushButton("偵測觸發點")
        self.macro_action_button = QPushButton("巨集管理")
        self.workflow_action_button = QPushButton("工作流")
        for button in (self.trigger_action_button, self.macro_action_button, self.workflow_action_button):
            button.setFocusPolicy(Qt.NoFocus)
            button.setFixedHeight(28)
            self.primary_actions.addWidget(button)
        self.main_layout.addLayout(self.primary_actions)

        self.details_widget = QWidget()
        self.details_layout = QVBoxLayout(self.details_widget)
        self.details_layout.setContentsMargins(8, 4, 8, 6)
        self.details_layout.setSpacing(3)

        self.script_name_label = QLabel("巨集： (未選擇)")
        self.target_label = QLabel("目標： (未鎖定)")
        self.state_text_label = QLabel("狀態： IDLE")
        # Event count label for recorder MVP
        self.event_count_label = QLabel("尚未錄製任何操作")
        self._compact_status_labels = (self.script_name_label, self.target_label, self.state_text_label, self.event_count_label)
        for label in self._compact_status_labels:
            label.setMinimumHeight(24)
            label.setMaximumHeight(30)
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.details_layout.addWidget(label)

        self.macro_section_label = QLabel("巨集：")
        self.details_layout.addWidget(self.macro_section_label)
        self.combo = QComboBox()
        self.combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.combo.setFocusPolicy(Qt.NoFocus)
        self.details_layout.addWidget(self.combo)

        self.workflow_section_label = QLabel("工作流：")
        self.details_layout.addWidget(self.workflow_section_label)
        self.workflow_combo = QComboBox()
        self.workflow_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.workflow_combo.setFocusPolicy(Qt.NoFocus)
        self.details_layout.addWidget(self.workflow_combo)

        self.workflow_execute_label = QLabel("執行工作流")
        self.details_layout.addWidget(self.workflow_execute_label)
        workflow_button_row = QHBoxLayout()
        self.start_workflow_button = QPushButton("開始")
        self.stop_workflow_button = QPushButton("停止")
        self.refresh_workflow_button = QPushButton("重新整理")
        self.export_runtime_log_button = QPushButton("匯出 Log")
        self.open_runtime_log_folder_button = QPushButton("Run Logs")
        for button in (self.start_workflow_button, self.stop_workflow_button, self.refresh_workflow_button, self.export_runtime_log_button, self.open_runtime_log_folder_button):
            button.setFocusPolicy(Qt.NoFocus)
            button.setFixedHeight(26)
        workflow_button_row.addWidget(self.start_workflow_button)
        workflow_button_row.addWidget(self.stop_workflow_button)
        workflow_button_row.addWidget(self.refresh_workflow_button)
        workflow_button_row.addWidget(self.export_runtime_log_button)
        workflow_button_row.addWidget(self.open_runtime_log_folder_button)
        self.details_layout.addLayout(workflow_button_row)

        self.workflow_status_label = QLabel("Workflow 狀態：IDLE")
        self.workflow_name_label = QLabel("Workflow：(未選擇)")
        self.workflow_step_label = QLabel("步驟：0 / 0")
        self.workflow_step_id_label = QLabel("Step ID：(無)")
        self.workflow_text_label = QLabel("監測文字：(無)")
        self.workflow_event_label = QLabel("觸發事件：(無)")
        self.workflow_macro_label = QLabel("Macro：(無)")
        self.workflow_ocr_label = QLabel("最近 OCR：（空）")
        self.workflow_ocr_last_success_label = QLabel("OCR Last Success：(無)")
        self.workflow_text_state_label = QLabel("文字狀態：(未知)")
        self.workflow_poll_label = QLabel("OCR Poll：0")
        self.workflow_ocr_success_label = QLabel("OCR Consecutive Success：0")
        self.workflow_ocr_failure_label = QLabel("OCR Consecutive Failure：0")
        self.workflow_trigger_state_label = QLabel("Trigger State：(未知)")
        self.workflow_confirm_label = QLabel("Confirm：0 / 0")
        self.workflow_cooldown_label = QLabel("Cooldown：0 ms")
        self.workflow_last_trigger_time_label = QLabel("Last Trigger：(無)")
        self.workflow_macro_state_label = QLabel("Macro State：IDLE")
        self.workflow_macro_duration_label = QLabel("Macro Duration：0 ms")
        self.workflow_elapsed_label = QLabel("Elapsed：00:00")
        self.workflow_poll_rate_label = QLabel("OCR Poll Rate：0.00 Hz")
        self.workflow_trigger_count_label = QLabel("Trigger Count：0")
        self.workflow_macro_count_label = QLabel("Macro Count：0")
        self.workflow_timeline_label = QLabel("Timeline：\n(無)")
        self.workflow_timeline_label.setWordWrap(True)
        self.runtime_debug_toggle = QPushButton("▶ Runtime Debug")
        self.runtime_debug_toggle.setFocusPolicy(Qt.NoFocus)
        self.details_layout.addWidget(self.runtime_debug_toggle)
        self._runtime_debug_widgets = []
        self.details_layout.addWidget(self.workflow_name_label)
        self.details_layout.addWidget(self.workflow_status_label)
        self.details_layout.addWidget(self.workflow_step_label)
        self.details_layout.addWidget(self.workflow_step_id_label)
        self.details_layout.addWidget(self.workflow_text_label)
        self.details_layout.addWidget(self.workflow_event_label)
        self.details_layout.addWidget(self.workflow_macro_label)
        self.details_layout.addWidget(self.workflow_ocr_label)
        self.details_layout.addWidget(self.workflow_ocr_last_success_label)
        self.details_layout.addWidget(self.workflow_text_state_label)
        self.details_layout.addWidget(self.workflow_poll_label)
        self.details_layout.addWidget(self.workflow_ocr_success_label)
        self.details_layout.addWidget(self.workflow_ocr_failure_label)
        self.details_layout.addWidget(self.workflow_trigger_state_label)
        self.details_layout.addWidget(self.workflow_confirm_label)
        self.details_layout.addWidget(self.workflow_cooldown_label)
        self.details_layout.addWidget(self.workflow_last_trigger_time_label)
        self.details_layout.addWidget(self.workflow_macro_state_label)
        self.details_layout.addWidget(self.workflow_macro_duration_label)
        self.details_layout.addWidget(self.workflow_elapsed_label)
        self.details_layout.addWidget(self.workflow_poll_rate_label)
        self.details_layout.addWidget(self.workflow_trigger_count_label)
        self.details_layout.addWidget(self.workflow_macro_count_label)
        self.details_layout.addWidget(self.workflow_timeline_label)
        self._runtime_debug_widgets.extend([
            self.workflow_name_label, self.workflow_status_label, self.workflow_step_label,
            self.workflow_step_id_label, self.workflow_text_label, self.workflow_event_label,
            self.workflow_macro_label, self.workflow_ocr_label, self.workflow_ocr_last_success_label,
            self.workflow_text_state_label, self.workflow_poll_label, self.workflow_ocr_success_label,
            self.workflow_ocr_failure_label, self.workflow_trigger_state_label, self.workflow_confirm_label,
            self.workflow_cooldown_label, self.workflow_last_trigger_time_label, self.workflow_macro_state_label,
            self.workflow_macro_duration_label, self.workflow_elapsed_label, self.workflow_poll_rate_label,
            self.workflow_trigger_count_label, self.workflow_macro_count_label, self.workflow_timeline_label,
        ])
        for item in self._runtime_debug_widgets:
            item.setVisible(False)

        opacity_layout = QHBoxLayout()
        self.opacity_label = QLabel("背景透明度：")
        self.opacity_combo = QComboBox()
        self.opacity_combo.setFocusPolicy(Qt.NoFocus)
        self.opacity_combo.setFixedHeight(24)
        self.opacity_combo.addItem("20%", 0.2)
        self.opacity_combo.addItem("35%", 0.35)
        self.opacity_combo.addItem("50%", 0.5)
        self.opacity_combo.addItem("70%", 0.7)
        opacity_layout.addWidget(self.opacity_label)
        opacity_layout.addWidget(self.opacity_combo)
        self.details_layout.addLayout(opacity_layout)

        # Recording controls live in MacroManager; retain the legacy signals
        # for callers but do not duplicate the controls in the main panel.

        button_row3 = QHBoxLayout()
        self.workflow_editor_button = QPushButton("Workflow Editor")
        self.workflow_editor_button.setFocusPolicy(Qt.NoFocus)
        self.workflow_editor_button.setFixedHeight(26)
        button_row3.addWidget(self.workflow_editor_button)
        self.workflow_debug_button = QPushButton("Runtime Debug")
        self.workflow_debug_button.setFocusPolicy(Qt.NoFocus)
        self.workflow_debug_button.setFixedHeight(26)
        button_row3.addWidget(self.workflow_debug_button)
        button_row3.addStretch()
        self.details_layout.addLayout(button_row3)

        self.main_layout.addWidget(self.details_widget)
        self.details_widget.setVisible(False)

        self.combo.currentIndexChanged.connect(self._on_script_selected)
        self.workflow_combo.currentIndexChanged.connect(self._on_workflow_selected)
        self.opacity_combo.currentIndexChanged.connect(self._on_opacity_changed)
        self.start_workflow_button.clicked.connect(lambda: self.start_workflow.emit())
        self.stop_workflow_button.clicked.connect(lambda: self.stop_workflow.emit())
        self.refresh_workflow_button.clicked.connect(lambda: self.refresh_workflows.emit())
        self.export_runtime_log_button.clicked.connect(lambda: self.export_runtime_log.emit())
        self.open_runtime_log_folder_button.clicked.connect(lambda: self.open_runtime_log_folder.emit())
        self.workflow_editor_button.clicked.connect(lambda: self.open_workflow_editor.emit())
        self.workflow_debug_button.clicked.connect(lambda: self.open_runtime_debug_panel.emit())
        self.trigger_action_button.clicked.connect(lambda: self.open_trigger_manager.emit())
        self.macro_action_button.clicked.connect(lambda: self.open_macro_manager.emit())
        self.workflow_action_button.clicked.connect(lambda: self.open_workflow_manager.emit())
        self.runtime_debug_toggle.clicked.connect(self.toggle_runtime_debug)

    def set_status(self, state, countdown_text=None):
        state_name = state.name if hasattr(state, "name") else str(state)
        color = STATE_COLORS.get(state_name, "#808080")
        self.state_badge.setStyleSheet(f"border-radius: 8px; background: {color};")
        countdown = f" ({countdown_text})" if countdown_text else ""
        self.state_text_label.setText(f"狀態： {state_name}{countdown}")
        self.status_label.setText(f"ScreenBot{countdown}")

    def set_script_list(self, scripts):
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem("(選擇腳本)", None)
        for filename, display in scripts:
            self.combo.addItem(display, filename)
        self.combo.blockSignals(False)

    def select_script(self, filename):
        index = self.combo.findData(filename)
        if index >= 0:
            self.combo.setCurrentIndex(index)
        else:
            self.combo.setCurrentIndex(0)

    def set_script_name(self, name):
        self.script_name_label.setText(f"巨集： {name}")

    def set_workflow_list(self, workflows):
        self.workflow_combo.blockSignals(True)
        self.workflow_combo.clear()
        self.workflow_combo.addItem("(選擇 Workflow)", None)
        for item in workflows:
            filename = item.get("filename")
            display = item.get("name") or filename
            self.workflow_combo.addItem(display, filename)
        self.workflow_combo.blockSignals(False)

    def select_workflow(self, filename):
        index = self.workflow_combo.findData(filename)
        if index >= 0:
            self.workflow_combo.setCurrentIndex(index)
        else:
            self.workflow_combo.setCurrentIndex(0)

    def set_workflow_runtime_info(self, info):
        status = info.get("status", "IDLE")
        self.workflow_status_label.setText(f"Workflow 狀態：{status}")
        self.workflow_name_label.setText(f"Workflow：{info.get('workflow_name') or '(未選擇)'}")
        self.workflow_step_label.setText(f"步驟：{info.get('step_number', 0)} / {info.get('total_steps', 0)}")
        self.workflow_step_id_label.setText(f"Step ID：{info.get('step_id') or '(無)'}")
        self.workflow_text_label.setText(f"監測文字：{info.get('trigger_text') or '(無)'}")
        self.workflow_event_label.setText(f"觸發事件：{info.get('trigger_event') or '(無)'}")
        self.workflow_macro_label.setText(f"Macro：{info.get('macro') or '(無)'}")
        ocr_text = info.get("last_ocr_text")
        self.workflow_ocr_label.setText(f"最近 OCR：{ocr_text if ocr_text else '（空）'}")
        self.workflow_ocr_last_success_label.setText(f"OCR Last Success：{info.get('ocr_last_success_time') or '(無)'}")
        self.workflow_text_state_label.setText(f"文字狀態：{info.get('text_state') or '(未知)'}")
        self.workflow_poll_label.setText(f"OCR Poll：{info.get('ocr_poll_count', 0)}")
        self.workflow_ocr_success_label.setText(f"OCR Consecutive Success：{info.get('ocr_success_count', 0)}")
        self.workflow_ocr_failure_label.setText(f"OCR Consecutive Failure：{info.get('ocr_failure_count', 0)}")
        self.workflow_trigger_state_label.setText(f"Trigger State：{info.get('trigger_state') or '(未知)'}")
        self.workflow_confirm_label.setText(f"Confirm：{info.get('confirm_progress') or '0 / 0'}")
        self.workflow_cooldown_label.setText(f"Cooldown：{info.get('cooldown_remaining_ms', 0)} ms")
        self.workflow_last_trigger_time_label.setText(f"Last Trigger：{info.get('last_trigger_time') or '(無)'}")
        macro_state = "RUNNING" if info.get("macro_running") else "IDLE"
        self.workflow_macro_state_label.setText(f"Macro State：{macro_state}")
        self.workflow_macro_duration_label.setText(f"Macro Duration：{info.get('macro_duration_ms', 0)} ms")
        self.workflow_elapsed_label.setText(f"Elapsed：{info.get('elapsed_text', '00:00')}")
        self.workflow_poll_rate_label.setText(f"OCR Poll Rate：{info.get('poll_rate_hz', 0.0):.2f} Hz")
        self.workflow_trigger_count_label.setText(f"Trigger Count：{info.get('trigger_count', 0)}")
        self.workflow_macro_count_label.setText(f"Macro Count：{info.get('macro_count', 0)}")
        timeline_lines = info.get("timeline_lines") or []
        if timeline_lines:
            self.workflow_timeline_label.setText("Timeline：\n" + "\n".join(timeline_lines))
        else:
            self.workflow_timeline_label.setText("Timeline：\n(無)")

    def set_workflow_running(self, running):
        self.workflow_combo.setEnabled(not running)
        self.start_workflow_button.setEnabled(not running)
        self.refresh_workflow_button.setEnabled(not running)
        self.stop_workflow_button.setEnabled(running)

    def set_event_count(self, count: int):
        if count <= 0:
            self.event_count_label.setText("尚未錄製任何操作")
        else:
            self.event_count_label.setText(f"目前錄製事件：{count} Events")

    def set_target_title(self, title):
        self.target_label.setText(f"目標： {title}")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.drag_started = False
            event.accept()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and self.drag_position is not None:
            new_pos = event.globalPosition().toPoint() - self.drag_position
            if not self.drag_started:
                self.drag_started = (new_pos - self.pos()).manhattanLength() > 5
            self.move(new_pos)
            event.accept()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.drag_position is not None:
            if not self.drag_started:
                self.toggle_panel()
            self.drag_position = None
            self.drag_started = False
            event.accept()
        super().mouseReleaseEvent(event)

    def toggle_panel(self):
        self.expanded = not self.expanded
        self.details_widget.setVisible(self.expanded)
        self.setMinimumWidth(420)
        self.setMaximumHeight(16777215)
        self.adjustSize()
        if self.expanded and not self.runtime_debug_expanded:
            self._compact_height = self.height()

    def toggle_runtime_debug(self):
        self.runtime_debug_expanded = not self.runtime_debug_expanded
        for item in self._runtime_debug_widgets:
            item.setVisible(self.runtime_debug_expanded)
        self.runtime_debug_toggle.setText("▼ Runtime Debug" if self.runtime_debug_expanded else "▶ Runtime Debug")
        if self.expanded:
            self.adjustSize()
            if not self.runtime_debug_expanded:
                self.setMinimumHeight(0)
                self.resize(self.width(), getattr(self, "_compact_height", self.height()))

    def _set_window_style(self, opacity):
        alpha = int(round(opacity * 255))
        self.setStyleSheet(
            f"QWidget {{ background: rgba(170,220,255,{opacity}); border-radius: 16px; border: 1px solid rgba(255,255,255,0.45); color: #ffffff; }}"
            "QLabel { color: #ffffff; }"
            "QPushButton { background: rgba(255,255,255,0.14); border: 1px solid rgba(255,255,255,0.25); border-radius: 8px; color: #ffffff; padding: 4px 8px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.24); }"
            "QComboBox { background: rgba(255,255,255,0.16); border: 1px solid rgba(255,255,255,0.28); border-radius: 8px; color: #ffffff; padding: 3px 6px; }"
            "QMenu { background: rgba(32,32,32,0.95); color: #ffffff; }"
        )
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 90))
        self.setGraphicsEffect(shadow)

    def _on_opacity_changed(self, index):
        value = self.opacity_combo.itemData(index)
        if value is not None:
            self._set_window_style(value)
            self.background_opacity_changed.emit(value)

    def set_background_opacity(self, value):
        best_index = 0
        best_diff = None
        for index in range(self.opacity_combo.count()):
            item_value = self.opacity_combo.itemData(index)
            diff = abs(item_value - value)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_index = index
        self.opacity_combo.setCurrentIndex(best_index)
        self._set_window_style(value)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction(QAction("開啟腳本資料夾", self, triggered=lambda: self.open_scripts_folder.emit()))
        menu.addAction(QAction("結束應用程式", self, triggered=lambda: self.exit_requested.emit()))
        menu.exec(QCursor.pos())

    def _on_script_selected(self, index):
        filename = self.combo.itemData(index)
        if filename:
            self.script_selected.emit(filename)

    def _on_workflow_selected(self, index):
        filename = self.workflow_combo.itemData(index)
        if filename:
            self.workflow_selected.emit(filename)
