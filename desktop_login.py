"""Окно входа настольного клиента: проверка логина и пароля выполняется сервером."""
import os

from PyQt6.QtWidgets import (
    QApplication, QDialog, QFormLayout, QLabel, QLineEdit, QPushButton, QHBoxLayout, QVBoxLayout,
)
from PyQt6.QtCore import Qt

import desktop_auth


class LoginDialog(QDialog):
    def __init__(self, url, username=""):
        super().__init__()
        self.setWindowTitle("SmartReport — вход")
        self.setMinimumWidth(420)
        self.token = ""
        self.server_input = QLineEdit(url)
        self.user_input = QLineEdit(username)
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #fca5a5;")
        form = QFormLayout()
        form.addRow("Сервер", self.server_input)
        form.addRow("Логин", self.user_input)
        form.addRow("Пароль", self.password_input)
        self.login_button = QPushButton("Войти")
        self.login_button.setDefault(True)
        cancel = QPushButton("Отмена")
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.login_button)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        self.login_button.clicked.connect(self.submit)
        cancel.clicked.connect(self.reject)
        self.password_input.returnPressed.connect(self.submit)

    def submit(self):
        url = self.server_input.text().strip().rstrip("/")
        username = self.user_input.text().strip()
        password = self.password_input.text()
        if not url.lower().startswith(("https://", "http://127.0.0.1", "http://localhost")):
            self.status.setText("Адрес сервера должен начинаться с https://")
            return
        if not username or not password:
            self.status.setText("Введите логин и пароль.")
            return
        self.login_button.setEnabled(False)
        self.status.setStyleSheet("color: #cbd5e1;")
        self.status.setText("Подключение к серверу…")
        QApplication.processEvents()
        try:
            self.token = desktop_auth.login(url, username, password)
        except desktop_auth.AuthError as error:
            self.status.setStyleSheet("color: #fca5a5;")
            self.status.setText(str(error))
            self.login_button.setEnabled(True)
            self.password_input.clear()
            return
        desktop_auth.save_settings(server_url=url, username=username)
        self.password_input.clear()
        self.accept()


def ensure_login():
    """True, если клиент может работать: токен действует, сервер недоступен при сохранённом токене или задан тестовый ключ."""
    if os.environ.get("SYNC_API_KEY") or os.environ.get("SMARTREPORT_TRIAL_MODE") == "1":
        return True
    url = desktop_auth.server_url()
    saved = desktop_auth.load_saved_token()
    if saved:
        state = desktop_auth.check_token(url, saved)
        if state is not False:
            desktop_auth.set_token(saved, persist=False)
            return True
        desktop_auth.set_token("")
    dialog = LoginDialog(url, desktop_auth.load_settings().get("username", ""))
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return False
    desktop_auth.set_token(dialog.token)
    return True
