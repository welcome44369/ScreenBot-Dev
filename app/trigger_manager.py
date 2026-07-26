"""Small CRUD dialog for independent Trigger resources.

The dialog deliberately delegates OCR execution and region capture to the
existing application/editor facilities; it only edits the Trigger resource.
"""
from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout, QListWidget, QPushButton, QMessageBox, QInputDialog, QDialog
from PySide6.QtCore import Qt
from app.trigger_wizard import TriggerWizard
from app.dialog_utils import show_warning
from app.manager_window_base import ManagerWindowBase


class TriggerManager(ManagerWindowBase):
    def __init__(self, trigger_store, workflow_store=None, window_tracker=None, text_detector=None, on_select_start=None, on_select_end=None, parent=None):
        super().__init__(parent)
        self.store = trigger_store
        self.workflow_store = workflow_store
        self.window_tracker, self.text_detector = window_tracker, text_detector
        self.on_select_start, self.on_select_end = on_select_start, on_select_end
        self.setWindowTitle("偵測觸發點")
        self.resize(420, 320)
        root = QVBoxLayout(self)
        self.list_widget = QListWidget()
        root.addWidget(self.list_widget)
        buttons = QHBoxLayout()
        for label, callback in (("新增", self._new), ("編輯", self._edit), ("刪除", self._delete), ("重新整理", self.refresh)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        root.addLayout(buttons)
        self.refresh()

    def refresh(self):
        self.list_widget.clear()
        for item in self.store.list_triggers():
            row = self.list_widget.addItem(item["name"])
            self.list_widget.item(self.list_widget.count() - 1).setData(32, item["id"])

    def _new(self):
        if self.window_tracker is not None and self.text_detector is not None:
            wizard = TriggerWizard(self.store, self.window_tracker, self.text_detector, self)
            if callable(self.on_select_start): self.on_select_start()
            wizard.setWindowModality(Qt.WindowModality.NonModal)
            wizard.finished.connect(lambda result: (self.refresh(), self.on_select_end() if callable(self.on_select_end) else None))
            wizard.show(); wizard.raise_(); wizard.activateWindow()
            return
        name, ok = QInputDialog.getText(self, "新增 Trigger", "名稱:")
        if not ok or not name.strip():
            return
        text, ok = QInputDialog.getText(self, "新增 Trigger", "偵測文字:")
        if not ok or not text.strip():
            return
        try:
            self.store.save_trigger({"version": 1, "name": name.strip(), "type": "text", "event": "appear", "text": text, "region": {"x_ratio": 0.0, "y_ratio": 0.0, "width_ratio": 1.0, "height_ratio": 1.0}})
            self.refresh()
        except Exception as exc:
            show_warning(self, "Trigger 儲存失敗", str(exc))

    def _edit(self):
        item = self.list_widget.currentItem()
        if item is None:
            return
        trigger_id = item.data(32)
        data = self.store.load_trigger(trigger_id)
        text, ok = QInputDialog.getText(self, "編輯 Trigger", "偵測文字:", text=data["text"])
        if ok and text.strip():
            data["text"] = text.strip()
            try:
                self.store.update_trigger(trigger_id, data)
                self.refresh()
            except Exception as exc:
                show_warning(self, "Trigger 更新失敗", str(exc))

    def _delete(self):
        item = self.list_widget.currentItem()
        if item is None:
            return
        try:
            self.store.delete_trigger(item.data(32), self.workflow_store)
            self.refresh()
        except Exception as exc:
            show_warning(self, "Trigger 無法刪除", str(exc))
