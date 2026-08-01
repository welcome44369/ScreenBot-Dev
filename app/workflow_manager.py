"""Simple one-trigger/one-macro workflow composer."""
from PySide6.QtWidgets import QFormLayout, QLineEdit, QComboBox, QSpinBox, QPushButton, QHBoxLayout
from app.dialog_utils import show_warning
from app.manager_window_base import ManagerWindowBase


class WorkflowManager(ManagerWindowBase):
    def __init__(self, workflow_store, trigger_store, macro_store, on_saved=None, parent=None):
        super().__init__(parent)
        self.workflow_store, self.trigger_store, self.macro_store = workflow_store, trigger_store, macro_store
        self.on_saved = on_saved
        self.setWindowTitle("工作流")
        form = QFormLayout(self)
        self.name_edit = QLineEdit()
        self.trigger_combo, self.macro_combo = QComboBox(), QComboBox()
        self.loop_combo = QComboBox()
        self.loop_combo.addItem("直到手動停止", "manual_stop")
        self.loop_combo.addItem("指定執行循環次數", "max_cycles")
        self.loop_combo.addItem("直到停止條件成立", "stop_trigger")
        self.max_cycles = QSpinBox(); self.max_cycles.setRange(1, 1000000); self.max_cycles.setValue(1)
        self.stop_combo = QComboBox(); self.stop_combo.addItem("(無)", None)
        self.stop_combo.setToolTip("僅偵測目前鎖定的目標視窗")
        form.addRow("工作流名稱", self.name_edit)
        form.addRow("觸發點", self.trigger_combo); form.addRow("巨集", self.macro_combo)
        form.addRow("循環模式", self.loop_combo); form.addRow("最大循環次數", self.max_cycles)
        form.addRow("停止條件", self.stop_combo)
        buttons = QHBoxLayout(); save, cancel = QPushButton("儲存"), QPushButton("取消")
        buttons.addWidget(save); buttons.addWidget(cancel); form.addRow(buttons)
        save.clicked.connect(self._save); cancel.clicked.connect(self.close)
        self.loop_combo.currentIndexChanged.connect(self._on_loop_mode_changed)
        self.refresh()

    def _on_loop_mode_changed(self):
        mode = self.loop_combo.currentData()
        self.max_cycles.setEnabled(mode == "max_cycles")
        self.stop_combo.setEnabled(mode == "stop_trigger")

    def refresh(self):
        self.trigger_combo.clear(); self.stop_combo.clear(); self.stop_combo.addItem("(無)", None)
        for item in self.trigger_store.list_triggers():
            self.trigger_combo.addItem(item["name"], item["id"]); self.stop_combo.addItem(item["name"], item["id"])
        self.macro_combo.clear()
        for filename, name in self.macro_store.list_scripts():
            self.macro_combo.addItem(name, filename[:-5] if filename.endswith(".json") else filename)
        self._on_loop_mode_changed()

    def _save(self):
        name = self.name_edit.text().strip()
        trigger_id, macro_id = self.trigger_combo.currentData(), self.macro_combo.currentData()
        if not name or not trigger_id or not macro_id:
            show_warning(self, "資料不足", "請填寫名稱、觸發點與巨集。"); return
        workflow = {"version": 2, "name": name, "steps": [{"id": "step_1", "trigger_ref": trigger_id, "macro_ref": macro_id}]}
        mode = self.loop_combo.currentData()
        loop = {"mode": mode, "restart_step": "step_1"}
        if mode == "max_cycles":
            loop["max_cycles"] = self.max_cycles.value()
        if mode == "stop_trigger":
            stop_ref = self.stop_combo.currentData()
            if not stop_ref:
                show_warning(self, "資料不足", "請選擇停止條件。"); return
            loop["stop_trigger_ref"] = stop_ref
        workflow["loop"] = loop
        try:
            self.workflow_store.save_workflow(workflow)
            if callable(self.on_saved): self.on_saved()
            self.close()
        except Exception as exc:
            show_warning(self, "Workflow 儲存失敗", str(exc))
