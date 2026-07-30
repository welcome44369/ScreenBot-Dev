import sys
import logging
import ctypes
import copy
import os
import threading
import time
from pathlib import Path
from datetime import datetime
from shutil import which

from PySide6.QtCore import QObject, QTimer, Qt, QUrl, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QInputDialog
from PySide6.QtGui import QCloseEvent, QDesktopServices

from app.logging_setup import setup_logging
from app.settings import Settings
from app.script_store import ScriptStore
from app.window_tracker import WindowTracker
from app.target_session import TargetConnectionState, TargetSessionService
from app.target_relative_overlay import TargetRelativeOverlayCoordinator
from app.input_safety import ForegroundInputSafetyGate, InputAuthorizationCode
from app.recorder import ActionRecorder
from app.player import ScriptPlayer
from app.floating_widget import FloatingWidget
from app.state import AppState
from app.hotkeys import HotkeyBridge, HotkeyManager
from app.recorder_overlay import RecorderOverlayController
from app.text_detector import TextDetector
from app.trigger_runner import TriggerRunner
from app.workflow_runner import WorkflowRunner
from app.workflow_store import WorkflowStore
from app.trigger_store import TriggerStore
from app.workflow_resolver import WorkflowResolver
from app.trigger_manager import TriggerManager
from app.workflow_manager import WorkflowManager
from app.macro_manager import MacroManager
from app.dialog_utils import show_info, show_warning, show_error, ask_confirmation
from app.workflow_diagnostics import WorkflowDiagnostics
from app.workflow_editor import WorkflowEditor
from app.workflow_debug_panel import WorkflowDebugPanel
from app.start_handoff import (
    StartHandoffCode,
    StartHandoffService,
    StartHandoffState,
    StartRequestKind,
)


class RecorderOverlayBridge(QObject):
    ripple_requested = Signal(dict)


class OcrProcessProbeBridge(QObject):
    completed = Signal(dict)
    failed = Signal(str)


class ScreenBotApp:
    def __init__(self, root_path):
        self.root_path = Path(root_path)
        self.logger = setup_logging(self.root_path)
        self.settings = Settings(self.root_path)
        self.script_store = ScriptStore(self.root_path)
        self.workflow_store = WorkflowStore(self.root_path, self.logger)
        self.trigger_store = TriggerStore(self.root_path)
        self.workflow_resolver = WorkflowResolver(self.trigger_store, self.script_store)
        self.window_tracker = WindowTracker()
        self.target_session = TargetSessionService(self.window_tracker, self.logger)
        self.window_tracker.attach_target_session(self.target_session)
        self.input_safety_gate = ForegroundInputSafetyGate(self.target_session, self.window_tracker, self.logger)
        self.ocr_process_diagnostics = None
        self.workflow_process_diagnostics = None
        if os.environ.get("SCREENBOT_WORKFLOW_PROCESS_DIAGNOSTIC") == "1":
            from app.workflow_process_diagnostics import WorkflowProcessDiagnostics

            self.workflow_process_diagnostics = WorkflowProcessDiagnostics(
                self.root_path,
                self.logger,
            )
        if os.environ.get("SCREENBOT_OCR_PROCESS_DIAGNOSTIC") == "1":
            from app.ocr_process_diagnostics import OcrProcessDiagnostics

            self.ocr_process_diagnostics = OcrProcessDiagnostics(
                self.root_path,
                self.logger,
            )
        self.text_detector = TextDetector(
            self.window_tracker,
            self.logger,
            tesseract_cmd=self._resolve_tesseract_cmd(),
            lang=self.settings.get("ocr_lang", "eng+chi_tra"),
        )
        self.text_detector.ocr_pipeline.set_process_diagnostics(
            self.workflow_process_diagnostics or self.ocr_process_diagnostics
        )
        self.trigger_runner = None
        self.player = ScriptPlayer(self.window_tracker, input_safety_gate=self.input_safety_gate)
        self.player.on_error = self._handle_player_error
        self.player.on_finished = self._handle_player_finished
        self.workflow_runner = WorkflowRunner(
            self.text_detector,
            self.script_store,
            self.player,
            logger=self.logger,
            input_safety_gate=self.input_safety_gate,
            workflow_process_diagnostics=self.workflow_process_diagnostics,
        )
        self.workflow_diagnostics = WorkflowDiagnostics(self.root_path, self.logger)
        self.start_handoff = StartHandoffService(timeout_seconds=15.0)
        self.workflow_data = None
        self.selected_workflow_filename = None
        self.workflow_ui_state = "IDLE"
        self.workflow_execution_ui_state = "IDLE_EXPANDED"
        self._workflow_editor = None  # single editor instance guard
        self._workflow_debug_panel = None
        self._trigger_manager = None
        self._workflow_manager = None
        self._macro_manager = None

        self.app = QApplication([])
        self.app.setQuitOnLastWindowClosed(False)

        self.overlay_diagnostics = None
        if os.environ.get("SCREENBOT_OVERLAY_DIAGNOSTIC") == "1":
            from app.overlay_diagnostics import OverlayDiagnostics

            self.overlay_diagnostics = OverlayDiagnostics(self.root_path, self.logger)

        self.widget = FloatingWidget(
            self.root_path,
            overlay_diagnostics=self.overlay_diagnostics,
        )
        self.target_relative_overlay = TargetRelativeOverlayCoordinator(
            self.target_session,
            self.widget,
            self.logger,
        )
        self.target_relative_overlay.start()
        if self.overlay_diagnostics is not None:
            self.target_session.subscribe(self._on_overlay_target_session_event)
            self.overlay_diagnostics.start()
        self.widget.script_selected.connect(self.load_script)
        self.widget.start_recording.connect(self.start_recording)
        self.widget.stop_recording.connect(self.stop_recording)
        self.widget.save_recording.connect(self.save_recording)
        self.widget.open_scripts_folder.connect(self.open_script_folder)
        self.widget.exit_requested.connect(self.confirm_exit)
        self.widget.background_opacity_changed.connect(self._change_background_opacity)
        self.widget.workflow_selected.connect(self._select_workflow)
        self.widget.start_workflow.connect(self._start_workflow_from_ui)
        self.widget.stop_workflow.connect(self._stop_workflow_from_ui)
        self.widget.refresh_workflows.connect(self._refresh_workflows)
        self.widget.export_runtime_log.connect(self._export_runtime_log)
        self.widget.open_runtime_log_folder.connect(self.open_runtime_log_folder)
        self.widget.open_workflow_editor.connect(self.open_workflow_editor)
        self.widget.open_runtime_debug_panel.connect(self.open_runtime_debug_panel)
        self.widget.open_trigger_manager.connect(self.open_trigger_manager)
        self.widget.open_macro_manager.connect(self.open_macro_manager)
        self.widget.open_workflow_manager.connect(self.open_workflow_manager)
        self.ocr_process_probe_bridge = OcrProcessProbeBridge()
        self.ocr_process_probe_bridge.completed.connect(
            self._on_ocr_process_probe_completed
        )
        self.ocr_process_probe_bridge.failed.connect(
            self._on_ocr_process_probe_failed
        )
        if self.ocr_process_diagnostics is not None:
            self.widget.run_ocr_process_probe.connect(
                self._run_ocr_process_probe
            )

        self.recorder_overlay = RecorderOverlayController(self.window_tracker, self.logger)
        self.recorder_overlay_bridge = RecorderOverlayBridge()
        self.recorder_overlay_bridge.ripple_requested.connect(self._on_overlay_ripple_requested)
        self._recorded_event_count = 0

        # Recorder: create after widget so we can pass a Qt-safe callback
        def _on_event_count(count):
            # The listener thread records only the source value.  The Qt
            # thread composes the compact status once with all other sources.
            QTimer.singleShot(0, lambda value=count: self._on_recording_event_count(value))

        def _on_click_recorded(payload):
            self.recorder_overlay_bridge.ripple_requested.emit(dict(payload))

        self.recorder = ActionRecorder(
            self.window_tracker,
            self.settings,
            on_event=_on_event_count,
            on_click=_on_click_recorded,
        )

        self.state = AppState.IDLE
        self.current_script = None
        self.selected_script_name = None
        self.last_recording = None
        self.last_recording_saved = True

        self.countdown_timer = QTimer()
        self.countdown_timer.setInterval(1000)
        self.countdown_timer.timeout.connect(self._countdown_tick)
        self.countdown_value = 0
        self.start_handoff_timer = QTimer()
        self.start_handoff_timer.setInterval(150)
        self.start_handoff_timer.timeout.connect(self._on_start_handoff_tick)
        self.workflow_ui_timer = QTimer()
        self.workflow_ui_timer.setInterval(250)
        self.workflow_ui_timer.timeout.connect(self._update_workflow_runtime_ui)
        self.workflow_ui_timer.start()

        self._load_scripts()
        self._refresh_workflows()
        self._restore_position()
        self.widget.set_background_opacity(self.settings.get("background_opacity", 0.35))
        self._update_workflow_runtime_ui()
        self.widget.set_workflow_running(False)
        # Hotkey bridge and manager
        self.hotkey_bridge = HotkeyBridge()
        self.hotkey_manager = HotkeyManager(self.hotkey_bridge, self.logger, self.settings.get("exit_hotkey", "esc"))
        # Connect bridge signals to slots (Qt main thread safe)
        self.hotkey_bridge.f8_pressed.connect(self._on_bridge_f8)
        self.hotkey_bridge.f9_pressed.connect(self._on_bridge_f9)
        self.hotkey_bridge.esc_pressed.connect(self._on_bridge_esc)
        # Register hotkeys
        try:
            self.hotkey_manager.register_hotkeys()
        except Exception:
            self.logger.exception("Hotkey registration failed")

        self.set_state(AppState.IDLE)
        self.widget.show()
        self.logger.info("ScreenBot 啟動")

    def _resolve_tesseract_cmd(self):
        configured = self.settings.get("tesseract_cmd")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        if which("tesseract"):
            return None
        default_path = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        if default_path.exists():
            return str(default_path)
        return None

    def run(self):
        sys.exit(self.app.exec())

    def _register_hotkeys(self):
        # Legacy compatibility: retained for external callers
        try:
            if hasattr(self, 'hotkey_manager') and self.hotkey_manager is not None:
                self.hotkey_manager.register_hotkeys()
                self.logger.info("註冊熱鍵 (透過 HotkeyManager)")
        except Exception:
            self.logger.exception("_register_hotkeys failed")

    def _unregister_hotkeys(self):
        try:
            if hasattr(self, 'hotkey_manager') and self.hotkey_manager is not None:
                self.hotkey_manager.unregister_all()
                self.logger.info("已解除所有熱鍵註冊")
        except Exception:
            self.logger.exception("_unregister_hotkeys failed")

    def _change_background_opacity(self, value):
        self.settings.set("background_opacity", value)

    def _load_scripts(self):
        scripts = self.script_store.list_scripts()
        self.widget.set_script_list(scripts)
        if scripts:
            self.widget.select_script(scripts[0][0])
            self.load_script(scripts[0][0])

    def _restore_position(self):
        pos = self.settings.get("bubble_position", [120, 120])
        self.widget.move(pos[0], pos[1])

    def set_state(self, state, countdown_text=None):
        self.state = state
        self.widget.set_script_name(self.current_script.get("name") if self.current_script else "(未選擇)")
        self._refresh_compact_presentation(countdown_text=countdown_text)
        self.logger.info(f"狀態切換為 {state.name}")

    def _on_bridge_f8(self):
        try:
            self.logger.info(f"Hotkey bridge: F8 pressed; state={self.state.name}")
            self._handle_f8()
        except Exception:
            self.logger.exception("Exception handling bridge F8")

    def _on_bridge_f9(self):
        try:
            self.logger.info(f"Hotkey bridge: F9 pressed; state={self.state.name}")
            self._handle_f9()
        except Exception:
            self.logger.exception("Exception handling bridge F9")

    def _on_bridge_esc(self):
        try:
            self.logger.info(f"Hotkey bridge: ESC pressed; state={self.state.name}")
            self._handle_exit_hotkey()
        except Exception:
            self.logger.exception("Exception handling bridge ESC")

    def _on_overlay_ripple_requested(self, payload):
        try:
            self.recorder_overlay.show_click_ripple(payload)
        except Exception:
            self.logger.exception("Failed to show recorder ripple")

    def _on_recording_event_count(self, count):
        self._recorded_event_count = max(0, int(count))
        self._refresh_compact_presentation()

    def _handle_f8(self):
        """Lock or refresh the target only; F8 must never start execution."""
        self.logger.info("F8 熱鍵觸發：僅鎖定／更新目標視窗")
        if self.workflow_runner.is_active():
            # Changing the target during an active run would break the locked
            # target contract.  Crucially, this branch still performs no
            # countdown, player start, workflow start, or run-log creation.
            self.logger.info("Workflow 執行中，忽略 F8 目標更新")
            return
        self.lock_or_refresh_target_only()

    def lock_or_refresh_target_only(self):
        """Refresh the explicit target lock without changing runtime state."""
        self._record_overlay_diagnostic("TARGET_LOCK_REQUESTED")
        try:
            info = self.window_tracker.lock_foreground_window()
            target_session = getattr(self, "target_session", None)
            target_snapshot = (
                target_session.get_snapshot()
                if target_session is not None
                else None
            )
            target_relative_overlay = getattr(
                self, "target_relative_overlay", None
            )
            if (
                target_relative_overlay is not None
                and target_snapshot is not None
            ):
                target_relative_overlay.bind_snapshot(target_snapshot)
            self._record_overlay_target_event(
                "TARGET_LOCK_SUCCEEDED",
                target_snapshot,
            )
            self._invalidate_armed_start("target_relocked", unavailable=False)
            self.text_detector.invalidate_capture_target()
            # A replacement target starts a fresh condition session.  F8 is
            # still lock-only and this does not start a workflow or macro.
            clear_memory = getattr(self.workflow_runner, "clear_condition_memory", None)
            if callable(clear_memory):
                clear_memory()
            self._refresh_compact_presentation()
            self.logger.info(
                "Target locked/refreshed only: title=%s hwnd=%s client=%sx%s",
                info.title,
                getattr(info, "hwnd", None),
                getattr(info, "client_width", None),
                getattr(info, "client_height", None),
            )
            return info
        except Exception as exc:
            self._record_overlay_diagnostic(
                "TARGET_LOCK_FAILED", exception_type=type(exc).__name__, message=str(exc)
            )
            self.logger.warning("鎖定目標視窗失敗: %s", exc)
            self._refresh_compact_presentation()
            self._show_message("無法鎖定目標視窗", str(exc))
            return None

    def _handle_f9(self):
        self.logger.info("F9 熱鍵觸發")
        if self.state == AppState.RUNNING:
            self.pause_script()
        elif self.state == AppState.PAUSED:
            self.resume_script()
        else:
            self.logger.debug("F9 在當前狀態無動作")

    def _handle_exit_hotkey(self):
        self.logger.info("退出熱鍵觸發")
        self.confirm_exit()

    def _start_countdown(self):
        if self.state != AppState.IDLE:
            return
        self.countdown_value = 5
        self.set_state(AppState.COUNTDOWN, f"{self.countdown_value}s")
        self.countdown_timer.start()

    def _countdown_tick(self):
        self.countdown_value -= 1
        if self.countdown_value > 0:
            self.set_state(AppState.COUNTDOWN, f"{self.countdown_value}s")
            return
        self.countdown_timer.stop()
        try:
            info = self.window_tracker.lock_foreground_window()
            self.text_detector.invalidate_capture_target()
            self._refresh_compact_presentation()
            self.logger.info(f"鎖定視窗: {info.title} pid={info.pid}")
            self._start_script()
        except Exception as exc:
            self.logger.warning(f"鎖定失敗: {exc}")
            self.set_state(AppState.ERROR)
            self._show_message("無法鎖定目標視窗", str(exc))
            self.set_state(AppState.IDLE)

    def _start_script(self):
        """Arm the current script; actual input startup occurs only after handoff."""
        if self.workflow_runner.is_active():
            self._show_message("Workflow 執行中", "請先停止 Workflow，再執行單獨腳本。")
            self.set_state(AppState.IDLE)
            return
        if self.current_script is None:
            self._show_message("腳本未選擇", "請先從控制面板選擇一個腳本。")
            self.set_state(AppState.IDLE)
            return
        self._arm_start_request(
            StartRequestKind.DIRECT_SCRIPT,
            copy.deepcopy(self.current_script),
            self.current_script.get("name") or self.selected_script_name or "獨立腳本",
        )

    def _arm_start_request(self, kind, frozen_payload, display_name):
        self._record_overlay_diagnostic(
            "START_INTENT_RECEIVED",
            kind=getattr(kind, "value", str(kind)),
            display_name=display_name,
        )
        expected = self.input_safety_gate.expected_current_session()
        if expected is None:
            self._show_message("請先鎖定目標視窗", "請先使用 F8 鎖定前景視窗，再開始執行。")
            return False

        request = self.start_handoff.create_request(
            kind=kind,
            target_session_id=expected.session_id,
            target_generation=expected.generation,
            display_name=display_name,
            frozen_payload=frozen_payload,
        )
        result = self.start_handoff.arm(request)
        if result.code is StartHandoffCode.ALREADY_ARMED:
            self.logger.info("Start request ignored because request=%s is already armed", result.snapshot.request.request_id)
            self._refresh_handoff_presentation()
            return False

        if not self._collapse_ui_for_workflow_execution():
            transition = self.start_handoff.cancel("collapse_failed")
            self._record_start_handoff_event(
                "START_REQUEST_COLLAPSE_FAILED", transition.snapshot, request=request
            )
            self._restore_ui_after_workflow_execution("collapse_failed")
            return False

        self.set_state(AppState.IDLE)
        self.start_handoff_timer.start()
        diagnostics = getattr(self, "workflow_process_diagnostics", None)
        if diagnostics is not None and diagnostics.active:
            diagnostics.bind_request(request.request_id)
        self._record_start_handoff_event("START_REQUEST_CREATED", result.snapshot, request=request)
        self._record_start_handoff_event("START_REQUEST_ARMED", result.snapshot, request=request)
        self._refresh_handoff_presentation()
        # With the compact widget's no-activate policy the locked target remains
        # foreground, so the normal product path commits in this same click.
        # If Windows/Qt activated another root, the attempt safely remains ARMED.
        self._attempt_start_handoff()
        return True

    def _collapse_ui_for_workflow_execution(self):
        """Perform and acknowledge the one-shot UI collapse before handoff."""
        if self.workflow_execution_ui_state in {
            "COLLAPSING", "COLLAPSED_ARMED", "RUNNING_COLLAPSED"
        }:
            return True
        self.workflow_execution_ui_state = "COLLAPSING"
        collapse = getattr(self.widget, "collapse_for_workflow_execution", None)
        if not callable(collapse):
            self.logger.error("Workflow start blocked: widget has no collapse acknowledgement")
            self.workflow_execution_ui_state = "IDLE_EXPANDED"
            return False
        try:
            collapsed = bool(collapse())
            QApplication.processEvents()
            acknowledged = getattr(self.widget, "is_workflow_execution_collapsed", lambda: False)()
        except (RuntimeError, TypeError) as exc:
            self.logger.error("Workflow UI collapse failed: %s", exc)
            collapsed = acknowledged = False
        if not (collapsed and acknowledged):
            self.workflow_execution_ui_state = "IDLE_EXPANDED"
            return False
        self.workflow_execution_ui_state = "COLLAPSED_ARMED"
        self._record_overlay_diagnostic("WORKFLOW_UI_COLLAPSED")
        return True

    def _restore_ui_after_workflow_execution(self, reason):
        """Restore once for every terminal/cancelled/failed start path."""
        if self.workflow_execution_ui_state == "IDLE_EXPANDED":
            return True
        self.workflow_execution_ui_state = "RESTORING"
        restore = getattr(self.widget, "restore_after_workflow_execution", None)
        restored = False
        try:
            restored = bool(restore()) if callable(restore) else False
            QApplication.processEvents()
        except (RuntimeError, TypeError) as exc:
            self.logger.error("Workflow UI restore failed: %s", exc)
        self.workflow_execution_ui_state = "IDLE_EXPANDED"
        self._record_overlay_diagnostic("WORKFLOW_UI_RESTORED", reason=reason, restored=restored)
        return restored

    def _on_start_handoff_tick(self):
        self._attempt_start_handoff()

    def _attempt_start_handoff(self):
        snapshot = self.start_handoff.get_snapshot()
        if snapshot.state is not StartHandoffState.ARMED or snapshot.request is None:
            self.start_handoff_timer.stop()
            self._refresh_handoff_presentation()
            return

        expired = self.start_handoff.expire_if_due()
        if expired.code is StartHandoffCode.TIMED_OUT:
            self.start_handoff_timer.stop()
            self._record_start_handoff_event(
                "START_REQUEST_TIMED_OUT", expired.snapshot, request=expired.request
            )
            self._restore_ui_after_workflow_execution("start_request_timed_out")
            self._refresh_handoff_presentation()
            return

        request = snapshot.request
        try:
            current = self.target_session.refresh()
        except (OSError, RuntimeError):
            transition = self.start_handoff.invalidate_target(
                "target_refresh_failed", unavailable=True
            )
            self.start_handoff_timer.stop()
            self._record_start_handoff_event(
                "START_REQUEST_TARGET_UNAVAILABLE", transition.snapshot, request=transition.request
            )
            self._restore_ui_after_workflow_execution("target_refresh_failed")
            self._refresh_handoff_presentation()
            return

        if (
            current is None
            or current.session_id != request.target_session_id
            or current.generation != request.target_generation
        ):
            transition = self.start_handoff.invalidate_target("target_changed")
            self.start_handoff_timer.stop()
            self._record_start_handoff_event(
                "START_REQUEST_TARGET_CHANGED", transition.snapshot, request=transition.request
            )
            self._restore_ui_after_workflow_execution("target_changed")
            self._refresh_handoff_presentation()
            return

        expected = self.input_safety_gate.expected_current_session()
        if expected is None or (
            expected.session_id != request.target_session_id
            or expected.generation != request.target_generation
        ):
            transition = self.start_handoff.invalidate_target("target_changed")
            self.start_handoff_timer.stop()
            self._record_start_handoff_event(
                "START_REQUEST_TARGET_CHANGED", transition.snapshot, request=transition.request
            )
            self._restore_ui_after_workflow_execution("target_changed")
            self._refresh_handoff_presentation()
            return

        self._record_overlay_diagnostic(
            "START_IMMEDIATE_AUTHORIZATION_BEGIN",
            request_id=request.request_id,
            authorization_phase="fast",
        )
        fast = self.input_safety_gate.authorize_foreground_input_fast(expected)
        self._record_overlay_diagnostic(
            "START_IMMEDIATE_AUTHORIZATION_RESULT",
            request_id=request.request_id,
            authorization_phase="fast",
            authorization_code=fast.code.value,
            foreground_hwnd=fast.foreground_hwnd,
            foreground_root_hwnd=fast.foreground_root_hwnd,
        )
        if fast.code is InputAuthorizationCode.TARGET_NOT_FOREGROUND:
            self._record_overlay_diagnostic(
                "START_REQUEST_REMAINS_ARMED",
                request_id=request.request_id,
                reason=fast.code.value,
            )
            self._refresh_handoff_presentation()
            return
        if fast.code is not InputAuthorizationCode.ALLOWED:
            transition = self.start_handoff.invalidate_target(
                fast.code.value,
                unavailable=True,
            )
            self.start_handoff_timer.stop()
            self._record_start_handoff_event(
                "START_REQUEST_TARGET_UNAVAILABLE", transition.snapshot, request=transition.request
            )
            self._restore_ui_after_workflow_execution(fast.code.value)
            self._refresh_handoff_presentation()
            return

        self._record_overlay_diagnostic(
            "START_IMMEDIATE_AUTHORIZATION_BEGIN",
            request_id=request.request_id,
            authorization_phase="first_full",
        )
        first_authorization = self.input_safety_gate.authorize_foreground_input(expected)
        self._record_overlay_diagnostic(
            "START_IMMEDIATE_AUTHORIZATION_RESULT",
            request_id=request.request_id,
            authorization_phase="first_full",
            authorization_code=first_authorization.code.value,
            foreground_hwnd=first_authorization.foreground_hwnd,
            foreground_root_hwnd=first_authorization.foreground_root_hwnd,
        )
        if first_authorization.code is InputAuthorizationCode.TARGET_NOT_FOREGROUND:
            self._record_overlay_diagnostic(
                "START_REQUEST_REMAINS_ARMED",
                request_id=request.request_id,
                reason=first_authorization.code.value,
            )
            self._refresh_handoff_presentation()
            return
        if not first_authorization.allowed:
            transition = self.start_handoff.invalidate_target(
                first_authorization.code.value,
                unavailable=first_authorization.code is not InputAuthorizationCode.TARGET_CHANGED,
            )
            self.start_handoff_timer.stop()
            event = (
                "START_REQUEST_TARGET_CHANGED"
                if first_authorization.code is InputAuthorizationCode.TARGET_CHANGED
                else "START_REQUEST_TARGET_UNAVAILABLE"
            )
            self._record_start_handoff_event(event, transition.snapshot, request=transition.request)
            self._restore_ui_after_workflow_execution(first_authorization.code.value)
            self._refresh_handoff_presentation()
            return

        self._record_start_handoff_event(
            "START_REQUEST_TARGET_READY",
            snapshot,
            authorization=first_authorization,
        )
        claim = self.start_handoff.claim_commit(request.request_id)
        if claim.code is not StartHandoffCode.CLAIMED:
            self._refresh_handoff_presentation()
            return

        self._record_start_handoff_event("START_REQUEST_COMMITTING", claim.snapshot, request=request)
        self._record_overlay_diagnostic(
            "START_IMMEDIATE_AUTHORIZATION_BEGIN",
            request_id=request.request_id,
            authorization_phase="final_full",
        )
        final_authorization = self.input_safety_gate.authorize_foreground_input(expected)
        self._record_overlay_diagnostic(
            "START_IMMEDIATE_AUTHORIZATION_RESULT",
            request_id=request.request_id,
            authorization_phase="final_full",
            authorization_code=final_authorization.code.value,
            foreground_hwnd=final_authorization.foreground_hwnd,
            foreground_root_hwnd=final_authorization.foreground_root_hwnd,
        )
        if not final_authorization.allowed:
            completed = self.start_handoff.complete_failed(
                request.request_id, final_authorization.code.value
            )
            self.start_handoff_timer.stop()
            self._record_start_handoff_event(
                "START_REQUEST_FAILED",
                completed.snapshot,
                request=completed.request,
                authorization=final_authorization,
            )
            self._restore_ui_after_workflow_execution(final_authorization.code.value)
            self._refresh_handoff_presentation()
            return

        try:
            if request.kind is StartRequestKind.DIRECT_SCRIPT:
                start_result = self._start_script_from_handoff(
                    request.frozen_payload, expected
                )
                started = start_result.status.name == "STARTED"
                failure_reason = start_result.reason or start_result.status.value
            else:
                self._start_workflow_from_handoff(request.frozen_payload, expected)
                started = True
                failure_reason = None
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            started = False
            failure_reason = str(exc)

        self.start_handoff_timer.stop()
        if started:
            completed = self.start_handoff.complete_started(request.request_id)
            self._record_start_handoff_event(
                "START_REQUEST_STARTED", completed.snapshot, request=completed.request
            )
            self.workflow_execution_ui_state = "RUNNING_COLLAPSED"
        else:
            completed = self.start_handoff.complete_failed(request.request_id, failure_reason)
            self._record_start_handoff_event(
                "START_REQUEST_FAILED", completed.snapshot, request=completed.request
            )
            self._restore_ui_after_workflow_execution(failure_reason or "start_failed")
        self._record_overlay_diagnostic(
            "START_REQUEST_STARTED" if started else "START_REQUEST_FAILED",
            request_id=request.request_id,
            failure_reason=failure_reason,
        )
        self._refresh_handoff_presentation()

    def _start_script_from_handoff(self, frozen_script, expected_target):
        result = self.player.start(frozen_script)
        if result.status.name == "STARTED":
            self.set_state(AppState.RUNNING)
        return result

    def _cancel_armed_start(self, reason):
        if not hasattr(self, "start_handoff"):
            return False
        transition = self.start_handoff.cancel(reason)
        if transition.code is not StartHandoffCode.CANCELLED:
            return False
        self.start_handoff_timer.stop()
        self._record_start_handoff_event(
            "START_REQUEST_CANCELLED", transition.snapshot, request=transition.request
        )
        self._restore_ui_after_workflow_execution(reason)
        self.set_state(AppState.IDLE)
        self._refresh_handoff_presentation()
        return True

    def _invalidate_armed_start(self, reason, unavailable):
        if not hasattr(self, "start_handoff"):
            return False
        transition = self.start_handoff.invalidate_target(reason, unavailable=unavailable)
        if transition.code not in {
            StartHandoffCode.TARGET_CHANGED,
            StartHandoffCode.TARGET_UNAVAILABLE,
        }:
            return False
        self.start_handoff_timer.stop()
        event = (
            "START_REQUEST_TARGET_UNAVAILABLE"
            if unavailable else "START_REQUEST_TARGET_CHANGED"
        )
        self._record_start_handoff_event(event, transition.snapshot, request=transition.request)
        self._restore_ui_after_workflow_execution(reason)
        self._refresh_handoff_presentation()
        return True

    def stop_script(self):
        if self._cancel_armed_start("start_request_cancelled"):
            return
        if self.state not in {AppState.RUNNING, AppState.PAUSED}:
            return
        self.player.stop()
        self.set_state(AppState.IDLE)

    def pause_script(self):
        if self.state != AppState.RUNNING:
            return
        self.player.pause()
        self.set_state(AppState.PAUSED)

    def resume_script(self):
        if self.state != AppState.PAUSED:
            return
        self.player.resume()
        self.set_state(AppState.RUNNING)

    def start_recording(self):
        if self.state == AppState.RECORDING:
            return
        if self.workflow_runner.is_active():
            self._show_message("Workflow 執行中", "Workflow 執行期間不可開始錄製。")
            return
        if self.window_tracker.target is None:
            self._show_message("請先鎖定目標視窗", "請先使用 F8 鎖定前景視窗，再開始錄製。")
            return
        try:
            # reset UI counter
            self._recorded_event_count = 0
            self.recorder.start()
            self.recorder_overlay.start()
            self.last_recording = None
            self.last_recording_saved = False
            self.set_state(AppState.RECORDING)
            if self._macro_manager is not None:
                self._macro_manager.update_state("RECORDING", 0)
        except Exception as exc:
            self.logger.exception("開始錄製失敗")
            self._show_message("開始錄製失敗", str(exc))
            self.set_state(AppState.ERROR)

    def stop_recording(self):
        if self.state != AppState.RECORDING:
            return
        script = self.recorder.stop()
        self.recorder_overlay.stop()
        if script:
            # keep recording in memory but do not prompt save automatically
            self.last_recording = script
            self.last_recording_saved = False
            self._recorded_event_count = len(script.get("events", []))
            self.logger.info("Recording kept in memory; use 儲存錄製 to save to disk")
        self.set_state(AppState.IDLE)
        if self._macro_manager is not None:
            self._macro_manager.update_state("IDLE", len(script.get("events", [])) if script else 0)

    def save_recording(self):
        if not self.last_recording:
            self._show_message("沒有可儲存的錄製", "請先錄製一個腳本，再儲存。")
            return
        if self.last_recording_saved:
            self._show_message("錄製已儲存", "目前沒有新的錄製需要儲存。")
            return
        self._prompt_save_recording(self.last_recording)

    def open_macro_manager(self):
        if self._macro_manager is not None and self._macro_manager.isVisible():
            self._macro_manager.bring_to_front(); return
        self._macro_manager = MacroManager(
            self.script_store, self.start_recording, self.stop_recording,
            self.save_recording, self.open_script_folder, self.widget,
        )
        self._macro_manager.bring_to_front()

    def _prompt_save_recording(self, script):
        default_name = script.get("name", "recorded_script")
        name, ok = QInputDialog.getText(self._macro_manager or self.widget, "儲存錄製", "請輸入腳本名稱：", text=default_name)
        if not ok or not name.strip():
            self._show_message("儲存已取消", "錄製已儲存於暫存，請稍後再按儲存錄製。")
            return
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        safe_name = name.strip().replace(" ", "_")
        filename = f"{timestamp}_{safe_name}.json"
        script["name"] = name.strip()
        try:
            self.script_store.save_script(script, filename)
            self.last_recording_saved = True
            self._show_message("儲存完成", f"錄製腳本已儲存為 {filename}")
            self._load_scripts()
            self.widget.select_script(filename)
            self.logger.info(f"錄製儲存為 {filename}")
        except FileExistsError:
            answer = ask_confirmation(self._macro_manager or self.widget, "檔案已存在", "腳本檔案已存在，是否覆寫？")
            if answer == QMessageBox.Yes:
                path = self.script_store.scripts_dir / filename
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
                self.script_store.save_script(script, filename)
                self.last_recording_saved = True
                self._show_message("覆寫完成", f"錄製腳本已覆寫為 {filename}")
                self._load_scripts()
                self.widget.select_script(filename)
        except Exception as exc:
            self.logger.exception("儲存錄製失敗")
            self._show_message("儲存失敗", str(exc))

    def load_script(self, filename):
        try:
            self.current_script = self.script_store.load_script(filename)
            self.selected_script_name = filename
            self.widget.set_script_name(self.current_script.get("name", "(未選擇)"))
        except Exception as exc:
            self.logger.exception("載入腳本失敗")
            self._show_message("無法載入腳本", str(exc))
            self.current_script = None
            self.selected_script_name = None

    def open_script_folder(self):
        path = self.script_store.scripts_dir
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _ocr_probe_idle_blocker(self):
        if self.workflow_runner.is_active():
            return "Workflow is active"
        if self.player.is_active():
            return "Player is active"
        if self.trigger_runner is not None and self.trigger_runner.is_active():
            return "TriggerRunner is active"
        if self.state != AppState.IDLE:
            return f"Application state is {self.state.name}"
        if self.countdown_timer.isActive():
            return "Countdown is active"
        handoff = self.start_handoff.get_snapshot()
        if handoff.state in {
            StartHandoffState.ARMED,
            StartHandoffState.COMMITTING,
        }:
            return f"Start handoff is {handoff.state.value}"
        return None

    def _run_ocr_process_probe(self):
        """Run one direct capture/OCR observation with no runtime execution."""
        diagnostics = self.ocr_process_diagnostics
        if diagnostics is None or not diagnostics.enabled:
            return
        blocker = self._ocr_probe_idle_blocker()
        if blocker:
            self.logger.warning("OCR process probe rejected: %s", blocker)
            self.widget.set_ocr_process_probe_state(False, blocker)
            return
        if diagnostics.active:
            self.widget.set_ocr_process_probe_state(
                True, "An OCR process probe is already running"
            )
            return
        snapshot = self.target_session.refresh()
        if (
            snapshot is None
            or snapshot.connection_state != TargetConnectionState.ATTACHED
            or not snapshot.identity_valid
        ):
            message = "Lock a valid external target with F8 before running the probe"
            self.logger.warning("OCR process probe rejected: %s", message)
            self.widget.set_ocr_process_probe_state(False, message)
            return

        expected_session_id = snapshot.session_id
        expected_generation = snapshot.generation
        target_hwnd = int(snapshot.hwnd)
        compact_hwnd = int(self.widget.winId())
        self.widget.set_ocr_process_probe_state(True, "One OCR observation is running")

        def one_observation():
            current = self.target_session.refresh()
            if (
                current is None
                or current.session_id != expected_session_id
                or current.generation != expected_generation
                or current.connection_state != TargetConnectionState.ATTACHED
                or not current.identity_valid
            ):
                raise RuntimeError("Locked target changed before the OCR probe")
            image = self.text_detector.capture_client_image(
                target_hwnd,
                allow_desktop_fallback=False,
            )
            return self.text_detector.recognize_image(
                image,
                layout_hint="multi_line",
            )

        def worker():
            try:
                result = diagnostics.run_probe(
                    one_observation,
                    target_snapshot=snapshot,
                    compact_hwnd=compact_hwnd,
                )
            except Exception as exc:
                self.logger.exception("OCR process probe failed")
                self.ocr_process_probe_bridge.failed.emit(
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                self.ocr_process_probe_bridge.completed.emit(result)

        self._ocr_probe_thread = threading.Thread(
            target=worker,
            name="ScreenBotOcrOneShotProbe",
            daemon=True,
        )
        self._ocr_probe_thread.start()

    def _on_ocr_process_probe_completed(self, result):
        message = (
            f"Probe complete: variants={result.get('variant_count')} "
            f"log={result.get('jsonl')}"
        )
        self.logger.info("OCR_PROCESS_PROBE_COMPLETED %s", message)
        self.widget.set_ocr_process_probe_state(
            False, message, completed=True
        )

    def _on_ocr_process_probe_failed(self, message):
        self.logger.error("OCR_PROCESS_PROBE_FAILED %s", message)
        self.widget.set_ocr_process_probe_state(
            False, message, completed=True
        )

    def confirm_exit(self):
        result = ask_confirmation(self.widget, "確認退出", "確定要退出 ScreenBot 嗎？")
        if result == QMessageBox.Yes:
            self.shutdown()

    def shutdown(self):
        self.logger.info("關閉 ScreenBot")
        self.countdown_timer.stop()
        self.start_handoff_timer.stop()
        self._cancel_armed_start("application_shutdown")
        self.workflow_ui_timer.stop()
        self.stop_workflow()
        self.stop_text_trigger()
        if self.state == AppState.RECORDING:
            try:
                self.recorder.stop()
            except Exception:
                pass
        self.recorder_overlay.destroy()
        if self.player.is_active():
            self.player.stop()
        self.text_detector.close()
        target_relative_overlay = getattr(self, "target_relative_overlay", None)
        if target_relative_overlay is not None:
            target_relative_overlay.close()
        target_session = getattr(self, "target_session", None)
        if target_session is not None:
            target_session.clear("application_shutdown")
        self._unregister_hotkeys()
        pos = self.widget.pos()
        self.settings.set("bubble_position", [pos.x(), pos.y()])
        if self._workflow_editor is not None:
            self._workflow_editor.close()
        if self._workflow_debug_panel is not None:
            self._workflow_debug_panel.close()
        self.widget.close()
        if self.overlay_diagnostics is not None:
            self.overlay_diagnostics.close()
        if self.ocr_process_diagnostics is not None:
            self.ocr_process_diagnostics.close()
        if self.workflow_process_diagnostics is not None:
            self.workflow_process_diagnostics.close()
        self.app.quit()

    def configure_text_trigger(self, trigger_data):
        self.stop_text_trigger()
        self.trigger_runner = TriggerRunner(
            self.text_detector,
            self.script_store,
            self.player,
            trigger_data,
            logger=self.logger,
        )
        return self.trigger_runner

    def start_text_trigger(self, trigger_data):
        runner = self.configure_text_trigger(trigger_data)
        runner.start()
        return runner

    def stop_text_trigger(self):
        if self.trigger_runner is not None:
            self.trigger_runner.stop()
            self.trigger_runner = None

    def load_workflow(self, workflow_source):
        import json

        if isinstance(workflow_source, dict):
            data = workflow_source
        else:
            source = str(workflow_source)
            if source.endswith(".json") and not any(ch in source for ch in ("\\", "/")):
                data = self.workflow_store.load_workflow(source)
            else:
                path = Path(source)
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
        # Resolve only at the application boundary. The runner continues to
        # receive its established embedded-trigger/macro schema, while legacy
        # workflow files remain untouched on disk.
        resolved = self.workflow_resolver.resolve(data)
        self.workflow_data = resolved
        self.workflow_runner.load_workflow(resolved)
        self.workflow_ui_state = "IDLE"
        self.logger.info("Workflow loaded: %s", data.get("name"))
        self._update_workflow_runtime_ui()
        return data

    def start_workflow(self):
        """Arm an already resolved workflow rather than starting it immediately."""
        if self.workflow_data is None:
            raise RuntimeError("Workflow is not loaded")
        # Compatibility for existing isolated tests that deliberately construct
        # a partial coordinator without target safety or handoff services.
        if not hasattr(self, "start_handoff") or not hasattr(self, "input_safety_gate"):
            self._run_loaded_workflow()
            return
        self._arm_start_request(
            StartRequestKind.WORKFLOW,
            copy.deepcopy(self.workflow_data),
            self.workflow_data.get("name") or self.selected_workflow_filename or "工作流",
        )

    def _start_workflow_from_handoff(self, frozen_workflow, expected_target):
        self.workflow_data = copy.deepcopy(frozen_workflow)
        self.workflow_runner.load_workflow(self.workflow_data)
        self._run_loaded_workflow()

    def _run_loaded_workflow(self):
        if self.workflow_runner.is_active():
            raise RuntimeError("Workflow is already running")
        if self.state == AppState.RECORDING:
            raise RuntimeError("Cannot start workflow while recording")
        if self.window_tracker.target is None:
            raise RuntimeError("請先鎖定目標視窗")
        self.stop_text_trigger()
        self.workflow_ui_state = "STARTING"
        self._update_workflow_runtime_ui()
        self.workflow_diagnostics.mark_workflow_started(
            self.workflow_data,
            target=self.window_tracker.target,
            capture_backend=(self.text_detector.get_capture_runtime_diagnostics() or {}).get("selected_backend"),
        )
        diagnostics = getattr(self, "workflow_process_diagnostics", None)
        if diagnostics is not None and diagnostics.active:
            diagnostics.bind_run(self.workflow_diagnostics.current_run_id)
            diagnostics.stage(
                "WORKFLOW_RUNNER_START_REQUESTED",
                workflow_name=self.workflow_data.get("name"),
            )
        self.workflow_runner.start()
        # The runner snapshot becomes authoritative as soon as its worker
        # advances.  STARTING remains only as the short hand-off state.
        self.widget.set_workflow_running(True)
        self.logger.info("Workflow started: %s", self.workflow_data.get("name"))
        self._update_workflow_runtime_ui()

    def stop_workflow(self):
        if self._cancel_armed_start("start_request_cancelled"):
            return
        if not self.workflow_runner.is_active():
            # Stop is intentionally idempotent: no inactive run is created or
            # finalized merely because the user pressed the button again.
            self.workflow_ui_state = "IDLE"
            self.widget.set_workflow_running(False)
            self._update_workflow_runtime_ui()
            return
        self.workflow_ui_state = "STOPPING"
        self.workflow_diagnostics.mark_workflow_stop_requested()
        self._update_workflow_runtime_ui()
        # Let the requested STOPPING state paint before the bounded runner
        # join below.  This is a one-shot UI hand-off, never a poll action.
        QApplication.processEvents()
        self.workflow_runner.stop()
        self.workflow_ui_state = "STOPPED"
        self.workflow_diagnostics.mark_workflow_stopped()
        self.widget.set_workflow_running(False)
        self._restore_ui_after_workflow_execution("workflow_stopped")
        self.logger.info("Workflow stopped")
        self._update_workflow_runtime_ui()

    def _select_workflow(self, filename):
        self.selected_workflow_filename = filename

    def _refresh_workflows(self):
        items = self.workflow_store.list_workflows()
        self.widget.set_workflow_list(items)
        names = {item["filename"] for item in items}
        if self.selected_workflow_filename and self.selected_workflow_filename in names:
            self.widget.select_workflow(self.selected_workflow_filename)
            return
        if items:
            self.selected_workflow_filename = items[0]["filename"]
            self.widget.select_workflow(self.selected_workflow_filename)
        else:
            self.selected_workflow_filename = None

    def _start_workflow_from_ui(self):
        try:
            if self.workflow_runner.is_active():
                self._show_message("Workflow 執行中", "目前已有 Workflow 在執行。")
                return
            if self.window_tracker.target is None:
                self._show_message("請先鎖定目標視窗", "請先使用 F8 鎖定前景視窗，再啟動 Workflow。")
                return
            filename = self.selected_workflow_filename
            if not filename:
                self._show_message("請選擇 Workflow", "請先從下拉選單選擇 Workflow。")
                return
            diagnostics = getattr(self, "workflow_process_diagnostics", None)
            if diagnostics is not None and diagnostics.enabled:
                diagnostics.begin_trace(
                    workflow_identifier=filename,
                    target_snapshot=self.target_session.get_snapshot(),
                    compact_hwnd=int(self.widget.winId()),
                    quarantine_callback=self._on_workflow_diagnostic_quarantine,
                )
                diagnostics.stage("START_BUTTON_CLICKED")
            workflow = self.workflow_store.load_workflow(filename)
            if diagnostics is not None and diagnostics.active:
                diagnostics.stage("WORKFLOW_LOADED")
            resolved_workflow = self.workflow_resolver.resolve(workflow)
            if diagnostics is not None and diagnostics.active:
                diagnostics.stage("WORKFLOW_RESOLVED")
            self._arm_start_request(
                StartRequestKind.WORKFLOW,
                copy.deepcopy(resolved_workflow),
                resolved_workflow.get("name") or filename,
            )
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            self.logger.exception("啟動 Workflow 失敗")
            self.workflow_diagnostics.mark_workflow_error(f"Workflow Start Error: {exc}")
            self.workflow_diagnostics.finalize_run("FAILED")
            self.workflow_ui_state = "ERROR"
            self.widget.set_workflow_running(False)
            self._update_workflow_runtime_ui()
            self._show_message("Workflow 載入失敗", str(exc))
            diagnostics = getattr(self, "workflow_process_diagnostics", None)
            if diagnostics is not None and diagnostics.active:
                diagnostics.stage(
                    "WORKFLOW_START_FAILED",
                    exception_type=type(exc).__name__,
                    error=str(exc),
                )
                diagnostics.end_trace("workflow_start_failed")

    def _on_workflow_diagnostic_quarantine(self, reason, details):
        """Fail closed from a diagnostic worker without touching Qt."""
        diagnostics = getattr(self, "workflow_process_diagnostics", None)
        if diagnostics is not None and diagnostics.active:
            diagnostics.stage(
                "WORKFLOW_STOP_REQUESTED",
                source="diagnostic_input_quarantine",
                quarantine_reason=reason,
            )
        accepted = self.workflow_runner.request_immediate_stop(
            "diagnostic_input_quarantine",
            details,
        )
        if not accepted and not self.workflow_runner.is_active():
            diagnostics.end_trace("quarantine_before_runner_start")

    def _stop_workflow_from_ui(self):
        try:
            self.stop_workflow()
        except Exception as exc:
            self.logger.exception("停止 Workflow 失敗")
            self.workflow_diagnostics.mark_workflow_error(f"Workflow Stop Error: {exc}")
            self.workflow_diagnostics.finalize_run("FAILED")
            self.workflow_ui_state = "ERROR"
            self._update_workflow_runtime_ui()
            self._show_message("停止 Workflow 失敗", str(exc))

    def _export_runtime_log(self):
        try:
            path = self.export_runtime_log_file()
            self.workflow_diagnostics.print_timeline_to_console()
            self._show_message("匯出完成", f"Runtime Log 已匯出：{path.name}")
        except Exception as exc:
            self.logger.exception("匯出 Runtime Log 失敗")
            self._show_message("匯出失敗", str(exc))

    def export_runtime_log_file(self):
        """Export runtime log and return output path."""
        return self.workflow_diagnostics.export_runtime_log()

    def open_runtime_log_folder(self):
        runtime_dir = self.workflow_diagnostics.run_log_manager.runtime_dir
        runtime_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(runtime_dir)))

    def open_trigger_manager(self):
        if self._trigger_manager is not None and self._trigger_manager.isVisible():
            self._trigger_manager.bring_to_front()
            return
        self._trigger_manager = TriggerManager(self.trigger_store, self.workflow_store, self.window_tracker, self.text_detector, self._pause_global_hotkeys, self._resume_global_hotkeys)
        self._trigger_manager.bring_to_front()

    def _pause_global_hotkeys(self):
        if hasattr(self, "hotkey_manager"):
            self.hotkey_manager.unregister_all()

    def _resume_global_hotkeys(self):
        if hasattr(self, "hotkey_manager"):
            self.hotkey_manager.register_hotkeys()

    def open_workflow_manager(self):
        if self._workflow_manager is not None and self._workflow_manager.isVisible():
            self._workflow_manager.bring_to_front(); return
        self._workflow_manager = WorkflowManager(
            self.workflow_store, self.trigger_store, self.script_store,
            on_saved=self._refresh_workflows,
        )
        self._workflow_manager.bring_to_front()

    def open_workflow_editor(self):
        """Open the Workflow Editor window (single instance)."""
        if self._workflow_editor is not None and self._workflow_editor.isVisible():
            self._workflow_editor.raise_()
            self._workflow_editor.activateWindow()
            return
        self._workflow_editor = WorkflowEditor(
            self.workflow_store,
            self.script_store,
            trigger_store=self.trigger_store,
            get_running_filename=self._get_running_workflow_filename,
            logger=self.logger,
            window_tracker=self.window_tracker,
            text_detector=self.text_detector,
            is_workflow_running=self.workflow_runner.is_active,
        )
        self._workflow_editor.workflow_saved.connect(self._on_workflow_editor_saved)
        self._workflow_editor.show()
        self.logger.info("Workflow Editor opened")

    def _get_running_workflow_filename(self):
        """Return filename of the currently running workflow, or None."""
        if self.workflow_runner.is_active():
            return self.selected_workflow_filename
        return None

    def _on_workflow_editor_saved(self, filename):
        """Refresh the workflow list after the editor saves a file."""
        self._refresh_workflows()
        self.logger.info("Workflow list refreshed after editor saved: %s", filename)

    def open_runtime_debug_panel(self):
        """Open the read-only runtime debug panel (single instance)."""
        if self._workflow_debug_panel is not None and self._workflow_debug_panel.isVisible():
            self._workflow_debug_panel.raise_()
            return
        self._workflow_debug_panel = WorkflowDebugPanel(
            snapshot_provider=self.get_runtime_debug_snapshot,
            export_log_callback=self.export_runtime_log_file,
            logger=self.logger,
        )
        self._workflow_debug_panel.show()
        self.logger.info("Workflow Runtime Debug Panel opened")

    def get_runtime_debug_snapshot(self):
        """Collect a lightweight snapshot for runtime debugging UI."""
        snapshot = self.workflow_runner.get_runtime_snapshot()
        trigger_runtime = snapshot.get("trigger_runtime") or {}
        trigger_status = trigger_runtime.get("trigger_status") or {}
        capture_diagnostics = (
            snapshot.get("capture_runtime")
            or trigger_runtime.get("capture_diagnostics")
            or {}
        )
        summary = self.workflow_diagnostics.get_summary()

        runtime_state = snapshot.get("state") or self._resolve_workflow_ui_status(snapshot)

        step = self.workflow_runner.current_step or {}
        trigger = step.get("trigger") or {}
        region = trigger.get("region")
        region_text = "N/A"
        if isinstance(region, dict):
            region_text = (
                f"x={region.get('x_ratio')}, y={region.get('y_ratio')}, "
                f"w={region.get('width_ratio')}, h={region.get('height_ratio')}"
            )

        target_session = getattr(self, "target_session", None)
        target_snapshot = (
            target_session.get_snapshot() if target_session is not None else None
        )
        target = self.window_tracker.target
        target_hwnd = None
        target_title = None
        window_valid = None
        window_rect = None
        foreground_status = "N/A"
        if target is not None:
            target_hwnd = target.hwnd
            target_title = target.title
            window_valid = target.is_valid()
            window_rect = (
                f"{target.window_left},{target.window_top},"
                f"{target.window_width}x{target.window_height} "
                f"(client {target.client_left},{target.client_top},{target.client_width}x{target.client_height})"
            )
            try:
                fg_hwnd = ctypes.windll.user32.GetForegroundWindow()
                foreground_status = "FOREGROUND" if fg_hwnd == target.hwnd else f"BACKGROUND (fg={fg_hwnd})"
            except Exception:
                foreground_status = "N/A"
        if target_snapshot is not None:
            foreground_status = target_snapshot.visibility_state.value

        observation = trigger_status.get("observation") or {}
        trigger_state = "(未知)"
        stable_present = trigger_status.get("stable_present")
        candidate_present = trigger_status.get("candidate_present")
        candidate_count = trigger_status.get("candidate_count", 0) or 0
        confirm_frames = trigger_status.get("confirm_frames", 1) or 1
        if trigger_status.get("observation_state"):
            trigger_state = trigger_status["observation_state"]
        elif candidate_present is not None and candidate_count < confirm_frames:
            trigger_state = "CONFIRMING"
        elif stable_present is True:
            trigger_state = "PRESENT"
        elif stable_present is False:
            trigger_state = "ABSENT"

        last_error = summary.get("last_error") or snapshot.get("error")
        error_time = summary.get("last_error_time")
        error_source = summary.get("last_error_source")

        timeline_lines = self.workflow_diagnostics.get_recent_lines(30)
        current_step_index = snapshot.get("step_number", 0)
        if current_step_index <= 0:
            current_step_index = "N/A"

        stop_runtime = snapshot.get("stop_trigger_runtime") or {}
        stop_status = stop_runtime.get("status") or {}
        stop_state = "WAITING"
        if stop_runtime.get("matched"):
            stop_state = "MATCHED"
        elif stop_status.get("candidate_present") is not None and (stop_status.get("candidate_count", 0) < stop_status.get("confirm_frames", 1)):
            stop_state = "CONFIRMING"

        return {
            "workflow_name": snapshot.get("workflow_name"),
            "workflow_file": self.selected_workflow_filename,
            "runtime_state": runtime_state,
            "current_step_index": current_step_index,
            "total_steps": snapshot.get("total_steps", 0) or "N/A",
            "current_step_id": snapshot.get("step_id"),
            "loop_mode": snapshot.get("loop_mode"),
            "current_cycle": snapshot.get("current_cycle"),
            "completed_cycles": snapshot.get("completed_cycles"),
            "max_cycles": snapshot.get("max_cycles"),
            "restart_step": snapshot.get("restart_step"),
            "finish_reason": snapshot.get("finish_reason"),
            "trigger_type": trigger.get("type"),
            "trigger_event": snapshot.get("trigger_event"),
            "expected_text": snapshot.get("trigger_text"),
            "trigger_state": trigger_state,
            "observation_state": trigger_status.get("observation_state"),
            "exact_match": observation.get("exact_match"),
            "text_similarity": observation.get("text_similarity"),
            "readability_score": observation.get("readability_score"),
            "presence_score": observation.get("presence_score"),
            "observation_valid": observation.get("observation_valid"),
            "observation_reason": observation.get("reason"),
            "trigger_armed": trigger_status.get("armed"),
            "absent_frames": trigger_status.get("absent_frames"),
            "absent_duration_ms": trigger_status.get("absent_duration_ms"),
            "required_frames": trigger_status.get("required_frames"),
            "required_absent_duration_ms": trigger_status.get("required_absent_duration_ms"),
            "capture_backend": capture_diagnostics.get("capture_backend"),
            "capture_session_id": capture_diagnostics.get("capture_session_id"),
            "capture_session_recreate_count": capture_diagnostics.get("capture_session_recreate_count"),
            "screenbot_visibility_changed": capture_diagnostics.get("screenbot_visibility_changed"),
            "target_visibility_changed": capture_diagnostics.get("target_visibility_changed"),
            "foreground_window_changed": capture_diagnostics.get("foreground_window_changed"),
            "overlay_active": capture_diagnostics.get("overlay_active"),
            "overlay_create_count": capture_diagnostics.get("overlay_create_count"),
            "preview_window_recreated": capture_diagnostics.get("preview_window_recreated"),
            "poll_count": trigger_runtime.get("poll_count"),
            "confirm_progress": summary.get("confirm_progress"),
            "cooldown_remaining_ms": summary.get("cooldown_remaining_ms"),
            "last_ocr_text": trigger_runtime.get("last_ocr_text"),
            "last_ocr_timestamp": trigger_runtime.get("last_ocr_success_time"),
            "last_ocr_success": summary.get("last_ocr_success_time"),
            "ocr_region": region_text,
            "current_macro": snapshot.get("macro"),
            "player_active": self.player.is_active(),
            "player_state": "RUNNING" if self.player.is_active() else "IDLE",
            "last_macro_start": "N/A",
            "last_macro_finish": "N/A",
            "target_hwnd": target_hwnd,
            "target_title": target_title,
            "window_valid": window_valid,
            "window_rect": window_rect,
            "foreground_status": foreground_status,
            "target_session_id": (
                target_snapshot.session_id if target_snapshot is not None else None
            ),
            "target_generation": (
                target_snapshot.generation if target_snapshot is not None else None
            ),
            "target_connection_state": (
                target_snapshot.connection_state.value
                if target_snapshot is not None
                else "UNLOCKED"
            ),
            "target_visibility_state": (
                target_snapshot.visibility_state.value
                if target_snapshot is not None
                else "UNKNOWN"
            ),
            "target_pid": target_snapshot.pid if target_snapshot is not None else None,
            "target_root_hwnd": (
                target_snapshot.root_hwnd if target_snapshot is not None else None
            ),
            "target_client_hwnd": (
                target_snapshot.client_hwnd if target_snapshot is not None else None
            ),
            "target_client_size": (
                target_snapshot.current_client_size
                if target_snapshot is not None
                else None
            ),
            "target_dpi": target_snapshot.dpi if target_snapshot is not None else None,
            "target_executable_path": (
                target_snapshot.executable_path
                if target_snapshot is not None
                else None
            ),
            "target_process_creation_time": (
                target_snapshot.process_creation_time
                if target_snapshot is not None
                else None
            ),
            "target_window_class": (
                target_snapshot.window_class if target_snapshot is not None else None
            ),
            "target_identity_strength": (
                target_snapshot.identity_strength
                if target_snapshot is not None
                else None
            ),
            "target_identity_degraded_reason": (
                target_snapshot.identity_degraded_reason
                if target_snapshot is not None
                else None
            ),
            "target_disconnect_reason": (
                target_snapshot.disconnect_reason
                if target_snapshot is not None
                else None
            ),
            "target_locked_at_monotonic": (
                target_snapshot.locked_at if target_snapshot is not None else None
            ),
            "target_last_validated_at_monotonic": (
                target_snapshot.last_validated_at
                if target_snapshot is not None
                else None
            ),
            "last_error": last_error if last_error else "None",
            "error_timestamp": error_time,
            "error_source": error_source,
            "timeline_lines": timeline_lines,
            "stop_trigger_enabled": stop_runtime.get("enabled"),
            "stop_trigger_id": stop_runtime.get("trigger_id"),
            "stop_trigger_name": stop_runtime.get("trigger_name"),
            "stop_trigger_type": stop_runtime.get("trigger_type"),
            "stop_trigger_event": stop_runtime.get("trigger_event"),
            "stop_trigger_condition": stop_runtime.get("trigger_condition"),
            "stop_trigger_condition_label": stop_runtime.get("trigger_condition_label"),
            "stop_trigger_text": stop_runtime.get("trigger_text"),
            "stop_trigger_state": stop_state,
            "stop_confirm_progress": stop_runtime.get("confirm_progress"),
            "stop_last_ocr_text": stop_runtime.get("last_ocr_text"),
            "pending_stop": stop_runtime.get("pending_stop"),
            "stop_trigger_timestamp": stop_runtime.get("matched_time"),
            "stop_source": stop_runtime.get("stop_source") or snapshot.get("stop_source"),
            "stop_trigger_macro_cancel_requested": stop_runtime.get("macro_cancel_requested"),
        }

    def _build_text_state(self, trigger_runtime):
        if not trigger_runtime:
            return "(未知)"
        trigger_status = trigger_runtime.get("trigger_status") or {}
        stable_present = trigger_status.get("stable_present")
        candidate_count = trigger_status.get("candidate_count") or 0
        confirm_frames = trigger_status.get("confirm_frames") or 1
        candidate_present = trigger_status.get("candidate_present")
        if candidate_count < confirm_frames and candidate_present is not None:
            return f"確認中 {candidate_count} / {confirm_frames}"
        if stable_present is True:
            return "存在"
        if stable_present is False:
            return "不存在"
        return "(未知)"

    def _target_compact_status(self):
        target_session = getattr(self, "target_session", None)
        if target_session is not None:
            snapshot = target_session.get_snapshot()
            target = self.window_tracker.target
            if snapshot is None:
                return None, False
            return (
                target,
                snapshot.connection_state == TargetConnectionState.ATTACHED
                and snapshot.identity_valid,
            )
        target = self.window_tracker.target
        if target is None:
            return None, False
        try:
            valid = bool(target.is_valid()) if callable(getattr(target, "is_valid", None)) else bool(getattr(target, "hwnd", None))
        except Exception:
            valid = False
        return target, valid

    def _record_overlay_diagnostic(self, event, **data):
        diagnostics = getattr(self, "overlay_diagnostics", None)
        if diagnostics is None or not diagnostics.enabled:
            return
        diagnostics.record_event(
            event,
            interaction_id=diagnostics.current_interaction_id(),
            **data,
        )

    def _record_overlay_target_event(self, event, snapshot=None, **data):
        diagnostics = getattr(self, "overlay_diagnostics", None)
        if diagnostics is None or not diagnostics.enabled:
            return
        diagnostics.observe_target_event(event, snapshot, **data)

    def _on_overlay_target_session_event(self, event, snapshot, payload):
        event_map = {
            "TARGET_SESSION_CREATED": "TARGET_SESSION_CHANGED",
            "TARGET_STATE_CHANGED": "TARGET_VISIBILITY_CHANGED",
            "TARGET_DISCONNECTED": "TARGET_DISCONNECTED",
            "TARGET_SESSION_CLEARED": "TARGET_SESSION_CHANGED",
        }
        diagnostic_event = event_map.get(event)
        if diagnostic_event is not None:
            self._record_overlay_target_event(
                diagnostic_event,
                snapshot,
                target_session_event=event,
                target_payload=payload,
            )

    def _record_start_handoff_event(self, event, snapshot, request=None, authorization=None):
        request = request or snapshot.request
        authorization = authorization or object()
        auth_snapshot = getattr(authorization, "snapshot", None)
        data = {
            "request_id": getattr(request, "request_id", None),
            "kind": getattr(getattr(request, "kind", None), "value", None),
            "display_name": getattr(request, "display_name", snapshot.display_name),
            "expected_session_id": getattr(request, "target_session_id", None),
            "expected_generation": getattr(request, "target_generation", None),
            "current_session_id": getattr(auth_snapshot, "session_id", None),
            "current_generation": getattr(auth_snapshot, "generation", None),
            "locked_root_hwnd": getattr(auth_snapshot, "root_hwnd", None),
            "foreground_hwnd": getattr(authorization, "foreground_hwnd", 0),
            "foreground_root_hwnd": getattr(authorization, "foreground_root_hwnd", 0),
            "request_age_ms": (
                int(round((time.monotonic() - request.requested_at_monotonic) * 1000))
                if request is not None else None
            ),
            "remaining_ms": snapshot.remaining_ms,
            "handoff_state": snapshot.state.value,
            "start_outcome": event,
            "reason": snapshot.reason,
            "timestamp": time.time(),
        }
        recorder = getattr(self.workflow_diagnostics, "record_start_handoff_event", None)
        if callable(recorder):
            recorder(event, data)
        else:
            self.logger.info("%s %s", event, data)
        self._record_overlay_diagnostic(event, **data)
        diagnostics = getattr(self, "workflow_process_diagnostics", None)
        if diagnostics is not None and diagnostics.active:
            diagnostics.stage(event, **data)

    def _refresh_handoff_presentation(self):
        handoff_service = getattr(self, "start_handoff", None)
        if handoff_service is None:
            return None
        snapshot = handoff_service.get_snapshot()
        presenter = getattr(self.widget, "set_start_handoff_presentation", None)
        if callable(presenter):
            presenter(snapshot)
        self._refresh_compact_presentation()
        return snapshot

    def _build_compact_status_view_model(self, snapshot=None, workflow_info=None, countdown_text=None):
        """Compose UI-only compact status without changing any runtime state."""
        snapshot = snapshot or self.workflow_runner.get_runtime_snapshot()
        workflow_info = workflow_info or {}
        workflow_status = workflow_info.get("status") or self._resolve_workflow_ui_status(snapshot)
        target, target_valid = self._target_compact_status()
        target_title = getattr(target, "title", "") or "(未知目標)"
        if target is None:
            target_text = "目標：未鎖定"
        elif target_valid:
            target_text = f"目標：已鎖定「{target_title}」"
        else:
            target_text = f"目標：已失效「{target_title}」"

        error = workflow_info.get("last_error") or workflow_info.get("step_start_error") or snapshot.get("error")
        input_safety = snapshot.get("input_safety") or {}
        app_state = self.state.name if hasattr(self.state, "name") else str(self.state)
        cycle = workflow_info.get("current_cycle") or snapshot.get("current_cycle") or 0
        workflow_name = workflow_info.get("workflow_name") or snapshot.get("workflow_name") or "工作流"
        trigger_text = workflow_info.get("trigger_text") or snapshot.get("trigger_text") or "觸發條件"
        trigger_event = workflow_info.get("trigger_event") or snapshot.get("trigger_event") or ""
        macro = workflow_info.get("macro") or snapshot.get("macro") or "巨集"
        handoff_service = getattr(self, "start_handoff", None)
        handoff = handoff_service.get_snapshot() if handoff_service is not None else None

        if handoff is not None and handoff.state is StartHandoffState.ARMED:
            remaining_seconds = max(0, (handoff.remaining_ms + 999) // 1000)
            visual_state, status_text = "START_ARMED", "等待目標前景"
            operation_text = (
                f"目前操作：請切回「{target_title}」以開始執行｜剩餘：{remaining_seconds} 秒"
            )
        elif handoff is not None and handoff.state is StartHandoffState.COMMITTING:
            visual_state, status_text = "STARTING", "正在確認目標並啟動"
            operation_text = "目前操作：正在執行最終目標授權確認"
        elif handoff is not None and handoff.state is StartHandoffState.TIMED_OUT:
            visual_state, status_text = "INPUT_BLOCKED", "啟動未執行"
            operation_text = "目前操作：目標未在時間內回到前景"
        elif handoff is not None and handoff.state is StartHandoffState.TARGET_CHANGED:
            visual_state, status_text = "TARGET_INVALID", "啟動已取消"
            operation_text = "目前操作：鎖定目標已改變"
        elif handoff is not None and handoff.state is StartHandoffState.TARGET_UNAVAILABLE:
            visual_state, status_text = "TARGET_INVALID", "啟動已取消"
            operation_text = "目前操作：目標目前不可用"
        elif handoff is not None and handoff.state is StartHandoffState.FAILED:
            visual_state, status_text = "ERROR", "啟動未執行"
            operation_text = f"目前操作：{handoff.reason or '目標授權失敗'}"
        elif input_safety.get("blocked"):
            visual_state, status_text = "INPUT_BLOCKED", "輸入已阻擋"
            operation_text = f"目前操作：目標不是有效前景視窗（{input_safety.get('reason') or 'unknown'}）"
        elif error or workflow_status == "ERROR" or app_state == "ERROR":
            visual_state, status_text = "ERROR", "執行錯誤"
            operation_text = f"目前操作：{error or '未知錯誤'}"
        elif workflow_status == "STOPPING":
            visual_state, status_text = "STOPPING", "正在停止"
            operation_text = "目前操作：正在安全停止工作流"
        elif workflow_status == "RUNNING_MACRO":
            visual_state, status_text = "RUNNING_MACRO", "執行巨集中"
            operation_text = f"目前操作：正在執行「{macro}」｜循環：{cycle}"
        elif workflow_status == "WAIT_TRIGGER":
            if input_safety.get("suspended"):
                visual_state, status_text = "WAIT_TRIGGER", "等待觸發（輸入暫停）"
                operation_text = "目前操作：目標已鎖定；一般觸發暫停，Global Stop 仍在監測"
            else:
                visual_state, status_text = "WAIT_TRIGGER", "等待觸發"
                condition = f"{trigger_event}「{trigger_text}」" if trigger_event else f"「{trigger_text}」"
                operation_text = f"目前操作：等待{condition}｜循環：{cycle}"
        elif workflow_status == "STARTING":
            visual_state, status_text = "STARTING", "正在啟動工作流"
            operation_text = f"目前操作：正在準備「{workflow_name}」"
        elif app_state == "RECORDING":
            visual_state, status_text = "RECORDING", "錄製巨集中"
            operation_text = f"目前操作：已錄製 {self._recorded_event_count} 個有效操作"
        elif app_state == "COUNTDOWN":
            visual_state, status_text = "COUNTDOWN", "即將執行"
            seconds = countdown_text or (f"{self.countdown_value}s" if self.countdown_value else "")
            operation_text = f"目前操作：{seconds} 後開始".replace("  後", " 後")
        elif app_state == "RUNNING":
            visual_state, status_text = "STANDALONE_RUNNING", "執行腳本中"
            operation_text = f"目前操作：正在執行「{self.current_script.get('name') if self.current_script else '獨立腳本'}」"
        elif app_state == "PAUSED":
            visual_state, status_text = "PAUSED", "已暫停"
            operation_text = "目前操作：等待繼續或停止"
        elif target is not None and not target_valid:
            visual_state, status_text = "TARGET_INVALID", "目標已失效"
            operation_text = "目前操作：請重新聚焦目標並按 F8"
        elif target_valid:
            visual_state, status_text = "TARGET_READY", "目標已鎖定／待命"
            operation_text = f"目前操作：已鎖定「{target_title}」，尚未執行工作流"
            target_session = getattr(self, "target_session", None)
            target_snapshot = (
                target_session.get_snapshot()
                if target_session is not None
                else None
            )
            if target_snapshot is not None:
                visibility_labels = {
                    "FOREGROUND": "前景",
                    "BACKGROUND": "背景",
                    "MINIMIZED": "最小化",
                    "HIDDEN": "隱藏",
                    "UNKNOWN": "未知",
                }
                visibility = visibility_labels.get(
                    target_snapshot.visibility_state.value,
                    target_snapshot.visibility_state.value,
                )
                operation_text += f"｜目標狀態：{visibility}"
        else:
            visual_state, status_text = "NO_TARGET", "未鎖定目標"
            operation_text = "目前操作：請按 F8 鎖定目標視窗"

        return {
            "visual_state": visual_state,
            "target_text": target_text,
            "target_full_title": target_title if target is not None else "",
            "target_valid": target_valid,
            "status_text": status_text,
            "operation_text": operation_text,
            "recording_event_count": self._recorded_event_count,
        }

    def _refresh_compact_presentation(self, snapshot=None, workflow_info=None, countdown_text=None):
        view_model = self._build_compact_status_view_model(snapshot, workflow_info, countdown_text)
        renderer = getattr(self.widget, "render_compact_status", None)
        if callable(renderer):
            renderer(view_model)
        else:
            # Minimal compatibility for external test doubles; the live widget
            # always renders through render_compact_status().
            self.widget.set_target_title(view_model["target_text"])
        return view_model

    def _resolve_workflow_ui_status(self, snapshot):
        runner_state = snapshot.get("state", "IDLE")
        if getattr(self.workflow_runner, "last_error", None) or runner_state == "ERROR":
            return "ERROR"
        if self.workflow_ui_state == "STOPPING":
            return "STOPPING"
        if self.workflow_runner.is_active():
            return runner_state
        if runner_state == "FINISHED":
            return "FINISHED"
        if runner_state == "STOPPED" or self.workflow_ui_state == "STOPPED":
            return "STOPPED"
        if self.workflow_ui_state == "STARTING":
            return "STARTING"
        return "IDLE"

    def _update_workflow_runtime_ui(self):
        target_session = getattr(self, "target_session", None)
        if target_session is not None:
            try:
                target_session.refresh()
            except Exception:
                self.logger.exception("TargetSession periodic refresh failed")
        snapshot = self.workflow_runner.get_runtime_snapshot()
        self.workflow_diagnostics.update_from_snapshot(snapshot)
        trigger_runtime = snapshot.get("trigger_runtime") or {}
        status = self._resolve_workflow_ui_status(snapshot)
        summary = self.workflow_diagnostics.get_summary()
        elapsed_seconds = int(round(summary.get("elapsed_seconds", 0)))
        elapsed_text = f"{elapsed_seconds // 3600:02d}:{(elapsed_seconds % 3600) // 60:02d}:{elapsed_seconds % 60:02d}"
        trigger_status = trigger_runtime.get("trigger_status") or {}
        info = {
            "status": status,
            "workflow_name": snapshot.get("workflow_name"),
            "step_number": snapshot.get("step_number", 0),
            "total_steps": snapshot.get("total_steps", 0),
            "step_id": snapshot.get("step_id"),
            "trigger_text": snapshot.get("trigger_text"),
            "trigger_event": snapshot.get("trigger_event"),
            "macro": snapshot.get("macro"),
            "last_ocr_text": trigger_runtime.get("last_ocr_text", ""),
            "ocr_last_success_time": summary.get("last_ocr_success_time"),
            "text_state": self._build_text_state(trigger_runtime),
            "ocr_poll_count": summary.get("poll_count", 0),
            "ocr_success_count": summary.get("consecutive_success", 0),
            "ocr_failure_count": summary.get("consecutive_failure", 0),
            "trigger_state": "存在" if trigger_status.get("stable_present") is True else ("不存在" if trigger_status.get("stable_present") is False else "(未知)"),
            "confirm_progress": summary.get("confirm_progress", "0 / 0"),
            "cooldown_remaining_ms": summary.get("cooldown_remaining_ms", 0),
            "last_trigger_time": summary.get("last_trigger_time"),
            "macro_running": summary.get("macro_running", False),
            "macro_duration_ms": summary.get("last_macro_duration_ms", 0),
            "elapsed_text": elapsed_text,
            "poll_rate_hz": summary.get("poll_rate_hz", 0.0),
            "trigger_count": summary.get("trigger_count", 0),
            "macro_count": summary.get("macro_count", 0),
            "timeline_lines": self.workflow_diagnostics.get_recent_lines(5),
            "loop_mode": snapshot.get("loop_mode"),
            "current_cycle": snapshot.get("current_cycle"),
            "completed_cycles": snapshot.get("completed_cycles"),
            "max_cycles": snapshot.get("max_cycles"),
            "restart_step": snapshot.get("restart_step"),
            "finish_reason": snapshot.get("finish_reason"),
            "last_error": snapshot.get("error"),
            "step_start_error": snapshot.get("step_start_error"),
        }
        self.widget.set_workflow_runtime_info(info)
        self.widget.set_workflow_running(status in {"STARTING", "WAIT_TRIGGER", "RUNNING_MACRO", "STOPPING"})
        if (
            self.workflow_execution_ui_state == "RUNNING_COLLAPSED"
            and snapshot.get("state") in {"FINISHED", "STOPPED", "ERROR"}
        ):
            self._restore_ui_after_workflow_execution(snapshot.get("state", "terminal").lower())
        self._refresh_handoff_presentation()

    def _handle_player_error(self, reason):
        QTimer.singleShot(0, lambda: self._on_player_error(reason))

    def _on_player_error(self, reason):
        self.set_state(AppState.ERROR)
        self._show_message("播放錯誤", reason)
        self._restore_ui_after_workflow_execution("player_error")
        self.set_state(AppState.IDLE)

    def _handle_player_finished(self):
        QTimer.singleShot(0, self._on_player_finished)

    def _on_player_finished(self):
        if self.state in {AppState.RUNNING, AppState.PAUSED}:
            self._restore_ui_after_workflow_execution("player_finished")
            self.set_state(AppState.IDLE)

    def _show_message(self, title, text):
        show_info(self._macro_manager or self.widget, title, text)
