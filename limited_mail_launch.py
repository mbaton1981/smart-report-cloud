"""One explicitly authorized local mail trial; no working SmartReport imports."""
from __future__ import annotations

import argparse
import imaplib
import json
import re
import secrets
import sys
import threading
import uuid
from pathlib import Path

from flask import Flask
from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QLabel, QLineEdit, QPushButton, QTextBrowser
from werkzeug.serving import WSGIRequestHandler, make_server

from limited_mail import quoted_folder, run_limited
from limited_mail_ui import ERROR_MESSAGES, PasswordDialog
from mail_intake import mailbox_key, register_mail_intake
from mail_worker import IntakeClient, WorkerError


class QuietHandler(WSGIRequestHandler):
    def log(self, type, message, *args):
        # No HTTP access output, receipt metadata or credential-bearing errors.
        return


def write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


class LocalTrial:
    def __init__(self, root: Path, *, client_factory=imaplib.IMAP4_SSL, replay=False):
        self.root, self.client_factory = root, client_factory
        self.result = None
        self.attempted = False
        self.replay = replay
        self.attempt_id = uuid.uuid4().hex
        if replay and (not (root / "selected-uids.json").is_file()
                       or not (root / "intake" / "intake.sqlite3").is_file()):
            raise WorkerError("replay_requires_existing_selection_and_queue")

    def output_path(self, name):
        return self.root / (f"replay-{self.attempt_id}-{name}" if self.replay else name)

    def run(self, host, username, folder, password):
        if self.attempted:
            raise WorkerError("one_manual_pass_only")
        self.attempted = True
        app = Flask("restricted-local-trial",
                    template_folder=str(Path(__file__).resolve().parent / "templates"))
        token = secrets.token_urlsafe(48)
        store = register_mail_intake(
            app, root=self.root / "intake", token=token, mailbox=mailbox_key(host, username, folder),
            enabled=True)
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        api = IntakeClient(f"http://127.0.0.1:{server.server_port}", token, allow_local=True)
        try:
            result = run_limited(api, host, username, password, folder,
                                 self.root / "selected-uids.json", client_factory=self.client_factory,
                                 replay_only=self.replay)
            drafts = api.call("GET", "/drafts")["drafts"]
            self.result = {**result, "status": store.status(), "drafts": drafts,
                           "expenses_created": 0}
            write_json(self.output_path("result.json"), self.result)
        finally:
            password, token = "", ""
            server.shutdown()
            thread.join()
            server.server_close()


class LocalTrialDialog(PasswordDialog):
    def __init__(self, trial):
        self.trial = trial
        super().__init__(lambda password: None, offline=False)
        self.setWindowTitle("SmartReport — один локальный импорт, только SmartReport-Test")
        self.notice.setText(
            "Разрешён один проход только папки SmartReport-Test (1–5 писем).\n"
            "Другие папки не открываются. Нет папки / больше пяти писем — остановка.\n"
            "Если папка ещё не подготовлена или требуется OAuth — нажмите «Отмена».\n"
            "Результат: отдельные локальные черновики, без расходов."
        )
        self.host = QLineEdit("mailcluster.loopia.se")
        self.username = QLineEdit("faktura@byggerochrenoverar.se")
        self.folder = QLineEdit("INBOX.SmartReport-Test")
        self.folder.setReadOnly(True)
        self.layout().insertWidget(1, QLabel(
            "Проверьте сервер и адрес: это параметры прежней настройки, не подтверждённый провайдер.\n"
            "Если они не ваши — исправьте. IMAPS: 993, проверка сертификата обязательна."))
        for index, (label, field) in enumerate((
            ("IMAP-сервер", self.host), ("Адрес ящика / логин", self.username), ("Точная папка", self.folder))):
            self.layout().insertWidget(2 + index * 2, QLabel(label))
            self.layout().insertWidget(3 + index * 2, field)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Импортировать до 5 писем — один раз")
        if trial.replay:
            self.setWindowTitle("SmartReport — разрешённый повтор только прежних UID")
            self.notice.setText("ТОЛЬКО ПОВТОР существующей тестовой очереди и сохранённого списка UID.\n"
                                "Новые письма не выбираются. Смена UIDVALIDITY — остановка.\n"
                                "Запускайте лишь после отдельного разрешения на реальное подключение.")
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Повторить только сохранённые UID")
        self.preview = QTextBrowser()
        self.preview.setMinimumHeight(220)
        self.preview.setOpenExternalLinks(False)
        self.layout().insertWidget(self.layout().count() - 1, self.preview)
        self.original = QPushButton("Открыть первый исходный PDF")
        self.original.setEnabled(False)
        self.original.clicked.connect(self.open_original)
        self.layout().insertWidget(self.layout().count() - 1, self.original)
        self.resize(740, 720)

    def accept(self):
        if self.worker is not None or self.trial.attempted:
            return
        host, username, folder = self.host.text().strip(), self.username.text().strip(), self.folder.text()
        if (not re.fullmatch(r"[A-Za-z0-9.-]+", host) or "." not in host
                or not username or any(ord(char) < 32 for char in username)):
            self.status.setText("Проверьте IMAP-сервер и адрес. Пароль не отправлен.")
            return
        quoted_folder(folder)
        self._operation = lambda password: self.trial.run(host, username, folder, password)
        super().accept()
        if self.worker is not None:
            self.host.setEnabled(False)
            self.username.setEnabled(False)

    def operation_finished(self):
        super().operation_finished()
        self._operation = lambda password: None
        self.password.setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Закрыть")
        if self.trial.result is not None:
            result = self.trial.result
            lines = [f"Получено выбранных писем: {len(result['messages'])}",
                     f"Черновиков: {len(result['drafts'])}. Создано расходов: 0."]
            for draft in result["drafts"]:
                fields = draft["result"]["fields"]
                lines.extend([
                    "", f"Черновик №{draft['id']}: {draft['recognition']['label']}",
                    f"Поставщик: {fields.get('supplier_name') or 'Не распознан'}",
                    f"Номер: {fields.get('invoice_number') or 'Не распознан'}",
                    f"Дата: {fields.get('document_date') or 'Не распознана'}",
                    f"К оплате: {fields.get('amount_due') or 'Не распознано'}",
                    f"Reference: {fields.get('payment_reference') or 'Не распознан'}",
                    f"Спорное совпадение: {'да' if draft['conflict'] else 'нет'}",
                ])
            self.preview.setPlainText("\n".join(lines))
            self.original.setEnabled(any(d["original"].endswith(".pdf") for d in result["drafts"]))

    def show_outcome(self, code):
        super().show_outcome(code)
        write_json(self.trial.output_path("outcome.json"), {
            "code": code,
            "message": ("Один повтор выбранных UID завершён." if self.trial.replay
                        else "Один проход завершён; повтор не запускался.") if code == "success"
            else ERROR_MESSAGES.get(code, ERROR_MESSAGES["internal"]),
        })

    def open_original(self):
        for draft in self.trial.result["drafts"]:
            if draft["original"].endswith(".pdf"):
                path = self.trial.root / "intake" / "originals" / draft["original"]
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                    self.status.setText("Системный просмотрщик не открыл PDF. Оригинал сохранён локально.")
                return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--authorized-single-pass", action="store_true")
    mode.add_argument("--authorized-replay", action="store_true")
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if not args.root.is_absolute():
        parser.error("An absolute isolated directory is required")
    if args.authorized_replay:
        marker = args.root / "isolation.json"
        if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8")) != {
                "mode": "single-folder-five-uids", "production": False}:
            parser.error("Replay requires the existing isolated trial marker")
    else:
        if args.root.exists():
            parser.error("First pass requires a new non-existing isolated directory")
        args.root.mkdir(parents=False)
        write_json(args.root / "isolation.json", {"mode": "single-folder-five-uids", "production": False})
    app = QApplication([sys.argv[0]])
    dialog = LocalTrialDialog(LocalTrial(args.root, replay=args.authorized_replay))
    dialog.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
