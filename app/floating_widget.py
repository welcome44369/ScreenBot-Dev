from pathlib import Path
import os

from PySide6.QtCore import QEvent, QTimer, Qt, QPoint, Signal, QUrl
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
from app.start_handoff import StartHandoffState
from app.windows_no_activate import WindowsNoActivateAdapter

STATE_COLORS = {
    "IDLE": "#808080",
    "COUNTDOWN": "#f0c040",
    "RUNNING": "#40a040",
    "PAUSED": "#f08020",
    "ERROR": "#c02020",
    "RECORDING": "#c02020",
}


# Compact status is a presentation concern rather than an AppState.  These
# styles deliberately use an opaque-enough foreground chip so the essential
# status remains legible over a bright game scene even when the panel itself
# is configured as translucent.
COMPACT_STATUS_STYLES = {
    "NO_TARGET": {"badge": "#64748b", "accent": "#94a3b8"},
    "TARGET_READY": {"badge": "#0891b2", "accent": "#22d3ee"},
    "TARGET_INVALID": {"badge": "#d97706", "accent": "#f59e0b"},
    "STARTING": {"badge": "#2563eb", "accent": "#60a5fa"},
    "START_ARMED": {"badge": "#ca8a04", "accent": "#facc15"},
    "WAIT_TRIGGER": {"badge": "#d97706", "accent": "#fbbf24"},
    "RUNNING_MACRO": {"badge": "#16a34a", "accent": "#4ade80"},
    "STOPPING": {"badge": "#ea580c", "accent": "#fb923c"},
    "ERROR": {"badge": "#dc2626", "accent": "#f87171"},
    "RECORDING": {"badge": "#a855f7", "accent": "#c084fc"},
    "COUNTDOWN": {"badge": "#ca8a04", "accent": "#facc15"},
    "STANDALONE_RUNNING": {"badge": "#16a34a", "accent": "#4ade80"},
    "PAUSED": {"badge": "#a16207", "accent": "#fde047"},
    "INPUT_BLOCKED": {"badge": "#dc2626", "accent": "#fb923c"},
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
    compact_hwnd_changed = Signal(int, int)
    compact_geometry_changed = Signal()
    compact_visibility_intent_changed = Signal(bool)
    workflow_execution_panel_visibility_changed = Signal(bool)
    run_ocr_process_probe = Signal()

    def __init__(self, root_path, native_no_activate_adapter=None, overlay_diagnostics=None):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowFlag(Qt.WindowDoesNotAcceptFocus)
        self.setFocusPolicy(Qt.NoFocus)
        self.root_path = Path(root_path)
        self._overlay_diagnostics = overlay_diagnostics
        self._native_hwnd = 0
        self._ui_visibility_intent = True
        self._coordinator_visibility_change = False
        self._target_visibility_suppressed = False
        self._suppress_exit_requests = False
        self._ocr_process_diagnostic_enabled = (
            os.environ.get("SCREENBOT_OCR_PROCESS_DIAGNOSTIC") == "1"
        )
        self.drag_position = None
        self.drag_started = False
        self.expanded = False
        self.runtime_debug_expanded = False
        self._workflow_execution_active = False
        self._workflow_execution_collapsed = False
        self._workflow_previous_expanded = False
        self._workflow_runtime_summary = {}
        self._compact_status_view = None
        self._native_no_activate_adapter = native_no_activate_adapter or WindowsNoActivateAdapter(
            observer=overlay_diagnostics
        )
        self._setup_ui()
        self.resize(420, 90)
        self._set_window_style(0.35)
        self._observe_lifecycle("COMPACT_CONSTRUCTOR_COMPLETED")
        QTimer.singleShot(0, self._apply_native_no_activate)

    def _diagnostics_enabled(self):
        return bool(getattr(self._overlay_diagnostics, "enabled", False))

    def _observe_lifecycle(self, event, **data):
        if self._diagnostics_enabled():
            self._overlay_diagnostics.observe_compact_lifecycle(
                event,
                old_hwnd=self._native_hwnd,
                new_hwnd=self._native_hwnd,
                **data,
            )

    def _apply_native_no_activate(self):
        """Reapply after Qt creates or recreates the compact root HWND."""
        try:
            previous_hwnd = self._native_hwnd
            current_hwnd = int(self.winId())
            self._native_hwnd = current_hwnd
            if self._diagnostics_enabled():
                lifecycle_event = (
                    "COMPACT_WINID_FIRST_ACQUIRED"
                    if not previous_hwnd
                    else "COMPACT_HWND_REACQUIRED"
                )
                self._overlay_diagnostics.observe_compact_lifecycle(
                    lifecycle_event,
                    old_hwnd=previous_hwnd,
                    new_hwnd=current_hwnd,
                )
                self._overlay_diagnostics.observe_compact_lifecycle(
                    "NOACTIVATE_DEFERRED_APPLICATION",
                    old_hwnd=current_hwnd,
                    new_hwnd=current_hwnd,
                )
            applied = self._native_no_activate_adapter.apply(current_hwnd)
            if current_hwnd != previous_hwnd:
                self.compact_hwnd_changed.emit(previous_hwnd, current_hwnd)
            return applied
        except (OSError, RuntimeError, TypeError):
            return False

    def has_native_no_activate(self):
        try:
            return self._native_no_activate_adapter.has_no_activate(int(self.winId()))
        except (OSError, RuntimeError, TypeError):
            return False

    def showEvent(self, event):
        if not self._coordinator_visibility_change:
            self._ui_visibility_intent = True
            self.compact_visibility_intent_changed.emit(True)
        self._observe_lifecycle("COMPACT_SHOW_EVENT_BEFORE")
        super().showEvent(event)
        self._observe_lifecycle("COMPACT_SHOW_EVENT_AFTER")
        QTimer.singleShot(0, self._apply_native_no_activate)

    def event(self, event):
        if event.type() == QEvent.WinIdChange:
            self._observe_lifecycle("COMPACT_WINID_CHANGE_BEFORE")
            QTimer.singleShot(0, self._apply_native_no_activate)
            self._observe_lifecycle("COMPACT_WINID_CHANGE_AFTER")
        return super().event(event)

    def nativeEvent(self, event_type, message):
        diagnostic_payload = None
        if self._diagnostics_enabled():
            diagnostic_payload = self._overlay_diagnostics.observe_native_message(
                event_type, message
            )
        result = self._native_no_activate_adapter.mouse_activate_result(message)
        if self._diagnostics_enabled() and diagnostic_payload is not None:
            QTimer.singleShot(
                0,
                lambda payload=diagnostic_payload, handler_result=result:
                self._overlay_diagnostics.observe_native_message_after(
                    payload, handler_result
                ),
            )
        if result is not None:
            return True, result
        return super().nativeEvent(event_type, message)

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
        self.hotkey_status_label = QLabel("")
        self.hotkey_status_label.setVisible(False)
        self.header.addWidget(self.hotkey_status_label)
        self.main_layout.addLayout(self.header)

        self.workflow_running_strip = QWidget()
        self.workflow_running_strip_layout = QHBoxLayout(self.workflow_running_strip)
        self.workflow_running_strip_layout.setContentsMargins(2, 0, 2, 0)
        self.workflow_running_strip_layout.setSpacing(6)
        self.workflow_running_strip_label = QLabel("工作流執行中")
        self.workflow_running_strip_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.workflow_running_expand_button = QPushButton("展開")
        self.workflow_running_expand_button.setFocusPolicy(Qt.NoFocus)
        self.workflow_running_expand_button.setFixedHeight(24)
        self.workflow_running_expand_button.setToolTip("展開完整工作流監控面板")
        self.workflow_running_stop_button = QPushButton("停止")
        self.workflow_running_stop_button.setFocusPolicy(Qt.NoFocus)
        self.workflow_running_stop_button.setFixedHeight(24)
        self.workflow_running_stop_button.setToolTip("停止目前工作流")
        self.workflow_running_strip_layout.addWidget(self.workflow_running_strip_label, 1)
        self.workflow_running_strip_layout.addWidget(self.workflow_running_expand_button)
        self.workflow_running_strip_layout.addWidget(self.workflow_running_stop_button)
        self.workflow_running_strip.setVisible(False)
        self.main_layout.addWidget(self.workflow_running_strip)

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
        self.workflow_toggle_button = QPushButton("收合")
        self.refresh_workflow_button = QPushButton("重新整理")
        self.export_runtime_log_button = QPushButton("匯出 Log")
        self.open_runtime_log_folder_button = QPushButton("Run Logs")
        for button in (self.start_workflow_button, self.stop_workflow_button, self.workflow_toggle_button, self.refresh_workflow_button, self.export_runtime_log_button, self.open_runtime_log_folder_button):
            button.setFocusPolicy(Qt.NoFocus)
            button.setFixedHeight(26)
        workflow_button_row.addWidget(self.start_workflow_button)
        workflow_button_row.addWidget(self.stop_workflow_button)
        workflow_button_row.addWidget(self.workflow_toggle_button)
        workflow_button_row.addWidget(self.refresh_workflow_button)
        workflow_button_row.addWidget(self.export_runtime_log_button)
        workflow_button_row.addWidget(self.open_runtime_log_folder_button)
        self.details_layout.addLayout(workflow_button_row)
        self.workflow_toggle_button.setVisible(False)
        self.workflow_toggle_button.setToolTip("收合完整工作流監控面板")

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
        self.ocr_process_probe_button = None
        if self._ocr_process_diagnostic_enabled:
            self.ocr_process_probe_button = QPushButton("Run OCR Probe")
            self.ocr_process_probe_button.setFocusPolicy(Qt.NoFocus)
            self.ocr_process_probe_button.setFixedHeight(26)
            button_row3.addWidget(self.ocr_process_probe_button)
        button_row3.addStretch()
        self.details_layout.addLayout(button_row3)

        self.main_layout.addWidget(self.details_widget)
        self.details_widget.setVisible(False)

        self.combo.currentIndexChanged.connect(self._on_script_selected)
        self.workflow_combo.currentIndexChanged.connect(self._on_workflow_selected)
        self.opacity_combo.currentIndexChanged.connect(self._on_opacity_changed)
        self.start_workflow_button.clicked.connect(lambda: self.start_workflow.emit())
        self.stop_workflow_button.clicked.connect(lambda: self.stop_workflow.emit())
        self.workflow_toggle_button.clicked.connect(self.toggle_workflow_execution_panel)
        self.workflow_running_expand_button.clicked.connect(self.toggle_workflow_execution_panel)
        self.workflow_running_stop_button.clicked.connect(lambda: self.stop_workflow.emit())
        self.refresh_workflow_button.clicked.connect(lambda: self.refresh_workflows.emit())
        self.export_runtime_log_button.clicked.connect(lambda: self.export_runtime_log.emit())
        self.open_runtime_log_folder_button.clicked.connect(lambda: self.open_runtime_log_folder.emit())
        self.workflow_editor_button.clicked.connect(lambda: self.open_workflow_editor.emit())
        self.workflow_debug_button.clicked.connect(lambda: self.open_runtime_debug_panel.emit())
        if self.ocr_process_probe_button is not None:
            self.ocr_process_probe_button.clicked.connect(
                lambda: self.run_ocr_process_probe.emit()
            )
        self.trigger_action_button.clicked.connect(lambda: self.open_trigger_manager.emit())
        self.macro_action_button.clicked.connect(lambda: self.open_macro_manager.emit())
        self.workflow_action_button.clicked.connect(lambda: self.open_workflow_manager.emit())
        self.runtime_debug_toggle.clicked.connect(self.toggle_runtime_debug)
        if self._diagnostics_enabled():
            self._install_diagnostic_event_filters()
            self.start_workflow.connect(self._on_diagnostic_start_signal)
            self.stop_workflow.connect(
                lambda: self._overlay_diagnostics.observe_ui_event("STOP_SIGNAL_EMITTED")
            )
            self.refresh_workflows.connect(
                lambda: self._overlay_diagnostics.observe_ui_event("REFRESH_SIGNAL_EMITTED")
            )

    def _install_diagnostic_event_filters(self):
        self._diagnostic_controls = {
            id(self.start_workflow_button): "START",
            id(self.stop_workflow_button): "STOP_CANCEL",
            id(self.refresh_workflow_button): "REFRESH",
            id(self.combo): "SCRIPT_COMBO",
            id(self.workflow_combo): "WORKFLOW_COMBO",
            id(self.runtime_debug_toggle): "RUNTIME_DEBUG_TOGGLE",
        }
        for control in (
            self.start_workflow_button,
            self.stop_workflow_button,
            self.refresh_workflow_button,
            self.combo,
            self.workflow_combo,
            self.runtime_debug_toggle,
        ):
            control.installEventFilter(self)
        self.combo.view().window().installEventFilter(self)
        self.workflow_combo.view().window().installEventFilter(self)

    def eventFilter(self, watched, event):
        if self._diagnostics_enabled():
            control = getattr(self, "_diagnostic_controls", {}).get(id(watched))
            if control == "START" and event.type() == QEvent.MouseButtonPress:
                self._overlay_diagnostics.begin_interaction("START")
                self._overlay_diagnostics.observe_ui_event(
                    "START_UI_MOUSE_PRESS", receiver=watched.metaObject().className()
                )
            elif control == "START" and event.type() == QEvent.MouseButtonRelease:
                self._overlay_diagnostics.observe_ui_event(
                    "START_UI_MOUSE_RELEASE", receiver=watched.metaObject().className()
                )
            elif control == "STOP_CANCEL" and event.type() == QEvent.MouseButtonPress:
                self._overlay_diagnostics.begin_interaction("STOP_CANCEL")
                self._overlay_diagnostics.observe_ui_event(
                    "STOP_CANCEL_UI_MOUSE_PRESS", receiver=watched.metaObject().className()
                )
            elif control == "REFRESH" and event.type() == QEvent.MouseButtonPress:
                self._overlay_diagnostics.begin_interaction("REFRESH")
                self._overlay_diagnostics.observe_ui_event(
                    "REFRESH_UI_MOUSE_PRESS", receiver=watched.metaObject().className()
                )
            elif control in {"SCRIPT_COMBO", "WORKFLOW_COMBO"}:
                if event.type() == QEvent.MouseButtonPress:
                    self._overlay_diagnostics.begin_interaction(control)
                    self._overlay_diagnostics.observe_ui_event(
                        "COMBO_UI_MOUSE_PRESS",
                        receiver=watched.metaObject().className(),
                        combo=control,
                    )
            elif watched in {self.combo.view().window(), self.workflow_combo.view().window()}:
                role = (
                    "SCREENBOT_COMBO_POPUP"
                    if watched is self.combo.view().window()
                    else "SCREENBOT_COMBO_POPUP"
                )
                if event.type() == QEvent.Show:
                    self._overlay_diagnostics.observe_popup(role, watched, "COMBO_POPUP_SHOW")
                elif event.type() == QEvent.Hide:
                    self._overlay_diagnostics.observe_popup(role, watched, "COMBO_POPUP_HIDE")
        return super().eventFilter(watched, event)

    def _on_diagnostic_start_signal(self):
        if self._diagnostics_enabled():
            self._overlay_diagnostics.observe_ui_event("START_SIGNAL_EMITTED")

    def render_compact_status(self, view_model):
        """Render the four always-visible status fields from one view model."""
        view = dict(view_model or {})
        visual_state = view.get("visual_state", "NO_TARGET")
        style = COMPACT_STATUS_STYLES.get(visual_state, COMPACT_STATUS_STYLES["NO_TARGET"])
        badge = style["badge"]
        accent = style["accent"]
        chip_style = (
            "QLabel {"
            "background: rgba(15, 23, 42, 232); color: #f8fafc; "
            f"border: 1px solid {accent}; border-left: 4px solid {accent}; "
            "border-radius: 6px; padding: 3px 7px;"
            "}"
        )
        self.state_badge.setStyleSheet(
            f"border-radius: 8px; background: {badge}; border: 2px solid {accent};"
        )
        self.status_label.setText(f"ScreenBot · {view.get('status_text') or visual_state}")
        self.target_label.setText(view.get("target_text") or "目標：未鎖定")
        self.state_text_label.setText(f"狀態： {view.get('status_text') or visual_state}")
        self.event_count_label.setText(view.get("operation_text") or "請按 F8 鎖定目標視窗")
        for label in (self.target_label, self.state_text_label, self.event_count_label):
            label.setStyleSheet(chip_style)
        self.target_label.setToolTip(view.get("target_full_title") or "")
        self._compact_status_view = view
        self._update_workflow_running_strip_text()

    def set_status(self, state, countdown_text=None):
        """Compatibility wrapper; final compact fields still use one renderer."""
        state_name = state.name if hasattr(state, "name") else str(state)
        visual_state = {
            "RUNNING": "STANDALONE_RUNNING",
            "COUNTDOWN": "COUNTDOWN",
            "PAUSED": "PAUSED",
            "RECORDING": "RECORDING",
            "ERROR": "ERROR",
        }.get(state_name, "NO_TARGET")
        countdown = f" {countdown_text}" if countdown_text else ""
        self.render_compact_status({
            "visual_state": visual_state,
            "status_text": f"{state_name}{countdown}",
            "target_text": (self._compact_status_view or {}).get("target_text", "目標：未鎖定"),
            "operation_text": (self._compact_status_view or {}).get("operation_text", "請按 F8 鎖定目標視窗"),
        })

    def set_hotkey_status(self, message, available):
        self.hotkey_status_label.setText(str(message))
        self.hotkey_status_label.setStyleSheet("color: #16a34a;" if available else "color: #dc2626;")
        self.hotkey_status_label.setVisible(True)

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
        self._workflow_runtime_summary = dict(info or {})
        status = info.get("status", "IDLE")
        workflow_name = info.get("workflow_name") or "(未選擇)"
        self.workflow_section_label.setText(f"工作流： {workflow_name}")
        self.workflow_status_label.setText(f"Workflow 狀態：{status}")
        self.workflow_name_label.setText(f"Workflow：{workflow_name}")
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
        self._update_workflow_running_strip_text()

    def set_workflow_running(self, running):
        self.workflow_combo.setEnabled(not running)
        self.start_workflow_button.setEnabled(not running)
        self.refresh_workflow_button.setEnabled(not running)
        self.stop_workflow_button.setEnabled(running)
        self.workflow_running_stop_button.setEnabled(running)
        self._refresh_workflow_execution_controls()

    def set_ocr_process_probe_state(
        self, running=False, message=None, completed=False
    ):
        button = self.ocr_process_probe_button
        if button is None:
            return
        button.setEnabled(not running and not completed)
        if running:
            button.setText("OCR Probe Running…")
        elif completed:
            button.setText("OCR Probe Complete")
        else:
            button.setText("Run OCR Probe")
        if message:
            button.setToolTip(str(message))

    def set_start_handoff_presentation(self, snapshot):
        """Render the application-owned handoff state without owning its logic."""
        state = snapshot.state
        if state is StartHandoffState.ARMED:
            self.start_workflow_button.setEnabled(False)
            self.stop_workflow_button.setEnabled(True)
            self.stop_workflow_button.setText("取消")
            self.workflow_status_label.setText("Workflow 狀態：等待目標前景")
        elif state is StartHandoffState.COMMITTING:
            self.start_workflow_button.setEnabled(False)
            self.stop_workflow_button.setEnabled(False)
            self.stop_workflow_button.setText("停止")
            self.workflow_status_label.setText("Workflow 狀態：正在確認目標並啟動")
        else:
            self.stop_workflow_button.setText("停止")

    def set_event_count(self, count: int):
        # Kept for external callers.  Application owns the final compact
        # presentation and will render this value only while recording.
        view = dict(self._compact_status_view or {})
        view["recording_event_count"] = max(0, int(count))
        self._compact_status_view = view

    def set_target_title(self, title):
        # Compatibility cache only.  The compact labels themselves are
        # exclusively written by render_compact_status().
        view = dict(self._compact_status_view or {})
        view["target_full_title"] = title
        self._compact_status_view = view
        self._update_workflow_running_strip_text()

    def _update_workflow_running_strip_text(self):
        if not hasattr(self, "workflow_running_strip_label"):
            return
        info = self._workflow_runtime_summary or {}
        workflow_name = info.get("workflow_name") or "(未選擇)"
        status = info.get("status") or "IDLE"
        cycle = info.get("current_cycle") or info.get("completed_cycles") or 0
        step_number = info.get("step_number", 0)
        total_steps = info.get("total_steps", 0)
        step_text = f"{step_number}/{total_steps}" if total_steps else "0/0"
        target_text = (self._compact_status_view or {}).get("target_text") or "目標：未鎖定"
        self.workflow_running_strip_label.setText(
            f"{target_text}｜{workflow_name}｜{status}｜循環 {cycle}｜步驟 {step_text}"
        )

    def _refresh_workflow_execution_controls(self):
        active = bool(self._workflow_execution_active)
        collapsed = bool(self._workflow_execution_collapsed)
        self.workflow_running_strip.setVisible(active and collapsed)
        self.workflow_toggle_button.setVisible(active and not collapsed)
        self.workflow_toggle_button.setEnabled(active and not collapsed)

    def set_workflow_execution_panel_expanded(self, expanded):
        if not self._workflow_execution_active:
            return False
        expanded = bool(expanded)
        self.expanded = expanded
        self._workflow_execution_collapsed = not expanded
        if not expanded:
            self.runtime_debug_expanded = False
            for item in self._runtime_debug_widgets:
                item.setVisible(False)
            self.runtime_debug_toggle.setText("▶ Runtime Debug")
        self.details_widget.setVisible(expanded)
        self.adjustSize()
        self._refresh_workflow_execution_controls()
        self.workflow_execution_panel_visibility_changed.emit(expanded)
        return (not self.details_widget.isHidden()) == expanded

    def toggle_workflow_execution_panel(self):
        if not self._workflow_execution_active:
            return False
        return self.set_workflow_execution_panel_expanded(self._workflow_execution_collapsed)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._diagnostics_enabled():
                self._overlay_diagnostics.begin_interaction("COMPACT_DRAG")
                self._overlay_diagnostics.observe_ui_event(
                    "COMPACT_DRAG_BEGIN", receiver=self.metaObject().className()
                )
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
            if self._diagnostics_enabled():
                self._overlay_diagnostics.observe_ui_event(
                    "COMPACT_DRAG_END",
                    receiver=self.metaObject().className(),
                    dragged=self.drag_started,
                )
            self.drag_position = None
            self.drag_started = False
            event.accept()
        super().mouseReleaseEvent(event)

    def toggle_panel(self):
        if self._workflow_execution_active:
            self.set_workflow_execution_panel_expanded(not self.expanded)
            return
        self.expanded = not self.expanded
        if self._diagnostics_enabled():
            self._overlay_diagnostics.observe_ui_event(
                "COMPACT_EXPAND_TOGGLED", expanded=self.expanded
            )
        self.details_widget.setVisible(self.expanded)
        self.setMinimumWidth(420)
        self.setMaximumHeight(16777215)
        self.adjustSize()
        if self.expanded and not self.runtime_debug_expanded:
            self._compact_height = self.height()

    def collapse_for_workflow_execution(self):
        """Collapse the expanded controls before a workflow can be committed.

        This is intentionally a presentation-only transition: it does not
        activate, move, raise, hide, or otherwise alter any target window.
        """
        if self._workflow_execution_active and self._workflow_execution_collapsed:
            return True
        self._workflow_previous_expanded = bool(self.expanded)
        self._workflow_execution_active = True
        self._workflow_execution_collapsed = True
        self.expanded = False
        self.runtime_debug_expanded = False
        for item in self._runtime_debug_widgets:
            item.setVisible(False)
        self.runtime_debug_toggle.setText("▶ Runtime Debug")
        self.details_widget.setVisible(False)
        self.adjustSize()
        self._refresh_workflow_execution_controls()
        return not self.details_widget.isVisible()

    def restore_after_workflow_execution(self):
        """Restore the pre-start panel presentation exactly once."""
        if not self._workflow_execution_active:
            return True
        self._workflow_execution_active = False
        self._workflow_execution_collapsed = False
        self.expanded = True
        self.details_widget.setVisible(self.expanded)
        self.adjustSize()
        self._refresh_workflow_execution_controls()
        return (not self.details_widget.isHidden()) == self.expanded

    def is_workflow_execution_collapsed(self):
        return (
            self._workflow_execution_active
            and self._workflow_execution_collapsed
            and not self.details_widget.isVisible()
        )

    def toggle_runtime_debug(self):
        self.runtime_debug_expanded = not self.runtime_debug_expanded
        if self._diagnostics_enabled():
            self._overlay_diagnostics.observe_ui_event(
                "COMPACT_RUNTIME_DEBUG_TOGGLED",
                expanded=self.runtime_debug_expanded,
            )
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
        if self._diagnostics_enabled():
            menu.aboutToShow.connect(
                lambda: self._overlay_diagnostics.observe_popup(
                    "SCREENBOT_MENU_POPUP", menu, "CONTEXT_MENU_SHOW"
                )
            )
            menu.aboutToHide.connect(
                lambda: self._overlay_diagnostics.observe_popup(
                    "SCREENBOT_MENU_POPUP", menu, "CONTEXT_MENU_HIDE"
                )
            )
        menu.exec(QCursor.pos())

    def hideEvent(self, event):
        if not self._coordinator_visibility_change:
            self._ui_visibility_intent = False
            self.compact_visibility_intent_changed.emit(False)
        self._observe_lifecycle("COMPACT_HIDE_EVENT")
        super().hideEvent(event)

    def closeEvent(self, event):
        if not self._coordinator_visibility_change:
            self._ui_visibility_intent = False
            self.compact_visibility_intent_changed.emit(False)
        self._observe_lifecycle("COMPACT_CLOSE_EVENT")
        if not self._suppress_exit_requests:
            self.exit_requested.emit()
        super().closeEvent(event)

    def moveEvent(self, event):
        super().moveEvent(event)
        self.compact_geometry_changed.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.compact_geometry_changed.emit()

    def is_compact_visibility_intended(self):
        return self._ui_visibility_intent

    def set_target_visibility_suppressed(self, suppressed):
        """Apply target lifecycle suppression without changing user intent."""
        suppressed = bool(suppressed)
        if suppressed == self._target_visibility_suppressed:
            return
        self._target_visibility_suppressed = suppressed
        self._coordinator_visibility_change = True
        try:
            if suppressed:
                if self.isVisible():
                    QWidget.hide(self)
            elif self._ui_visibility_intent and not self.isVisible():
                QWidget.show(self)
                QTimer.singleShot(0, self._apply_native_no_activate)
        finally:
            self._coordinator_visibility_change = False

    def _on_script_selected(self, index):
        filename = self.combo.itemData(index)
        if filename:
            self.script_selected.emit(filename)

    def _on_workflow_selected(self, index):
        filename = self.workflow_combo.itemData(index)
        if filename:
            self.workflow_selected.emit(filename)
