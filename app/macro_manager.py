from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QPushButton
from app.manager_window_base import ManagerWindowBase


class MacroManager(ManagerWindowBase):
    def __init__(self, script_store, on_start, on_stop, on_save, on_open_folder, parent=None):
        super().__init__(parent); self.store=script_store
        self.on_start, self.on_stop, self.on_save, self.on_open_folder = on_start, on_stop, on_save, on_open_folder
        self.setWindowTitle("巨集管理"); self.resize(420, 360)
        root=QVBoxLayout(self); self.status=QLabel("目前狀態：IDLE"); self.events=QLabel("已錄製事件：0")
        root.addWidget(self.status); root.addWidget(self.events)
        row=QHBoxLayout(); self.start=QPushButton("開始錄製"); self.stop=QPushButton("停止錄製"); row.addWidget(self.start); row.addWidget(self.stop); root.addLayout(row)
        row=QHBoxLayout(); self.save=QPushButton("儲存巨集"); self.folder=QPushButton("開啟巨集資料夾"); row.addWidget(self.save); row.addWidget(self.folder); root.addLayout(row)
        root.addWidget(QLabel("巨集清單")); self.list=QListWidget(); root.addWidget(self.list)
        row=QHBoxLayout(); refresh=QPushButton("重新整理"); close=QPushButton("關閉"); row.addWidget(refresh); row.addWidget(close); root.addLayout(row)
        self.start.clicked.connect(self.on_start); self.stop.clicked.connect(self.on_stop); self.save.clicked.connect(self.on_save); self.folder.clicked.connect(self.on_open_folder); refresh.clicked.connect(self.refresh); close.clicked.connect(self.close); self.refresh()
    def refresh(self):
        self.list.clear()
        for filename, name in self.store.list_scripts(): self.list.addItem(name)
    def update_state(self, state, event_count=0):
        self.status.setText(f"目前狀態：{state}"); self.events.setText(f"已錄製事件：{event_count}"); self.refresh()
