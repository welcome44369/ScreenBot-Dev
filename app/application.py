import sys
import logging
import ctypes
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


class RecorderOverlayBridge(QObject):
    ripple_requested = Signal(dict)


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
        self.text_detector = TextDetector(
            self.window_tracker,
            self.logger,
            tesseract_cmd=self._resolve_tesseract_cmd(),
            lang=self.settings.get("ocr_lang", "eng+chi_tra"),
        )
        self.trigger_runner = None
        self.player = ScriptPlayer(self.window_tracker)
        self.player.on_error = self._handle_player_error
        self.player.on_finished = self._handle_player_finished
        self.workflow_runner = WorkflowRunner(self.text_detector, self.script_store, self.player, logger=self.logger)
        self.workflow_diagnostics = WorkflowDiagnostics(self.root_path, self.logger)
        self.workflow_data = None
        self.selected_workflow_filename = None
        self.workflow_ui_state = "IDLE"
        self._workflow_editor = None  # single editor instance guard
        self._workflow_debug_panel = None
        self._trigger_manager = None
        self._workflow_manager = None
        self._macro_manager = None

        self.app = QApplication([])
        self.app.setQuitOnLastWindowClosed(False)

        self.widget = FloatingWidget(self.root_path)
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

        self.recorder_overlay = RecorderOverlayController(self.window_tracker, self.logger)
        self.recorder_overlay_bridge = RecorderOverlayBridge()
        self.recorder_overlay_bridge.ripple_requested.connect(self._on_overlay_ripple_requested)

        # Recorder: create after widget so we can pass a Qt-safe callback
        def _on_event_count(count):
            # schedule update on Qt main thread
            QTimer.singleShot(0, lambda: self.widget.set_event_count(count))

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
        self.widget.set_status(state, countdown_text)
        self.widget.set_script_name(self.current_script.get("name") if self.current_script else "(未選擇)")
        self.widget.set_target_title(self.window_tracker.target.title if self.window_tracker.target else "(未鎖定)")
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

    def _handle_f8(self):
        self.logger.info("F8 熱鍵觸發")
        if self.workflow_runner.is_active():
            self.logger.info("Workflow 執行中，忽略 F8")
            return
        if self.state == AppState.IDLE:
            self._start_countdown()
        elif self.state in {AppState.RUNNING, AppState.PAUSED}:
            self.stop_script()
        elif self.state == AppState.COUNTDOWN:
            # cancel countdown
            self.countdown_timer.stop()
            self.set_state(AppState.IDLE)
        else:
            self.logger.debug("F8 在當前狀態無動作")

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
            self.widget.set_target_title(info.title)
            self.logger.info(f"鎖定視窗: {info.title} pid={info.pid}")
            self._start_script()
        except Exception as exc:
            self.logger.warning(f"鎖定失敗: {exc}")
            self.set_state(AppState.ERROR)
            self._show_message("無法鎖定目標視窗", str(exc))
            self.set_state(AppState.IDLE)

    def _start_script(self):
        if self.workflow_runner.is_active():
            self._show_message("Workflow 執行中", "請先停止 Workflow，再執行單獨腳本。")
            self.set_state(AppState.IDLE)
            return
        if self.current_script is None:
            self._show_message("腳本未選擇", "請先從控制面板選擇一個腳本。")
            self.set_state(AppState.IDLE)
            return
        try:
            self.player.start(self.current_script)
            self.set_state(AppState.RUNNING)
        except Exception as exc:
            self.logger.exception("啟動腳本失敗")
            self.set_state(AppState.ERROR)
            self._show_message("腳本啟動失敗", str(exc))
            self.set_state(AppState.IDLE)

    def stop_script(self):
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
            self.widget.set_event_count(0)
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
            self.widget.set_event_count(len(script.get("events", [])))
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

    def confirm_exit(self):
        result = ask_confirmation(self.widget, "確認退出", "確定要退出 ScreenBot 嗎？")
        if result == QMessageBox.Yes:
            self.shutdown()

    def shutdown(self):
        self.logger.info("關閉 ScreenBot")
        self.countdown_timer.stop()
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
        self._unregister_hotkeys()
        pos = self.widget.pos()
        self.settings.set("bubble_position", [pos.x(), pos.y()])
        if self._workflow_editor is not None:
            self._workflow_editor.close()
        if self._workflow_debug_panel is not None:
            self._workflow_debug_panel.close()
        self.widget.close()
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
        if self.workflow_data is None:
            raise RuntimeError("Workflow is not loaded")
        if self.workflow_runner.is_active():
            raise RuntimeError("Workflow is already running")
        if self.state == AppState.RECORDING:
            raise RuntimeError("Cannot start workflow while recording")
        if self.window_tracker.target is None:
            raise RuntimeError("請先鎖定目標視窗")
        self.stop_text_trigger()
        self.workflow_diagnostics.mark_workflow_started(
            self.workflow_data,
            target=self.window_tracker.target,
            capture_backend=(self.text_detector.get_capture_runtime_diagnostics() or {}).get("selected_backend"),
        )
        self.workflow_runner.start()
        self.workflow_ui_state = "WAIT_TRIGGER"
        self.widget.set_workflow_running(True)
        self.logger.info("Workflow started: %s", self.workflow_data.get("name"))
        self._update_workflow_runtime_ui()

    def stop_workflow(self):
        self.workflow_runner.stop()
        self.workflow_diagnostics.mark_workflow_stopped()
        self.workflow_ui_state = "STOPPED"
        self.widget.set_workflow_running(False)
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
            workflow = self.workflow_store.load_workflow(filename)
            self.load_workflow(workflow)
            self.start_workflow()
        except Exception as exc:
            self.logger.exception("啟動 Workflow 失敗")
            self.workflow_diagnostics.mark_workflow_error(f"Workflow Start Error: {exc}")
            self.workflow_diagnostics.finalize_run("FAILED")
            self.workflow_ui_state = "ERROR"
            self.widget.set_workflow_running(False)
            self._update_workflow_runtime_ui()
            self._show_message("Workflow 載入失敗", str(exc))

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
            "last_error": last_error if last_error else "None",
            "error_timestamp": error_time,
            "error_source": error_source,
            "timeline_lines": timeline_lines,
            "stop_trigger_enabled": stop_runtime.get("enabled"),
            "stop_trigger_type": stop_runtime.get("trigger_type"),
            "stop_trigger_event": stop_runtime.get("trigger_event"),
            "stop_trigger_text": stop_runtime.get("trigger_text"),
            "stop_trigger_state": stop_state,
            "stop_confirm_progress": stop_runtime.get("confirm_progress"),
            "stop_last_ocr_text": stop_runtime.get("last_ocr_text"),
            "pending_stop": stop_runtime.get("pending_stop"),
            "stop_trigger_timestamp": stop_runtime.get("matched_time"),
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

    def _resolve_workflow_ui_status(self, snapshot):
        if self.workflow_ui_state == "ERROR":
            return "ERROR"
        if self.workflow_runner.last_error:
            return "ERROR"
        if self.workflow_runner.state.name == "FINISHED":
            return "FINISHED"
        if self.workflow_runner.is_active():
            return snapshot.get("state", "WAIT_TRIGGER")
        if self.workflow_ui_state == "STOPPED":
            return "STOPPED"
        return "IDLE"

    def _update_workflow_runtime_ui(self):
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
        }
        self.widget.set_workflow_runtime_info(info)
        self.widget.set_workflow_running(self.workflow_runner.is_active())

    def _handle_player_error(self, reason):
        QTimer.singleShot(0, lambda: self._on_player_error(reason))

    def _on_player_error(self, reason):
        self.set_state(AppState.ERROR)
        self._show_message("播放錯誤", reason)
        self.set_state(AppState.IDLE)

    def _handle_player_finished(self):
        QTimer.singleShot(0, self._on_player_finished)

    def _on_player_finished(self):
        if self.state in {AppState.RUNNING, AppState.PAUSED}:
            self.set_state(AppState.IDLE)

    def _show_message(self, title, text):
        show_info(self._macro_manager or self.widget, title, text)
