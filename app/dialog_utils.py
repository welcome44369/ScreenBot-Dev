from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox


def show_message(parent, icon, title, text, buttons=QMessageBox.StandardButton.Ok):
    box = QMessageBox(parent)
    box.setIcon(icon); box.setWindowTitle(title); box.setText(text); box.setStandardButtons(buttons)
    box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    box.setWindowModality(Qt.WindowModality.ApplicationModal)
    box.raise_(); box.activateWindow()
    return box.exec()


def show_info(parent, title, text): return show_message(parent, QMessageBox.Icon.Information, title, text)
def show_warning(parent, title, text): return show_message(parent, QMessageBox.Icon.Warning, title, text)
def show_error(parent, title, text): return show_message(parent, QMessageBox.Icon.Critical, title, text)
def ask_confirmation(parent, title, text):
    return show_message(parent, QMessageBox.Icon.Question, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
