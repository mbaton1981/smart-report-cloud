"""Ephemeral password prompt. The executable entry point is offline-only."""
from __future__ import annotations

import argparse
import imaplib
import ssl
import sys
from collections.abc import Callable

import requests
from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QLabel, QLineEdit, QVBoxLayout,
)

from mail_import import WorkerError
from folder_mail import FolderError


ERROR_MESSAGES = {
    "auth": "Вход отклонён или IMAP-сеанс прерван. Проверьте поддерживаемый способ авторизации. "
            "Настройки безопасности не изменены.",
    "tls": "Не удалось проверить сертификат сервера. Подключение остановлено; "
           "проверка сертификата не отключается.",
    "network": "Ошибка соединения. Операция остановлена.",
    "restricted": "Ограниченная проверка остановлена защитой папки, выбранных писем или локального API.",
    "internal": "Внутренняя ошибка. Операция остановлена; текст исключения скрыт для защиты пароля.",
    "folder": "Папку открыть не удалось: она отсутствует, недоступна или её точный путь отличается. "
              "Другие папки не проверялись. Подготовьте SmartReport-Test отдельно.",
    "count": "В выбранной папке должно быть от одного до пяти писем. Содержимое писем не загружалось.",
}


class PasswordOperation(QThread):
    outcome = pyqtSignal(str)

    def __init__(self, password: str, operation: Callable[[str], None], parent=None):
        super().__init__(parent)
        self._password = password
        self._operation = operation

    def run(self):
        password, self._password = self._password, ""
        try:
            self._operation(password)
        except ssl.SSLCertVerificationError:
            self.outcome.emit("tls")
        except imaplib.IMAP4.error:
            self.outcome.emit("auth")
        except (WorkerError, FolderError) as error:
            code = {
                "readonly_folder_failed": "folder",
                "folder_must_contain_one_to_five_messages": "count",
            }.get(str(error), str(error))
            self.outcome.emit(code if code in ERROR_MESSAGES else "restricted")
        except (requests.RequestException, OSError):
            self.outcome.emit("network")
        except Exception:
            # This UI boundary must never display/log raw exception strings or
            # tracebacks: a provider or callback can include the credential.
            self.outcome.emit("internal")
        else:
            self.outcome.emit("success")
        finally:
            password = ""
            del self._operation


class PasswordDialog(QDialog):
    def __init__(self, operation: Callable[[str], None], *, offline: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SmartReport — отдельная проверка пароля")
        self.setMinimumWidth(540)
        self._operation = operation
        self.worker = None
        layout = QVBoxLayout(self)
        self.notice = QLabel(
            "ОФЛАЙН-ПРОВЕРКА. Введите только вымышленный пароль.\n"
            "Соединение с почтой не выполняется."
            if offline else
            "Отдельная локальная проверка выбранной папки.\n"
            "Продолжайте только после согласования реального подключения."
        )
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.password.setInputMethodHints(
            Qt.InputMethodHint.ImhHiddenText | Qt.InputMethodHint.ImhSensitiveData
            | Qt.InputMethodHint.ImhNoPredictiveText
        )
        self.password.setPlaceholderText("Вымышленный пароль" if offline else "Пароль приложения / ящика")
        self.password.setAccessibleName("Пароль, скрытый ввод")
        layout.addWidget(self.password)
        self.status = QLabel("Пароль не сохраняется. После отправки или отмены поле очищается.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Проверить офлайн" if offline else "Выполнить согласованную проверку")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.password.returnPressed.connect(self.accept)
        layout.addWidget(self.buttons)

    def clear_password(self):
        # setText resets undo/redo history as well as the visible value.
        self.password.setText("")
        self.password.setModified(False)

    def accept(self):
        if self.worker is not None:
            return
        if not self.password.text():
            self.status.setText("Введите пароль или нажмите «Отмена».")
            return
        password = self.password.text()
        self.clear_password()
        self.password.setEnabled(False)
        self.buttons.setEnabled(False)
        self.status.setText("Выполняется проверка. Пароль скрыт и не записывается.")
        self.worker = PasswordOperation(password, self._operation, self)
        password = ""
        self.worker.outcome.connect(self.show_outcome)
        self.worker.finished.connect(self.operation_finished)
        self.worker.start()

    def show_outcome(self, code: str):
        self.status.setText(
            "Проверка завершена. Поле пароля очищено."
            if code == "success" else ERROR_MESSAGES.get(code, ERROR_MESSAGES["internal"]))

    def operation_finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.clear_password()
        self.password.setEnabled(True)
        self.buttons.setEnabled(True)

    def reject(self):
        self.clear_password()
        if self.worker is not None:
            self.status.setText("Дождитесь завершения текущей операции; повторный запуск заблокирован.")
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent):
        self.clear_password()
        if self.worker is not None:
            event.ignore()
            self.status.setText("Дождитесь завершения операции. Поле пароля очищено.")
        else:
            super().closeEvent(event)


def offline_operation(password: str) -> None:
    if not password:
        raise WorkerError("offline_password_missing")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-demo", action="store_true", required=True,
                        help="Only an offline dummy-password operation is available")
    parser.parse_args()
    app = QApplication([sys.argv[0]])
    dialog = PasswordDialog(offline_operation, offline=True)
    dialog.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
