from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QPlainTextEdit,
)


STATE_COLORS = {
    "IDLE": "#808080",
    "STOPPED": "#808080",
    "WAIT_TRIGGER": "#f0c040",
    "RUNNING_MACRO": "#3c78d8",
    "FINISHED": "#40a040",
    "ERROR": "#c02020",
}


class WorkflowDebugPanel(QWidget):
    """Read-only runtime debug panel for workflow diagnostics."""

    def __init__(self, snapshot_provider, export_log_callback, logger=None):
        super().__init__(None, Qt.Window | Qt.Tool)
        self.snapshot_provider = snapshot_provider
        self.export_log_callback = export_log_callback
        self.logger = logger
        self._last_snapshot = {}
        self._last_status = "IDLE"
        self._max_ocr_chars = 1200

        self.setWindowTitle("Workflow Runtime Debug Panel")
        self.resize(780, 760)
        self.setMinimumSize(700, 600)

        self._build_ui()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(300)
        self.refresh_timer.timeout.connect(self.refresh_now)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        top_row = QHBoxLayout()
        self.state_badge = QLabel("IDLE")
        self.state_badge.setAlignment(Qt.AlignCenter)
        self.state_badge.setFixedWidth(120)
        self.state_badge.setStyleSheet("border-radius: 6px; padding: 4px; background: #808080; color: white;")
        top_row.addWidget(self.state_badge)
        self.updated_label = QLabel("Updated: N/A")
        top_row.addWidget(self.updated_label)
        top_row.addStretch()
        root.addLayout(top_row)

        root.addWidget(self._build_workflow_group())
        root.addWidget(self._build_trigger_group())
        root.addWidget(self._build_stop_trigger_group())
        root.addWidget(self._build_macro_group())
        root.addWidget(self._build_target_group())
        root.addWidget(self._build_error_group())
        root.addWidget(self._build_ocr_group(), 1)
        root.addWidget(self._build_timeline_group(), 1)
        root.addLayout(self._build_actions_row())

    def _build_workflow_group(self):
        box = QGroupBox("Workflow")
        lay = QVBoxLayout(box)
        self.workflow_name_label = QLabel("Workflow Name: N/A")
        self.workflow_file_label = QLabel("Workflow File: N/A")
        self.runtime_state_label = QLabel("Runtime State: IDLE")
        self.step_index_label = QLabel("Current Step Index: N/A")
        self.total_steps_label = QLabel("Total Steps: N/A")
        self.step_id_label = QLabel("Current Step ID: N/A")
        self.loop_mode_label = QLabel("Loop Mode: N/A")
        self.cycle_label = QLabel("Cycle: N/A")
        self.completed_cycles_label = QLabel("Completed Cycles: N/A")
        self.max_cycles_label = QLabel("Maximum Cycles: N/A")
        self.restart_step_label = QLabel("Restart Step: N/A")
        self.finish_reason_label = QLabel("Finish Reason: N/A")
        for w in (
            self.workflow_name_label,
            self.workflow_file_label,
            self.runtime_state_label,
            self.step_index_label,
            self.total_steps_label,
            self.step_id_label,
            self.loop_mode_label,
            self.cycle_label,
            self.completed_cycles_label,
            self.max_cycles_label,
            self.restart_step_label,
            self.finish_reason_label,
        ):
            lay.addWidget(w)
        return box

    def _build_trigger_group(self):
        box = QGroupBox("Trigger")
        lay = QVBoxLayout(box)
        self.trigger_type_label = QLabel("Trigger Type: N/A")
        self.trigger_event_label = QLabel("Trigger Event: N/A")
        self.expected_text_label = QLabel("Expected Text: N/A")
        self.trigger_state_label = QLabel("Trigger State: N/A")
        self.observation_label = QLabel("Observation: N/A")
        self.observation_scores_label = QLabel("Scores: N/A")
        self.observation_valid_label = QLabel("Observation Valid: N/A")
        self.observation_reason_label = QLabel("Observation Reason: N/A")
        self.disappear_progress_label = QLabel("Disappear Confirmation: N/A")
        self.capture_runtime_label = QLabel("Capture Runtime: N/A")
        self.poll_count_label = QLabel("Poll Count: N/A")
        self.confirm_progress_label = QLabel("Confirm Progress: N/A")
        self.cooldown_label = QLabel("Cooldown Remaining: N/A")
        for w in (
            self.trigger_type_label,
            self.trigger_event_label,
            self.expected_text_label,
            self.trigger_state_label,
            self.observation_label,
            self.observation_scores_label,
            self.observation_valid_label,
            self.observation_reason_label,
            self.disappear_progress_label,
            self.capture_runtime_label,
            self.poll_count_label,
            self.confirm_progress_label,
            self.cooldown_label,
        ):
            lay.addWidget(w)
        return box

    def _build_macro_group(self):
        box = QGroupBox("Macro")
        lay = QVBoxLayout(box)
        self.current_macro_label = QLabel("Current Macro: N/A")
        self.player_active_label = QLabel("Player Active: N/A")
        self.player_state_label = QLabel("Player State: N/A")
        self.last_macro_start_label = QLabel("Last Macro Start: N/A")
        self.last_macro_finish_label = QLabel("Last Macro Finish: N/A")
        for w in (
            self.current_macro_label,
            self.player_active_label,
            self.player_state_label,
            self.last_macro_start_label,
            self.last_macro_finish_label,
        ):
            lay.addWidget(w)
        return box

    def _build_stop_trigger_group(self):
        box = QGroupBox("Global Stop Trigger")
        lay = QVBoxLayout(box)
        self.stop_trigger_enabled_label = QLabel("Stop Trigger Enabled: N/A")
        self.stop_trigger_type_label = QLabel("Stop Trigger Type: N/A")
        self.stop_trigger_event_label = QLabel("Stop Trigger Event: N/A")
        self.stop_trigger_text_label = QLabel("Stop Trigger Text: N/A")
        self.stop_trigger_state_label = QLabel("Stop Trigger State: N/A")
        self.stop_confirm_progress_label = QLabel("Stop Confirm Progress: N/A")
        self.stop_last_ocr_text_label = QLabel("Stop Last OCR Text: N/A")
        self.pending_stop_label = QLabel("Pending Stop: N/A")
        self.stop_trigger_time_label = QLabel("Stop Trigger Timestamp: N/A")
        for w in (
            self.stop_trigger_enabled_label,
            self.stop_trigger_type_label,
            self.stop_trigger_event_label,
            self.stop_trigger_text_label,
            self.stop_trigger_state_label,
            self.stop_confirm_progress_label,
            self.stop_last_ocr_text_label,
            self.pending_stop_label,
            self.stop_trigger_time_label,
        ):
            lay.addWidget(w)
        return box

    def _build_target_group(self):
        box = QGroupBox("Target Window")
        lay = QVBoxLayout(box)
        self.target_hwnd_label = QLabel("Target HWND: N/A")
        self.target_title_label = QLabel("Window Title: N/A")
        self.target_valid_label = QLabel("Window Valid: N/A")
        self.target_rect_label = QLabel("Window Rect: N/A")
        self.foreground_label = QLabel("Foreground Status: N/A")
        self.capture_note_label = QLabel(
            "Capture depends on visible screen pixels.\nCovered or minimized windows may produce incorrect OCR results."
        )
        self.capture_note_label.setWordWrap(True)
        for w in (
            self.target_hwnd_label,
            self.target_title_label,
            self.target_valid_label,
            self.target_rect_label,
            self.foreground_label,
            self.capture_note_label,
        ):
            lay.addWidget(w)
        return box

    def _build_error_group(self):
        box = QGroupBox("Error")
        lay = QVBoxLayout(box)
        self.last_error_label = QLabel("Last Error: None")
        self.error_time_label = QLabel("Error Timestamp: N/A")
        self.error_source_label = QLabel("Error Source: N/A")
        for w in (self.last_error_label, self.error_time_label, self.error_source_label):
            lay.addWidget(w)
        return box

    def _build_ocr_group(self):
        box = QGroupBox("OCR")
        lay = QVBoxLayout(box)
        self.ocr_timestamp_label = QLabel("Last OCR Timestamp: N/A")
        self.ocr_success_label = QLabel("Last OCR Success: N/A")
        self.ocr_region_label = QLabel("OCR Region: N/A")
        self.ocr_text_edit = QPlainTextEdit()
        self.ocr_text_edit.setReadOnly(True)
        self.ocr_text_edit.setPlaceholderText("Last OCR Text...")
        self.ocr_text_edit.setMaximumBlockCount(200)
        lay.addWidget(self.ocr_timestamp_label)
        lay.addWidget(self.ocr_success_label)
        lay.addWidget(self.ocr_region_label)
        lay.addWidget(self.ocr_text_edit, 1)
        return box

    def _build_timeline_group(self):
        box = QGroupBox("Runtime Timeline (latest 30)")
        lay = QVBoxLayout(box)
        self.timeline_edit = QPlainTextEdit()
        self.timeline_edit.setReadOnly(True)
        self.timeline_edit.setPlaceholderText("Timeline...")
        self.timeline_edit.setMaximumBlockCount(80)
        lay.addWidget(self.timeline_edit, 1)
        return box

    def _build_actions_row(self):
        row = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh Now")
        self.copy_button = QPushButton("Copy Snapshot")
        self.export_button = QPushButton("Export Runtime Log")
        self.clear_button = QPushButton("Clear View")
        row.addWidget(self.refresh_button)
        row.addWidget(self.copy_button)
        row.addWidget(self.export_button)
        row.addWidget(self.clear_button)
        row.addStretch()

        self.refresh_button.clicked.connect(self.refresh_now)
        self.copy_button.clicked.connect(self.copy_snapshot)
        self.export_button.clicked.connect(self.export_runtime_log)
        self.clear_button.clicked.connect(self.clear_view)
        return row

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_timer.start()
        self.refresh_now()

    def closeEvent(self, event):
        self.refresh_timer.stop()
        super().closeEvent(event)

    def _fmt(self, value, default="N/A"):
        if value is None:
            return default
        if isinstance(value, str):
            txt = value.strip()
            return txt if txt else default
        return str(value)

    def _set_state_badge(self, state):
        color = STATE_COLORS.get(state, "#606060")
        self.state_badge.setText(state)
        self.state_badge.setStyleSheet(f"border-radius: 6px; padding: 4px; background: {color}; color: white;")
        self._last_status = state

    def refresh_now(self):
        try:
            snapshot = self.snapshot_provider() or {}
        except Exception as exc:
            if self.logger:
                self.logger.exception("Debug panel snapshot provider failed")
            self.last_error_label.setText(f"Last Error: {exc}")
            return
        self._last_snapshot = snapshot
        self._render_snapshot(snapshot)

    def _render_snapshot(self, snap):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.updated_label.setText(f"Updated: {now}")

        state = self._fmt(snap.get("runtime_state"), default="IDLE")
        self._set_state_badge(state)

        self.workflow_name_label.setText(f"Workflow Name: {self._fmt(snap.get('workflow_name'))}")
        self.workflow_file_label.setText(f"Workflow File: {self._fmt(snap.get('workflow_file'))}")
        self.runtime_state_label.setText(f"Runtime State: {state}")
        self.step_index_label.setText(f"Current Step Index: {self._fmt(snap.get('current_step_index'))}")
        self.total_steps_label.setText(f"Total Steps: {self._fmt(snap.get('total_steps'))}")
        self.step_id_label.setText(f"Current Step ID: {self._fmt(snap.get('current_step_id'))}")

        self.trigger_type_label.setText(f"Trigger Type: {self._fmt(snap.get('trigger_type'))}")
        self.trigger_event_label.setText(f"Trigger Event: {self._fmt(snap.get('trigger_event'))}")
        self.expected_text_label.setText(f"Expected Text: {self._fmt(snap.get('expected_text'))}")
        self.trigger_state_label.setText(f"Trigger State: {self._fmt(snap.get('trigger_state'))}")
        self.observation_label.setText(f"Observation: {self._fmt(snap.get('observation_state'))}")
        self.observation_scores_label.setText(
            "Scores: "
            f"text={self._fmt(snap.get('text_similarity'))}, "
            f"readability={self._fmt(snap.get('readability_score'))}, "
            f"presence={self._fmt(snap.get('presence_score'))}"
        )
        self.observation_valid_label.setText(
            f"Observation Valid: {self._fmt(snap.get('observation_valid'))}; exact={self._fmt(snap.get('exact_match'))}"
        )
        self.observation_reason_label.setText(f"Observation Reason: {self._fmt(snap.get('observation_reason'))}")
        self.disappear_progress_label.setText(
            "Disappear Confirmation: "
            f"armed={self._fmt(snap.get('trigger_armed'))}, "
            f"frames={self._fmt(snap.get('absent_frames'))}/{self._fmt(snap.get('required_frames'))}, "
            f"duration={self._fmt(snap.get('absent_duration_ms'))}/{self._fmt(snap.get('required_absent_duration_ms'))} ms"
        )
        self.capture_runtime_label.setText(
            "Capture Runtime: "
            f"backend={self._fmt(snap.get('capture_backend'))}, "
            f"session={self._fmt(snap.get('capture_session_id'))}, "
            f"recreates={self._fmt(snap.get('capture_session_recreate_count'))}, "
            f"visibility={self._fmt(snap.get('screenbot_visibility_changed'))}, "
            f"foreground={self._fmt(snap.get('foreground_window_changed'))}, "
            f"overlay={self._fmt(snap.get('overlay_create_count'))}"
        )
        self.poll_count_label.setText(f"Poll Count: {self._fmt(snap.get('poll_count'))}")
        self.confirm_progress_label.setText(f"Confirm Progress: {self._fmt(snap.get('confirm_progress'))}")
        self.cooldown_label.setText(f"Cooldown Remaining: {self._fmt(snap.get('cooldown_remaining_ms'))} ms")

        self.current_macro_label.setText(f"Current Macro: {self._fmt(snap.get('current_macro'))}")
        self.player_active_label.setText(f"Player Active: {self._fmt(snap.get('player_active'))}")
        self.player_state_label.setText(f"Player State: {self._fmt(snap.get('player_state'))}")
        self.last_macro_start_label.setText(f"Last Macro Start: {self._fmt(snap.get('last_macro_start'))}")
        self.last_macro_finish_label.setText(f"Last Macro Finish: {self._fmt(snap.get('last_macro_finish'))}")

        self.target_hwnd_label.setText(f"Target HWND: {self._fmt(snap.get('target_hwnd'))}")
        self.target_title_label.setText(f"Window Title: {self._fmt(snap.get('target_title'))}")
        self.target_valid_label.setText(f"Window Valid: {self._fmt(snap.get('window_valid'))}")
        self.target_rect_label.setText(f"Window Rect: {self._fmt(snap.get('window_rect'))}")
        self.foreground_label.setText(f"Foreground Status: {self._fmt(snap.get('foreground_status'))}")

        last_error = self._fmt(snap.get("last_error"), default="None")
        self.last_error_label.setText(f"Last Error: {last_error}")
        self.error_time_label.setText(f"Error Timestamp: {self._fmt(snap.get('error_timestamp'))}")
        self.error_source_label.setText(f"Error Source: {self._fmt(snap.get('error_source'))}")

        loop_mode = self._fmt(snap.get("loop_mode"))
        current_cycle = self._fmt(snap.get("current_cycle"))
        max_cycles = snap.get("max_cycles")
        max_cycle_text = "∞" if max_cycles in (None, "", "N/A") and loop_mode in {"manual_stop", "stop_trigger"} else self._fmt(max_cycles)
        self.loop_mode_label.setText(f"Loop Mode: {loop_mode}")
        self.cycle_label.setText(f"Cycle: {current_cycle} / {max_cycle_text}")
        self.completed_cycles_label.setText(f"Completed Cycles: {self._fmt(snap.get('completed_cycles'))}")
        self.max_cycles_label.setText(f"Maximum Cycles: {max_cycle_text}")
        self.restart_step_label.setText(f"Restart Step: {self._fmt(snap.get('restart_step'))}")
        self.finish_reason_label.setText(f"Finish Reason: {self._fmt(snap.get('finish_reason'), default='N/A')}")

        self.stop_trigger_enabled_label = getattr(self, "stop_trigger_enabled_label", None)
        if self.stop_trigger_enabled_label is not None:
            self.stop_trigger_enabled_label.setText(f"Stop Trigger Enabled: {self._fmt(snap.get('stop_trigger_enabled'))}")
            self.stop_trigger_type_label.setText(f"Stop Trigger Type: {self._fmt(snap.get('stop_trigger_type'))}")
            self.stop_trigger_event_label.setText(f"Stop Trigger Event: {self._fmt(snap.get('stop_trigger_event'))}")
            self.stop_trigger_text_label.setText(f"Stop Trigger Text: {self._fmt(snap.get('stop_trigger_text'))}")
            self.stop_trigger_state_label.setText(f"Stop Trigger State: {self._fmt(snap.get('stop_trigger_state'))}")
            self.stop_confirm_progress_label.setText(f"Stop Confirm Progress: {self._fmt(snap.get('stop_confirm_progress'))}")
            self.stop_last_ocr_text_label.setText(f"Stop Last OCR Text: {self._fmt(snap.get('stop_last_ocr_text'))}")
            self.pending_stop_label.setText(f"Pending Stop: {self._fmt(snap.get('pending_stop'))}")
            self.stop_trigger_time_label.setText(f"Stop Trigger Timestamp: {self._fmt(snap.get('stop_trigger_timestamp'))}")

        self.ocr_timestamp_label.setText(f"Last OCR Timestamp: {self._fmt(snap.get('last_ocr_timestamp'))}")
        self.ocr_success_label.setText(f"Last OCR Success: {self._fmt(snap.get('last_ocr_success'))}")
        self.ocr_region_label.setText(f"OCR Region: {self._fmt(snap.get('ocr_region'))}")
        ocr_text = self._fmt(snap.get("last_ocr_text"), default="")
        if len(ocr_text) > self._max_ocr_chars:
            ocr_text = ocr_text[: self._max_ocr_chars] + "\n...(truncated)..."
        self.ocr_text_edit.setPlainText(ocr_text)

        timeline_lines = snap.get("timeline_lines") or []
        self.timeline_edit.setPlainText("\n".join(timeline_lines))

    def _snapshot_to_text(self, snap):
        def g(key, default="N/A"):
            val = snap.get(key)
            if val is None:
                return default
            if isinstance(val, str):
                txt = val.strip()
                return txt if txt else default
            return str(val)

        timeline_lines = snap.get("timeline_lines") or []
        return "\n".join(
            [
                "ScreenBot Runtime Snapshot",
                "",
                f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"Workflow: {g('workflow_name')}",
                f"Workflow File: {g('workflow_file')}",
                f"Runtime State: {g('runtime_state')}",
                f"Current Step: {g('current_step_id')}",
                f"Step Index: {g('current_step_index')}",
                f"Total Steps: {g('total_steps')}",
                f"Loop Mode: {g('loop_mode')}",
                f"Current Cycle: {g('current_cycle')}",
                f"Completed Cycles: {g('completed_cycles')}",
                f"Maximum Cycles: {g('max_cycles')}",
                f"Restart Step: {g('restart_step')}",
                f"Finish Reason: {g('finish_reason')}",
                "",
                f"Target Window: {g('target_title')}",
                f"Target HWND: {g('target_hwnd')}",
                f"Window Valid: {g('window_valid')}",
                f"Window Rect: {g('window_rect')}",
                f"Foreground Status: {g('foreground_status')}",
                "",
                f"Trigger Type: {g('trigger_type')}",
                f"Trigger Event: {g('trigger_event')}",
                f"Expected Text: {g('expected_text')}",
                f"Trigger State: {g('trigger_state')}",
                f"Poll Count: {g('poll_count')}",
                f"Confirm Progress: {g('confirm_progress')}",
                f"Cooldown Remaining: {g('cooldown_remaining_ms')}",
                "",
                f"Last OCR Text: {g('last_ocr_text', '')}",
                f"Last OCR Timestamp: {g('last_ocr_timestamp')}",
                f"Last OCR Success: {g('last_ocr_success')}",
                f"OCR Region: {g('ocr_region')}",
                "",
                f"Current Macro: {g('current_macro')}",
                f"Player Active: {g('player_active')}",
                f"Player State: {g('player_state')}",
                f"Last Macro Start: {g('last_macro_start')}",
                f"Last Macro Finish: {g('last_macro_finish')}",
                "",
                f"Last Error: {g('last_error', 'None')}",
                f"Error Timestamp: {g('error_timestamp')}",
                f"Error Source: {g('error_source')}",
                "",
                f"Stop Trigger Enabled: {g('stop_trigger_enabled')}",
                f"Stop Trigger Type: {g('stop_trigger_type')}",
                f"Stop Trigger Event: {g('stop_trigger_event')}",
                f"Stop Trigger Text: {g('stop_trigger_text')}",
                f"Stop Trigger State: {g('stop_trigger_state')}",
                f"Stop Confirm Progress: {g('stop_confirm_progress')}",
                f"Stop Last OCR Text: {g('stop_last_ocr_text')}",
                f"Pending Stop: {g('pending_stop')}",
                f"Stop Trigger Timestamp: {g('stop_trigger_timestamp')}",
                "",
                "Recent Timeline:",
                *timeline_lines,
            ]
        )

    def copy_snapshot(self):
        snap_text = self._snapshot_to_text(self._last_snapshot or {})
        QApplication.clipboard().setText(snap_text)
        self.updated_label.setText(f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (snapshot copied)")

    def export_runtime_log(self):
        try:
            path = self.export_log_callback()
        except Exception as exc:
            if self.logger:
                self.logger.exception("Debug panel export runtime log failed")
            self.last_error_label.setText(f"Last Error: {exc}")
            return
        path_text = self._fmt(path)
        self.updated_label.setText(f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (exported {path_text})")

    def clear_view(self):
        self.ocr_text_edit.clear()
        self.timeline_edit.clear()
        self.updated_label.setText(f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (view cleared)")
