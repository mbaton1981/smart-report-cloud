"""Explicitly isolated mail-to-invoice test UI; never connects to the working API."""
from __future__ import annotations

import argparse
import copy
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QTextEdit,
)

from document_preview import DocumentGalleryDialog
from expense_documents import net_invoice_issues, non_invoice_document_type, sha256_file
from mail_import import ADDRESS, MailImportError, MailQueue, import_inbox, safe_error
from mail_trial import TrialStore
from mail_viewer import MailViewer


class LocalMailStore(TrialStore):
    def __init__(self, root: Path):
        marker = root / "local-test.json"
        if not marker.is_file():
            raise MailImportError("This is not an explicitly isolated mail test directory.")
        super().__init__(root)
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS local_deliveries (
                    document_id INTEGER PRIMARY KEY REFERENCES documents(id),
                    client_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','sent')),
                    server_id INTEGER,
                    last_error TEXT
                )
            """)

    def delivery(self, document_id):
        row = self.conn.execute(
            "SELECT * FROM local_deliveries WHERE document_id=?", (document_id,),
        ).fetchone()
        return dict(row) if row else None

    def approve(self, document_id, payload, expected_revision):
        document, _ = self.document(document_id)
        fields = json.loads(document["result_json"]).get("fields", {})
        if (document["state"] != "requires_review" or fields.get("document_type") != "invoice"
                or non_invoice_document_type(fields.get("raw_text") or "")):
            raise MailImportError("Only a recognized ordinary invoice can be confirmed.")
        rows = payload.get("allocations")
        if (payload.get("verification_status") != "confirmed"
                or not isinstance(rows, list) or not rows
                or any(row.get("destination") != "object" for row in rows)):
            raise MailImportError("Manual confirmation and object selection are required.")
        issues = net_invoice_issues(payload, rows)
        if issues:
            raise MailImportError(" ".join(issues))
        if self.delivery(document_id):
            raise MailImportError("Already approved: retry the existing delivery instead.")
        draft = copy.deepcopy(payload)
        draft["verification_status"] = "draft"
        # Draft validation uses the isolated catalog and checks money and dates.
        self.save(document_id, draft, expected_revision)
        payload = copy.deepcopy(payload)
        payload["client_id"] = uuid.uuid4().hex
        payload["required_attachment_sha256"] = [document["sha256"]]
        with self.conn:
            self.conn.execute(
                "INSERT INTO local_deliveries VALUES (?,?,?,'pending',NULL,NULL)",
                (document_id, payload["client_id"], json.dumps(payload, ensure_ascii=False)),
            )

    def send(self, document_id, url, headers):
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password:
            raise MailImportError("Only the isolated 127.0.0.1 test server is allowed.")
        delivery = self.delivery(document_id)
        if delivery is None:
            raise MailImportError("No manual approval: sending is forbidden.")
        if delivery["state"] == "sent":
            return delivery["server_id"]
        document, original = self.document(document_id)
        try:
            with original.open("rb") as source:
                response = requests.post(
                    url + "/api/expense-documents", headers=headers,
                    data={"payload": delivery["payload_json"]},
                    files={"original": (original.name, source, "application/octet-stream")},
                    timeout=30, allow_redirects=False,
                )
            with response:
                if response.is_redirect:
                    raise MailImportError("Redirect outside the isolated API is forbidden.")
                if not response.ok:
                    error = response.json()
                    raise MailImportError(
                        f"HTTP {response.status_code}: {error.get('error') or 'Test API rejected delivery'}"
                        + (f" (card {error['existing_document_id']})" if error.get("existing_document_id") else "")
                    )
                response.raise_for_status()
                result = response.json()
            card = result.get("document") or {}
            if (result.get("ok") is not True or type(card.get("id")) is not int
                    or card.get("client_id") != delivery["client_id"]
                    or card.get("verification_status") != "confirmed"
                    or not any(item.get("sha256") == document["sha256"] and item.get("has_original")
                               for item in card.get("attachments") or [])):
                raise MailImportError("Incomplete server acknowledgement; original and approval retained.")
        except (requests.RequestException, ValueError, OSError, MailImportError) as error:
            with self.conn:
                self.conn.execute(
                    "UPDATE local_deliveries SET last_error=? WHERE document_id=?",
                    (type(error).__name__, document_id),
                )
            raise
        with self.conn:
            self.conn.execute(
                "UPDATE local_deliveries SET state='sent',server_id=?,last_error=NULL WHERE document_id=?",
                (card["id"], document_id),
            )
        return card["id"]


class FetchWorker(QThread):
    finished_report = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, root, password, since, latest_count=None):
        super().__init__()
        self.root, self.password, self.since = root, password, since
        self.latest_count = latest_count

    def run(self):
        queue = None
        try:
            queue = MailQueue(self.root)
            summary = import_inbox(queue, self.password, since=self.since, latest_count=self.latest_count)
            self.finished_report.emit(summary)
        except Exception as error:
            self.failed.emit(safe_error(error))
        finally:
            self.password = ""
            if queue is not None:
                queue.close()


class LocalMailViewer(MailViewer):
    def __init__(self, root, url, headers):
        self.url, self.headers = url, headers
        super().__init__(root)
        self.store.close()
        self.store = LocalMailStore(root)
        self.setWindowTitle("SmartReport — локальная приёмка почты (НЕ рабочий сервер)")
        for label in self.findChildren(QLabel):
            if label.text().startswith("Только тестовые объекты."):
                label.setText("ЛОКАЛЬНЫЙ ТЕСТ: черновики остаются в очереди. Расход сохраняется "
                              "только после выбора объекта, проверки оригинала и явного подтверждения.")
        self.manual_approval = QCheckBox(
            "Разрешить подтверждение тестового расхода после согласования с владельцем"
        )
        self.manual_approval.setChecked(False)
        self.layout().addWidget(self.manual_approval)
        fetch = QPushButton("Проверить 5 последних писем (только чтение, пароль локально)")
        fetch.clicked.connect(self.fetch_mail)
        self.layout().addWidget(fetch)
        self.fetch_button = fetch
        retry = QPushButton("Повторить отправку уже подтверждённой фактуры")
        retry.clicked.connect(self.retry_selected)
        self.layout().addWidget(retry)
        original = QPushButton("Открыть оригинал из сохранённой тестовой карточки")
        original.clicked.connect(self.open_saved_original)
        self.layout().addWidget(original)
        self.status = QLabel("Будут проверены 5 последних писем INBOX, включая ранее полученные. "
                            "Расходы без подтверждения не создаются. Пароль не сохраняется.")
        self.status.setWordWrap(True)
        self.layout().addWidget(self.status)
        self.mail_report = QTextEdit()
        self.mail_report.setReadOnly(True)
        self.mail_report.setPlaceholderText("Здесь появится отчёт по каждому письму и вложению.")
        self.layout().addWidget(self.mail_report)
        self.fetch_worker = None
        self.reload()

    def reload(self):
        super().reload()
        if not isinstance(self.store, LocalMailStore):
            return
        for index, document in enumerate(self.documents):
            delivery = self.store.delivery(document["id"])
            if delivery:
                text = (f"Тестовая карточка №{delivery['server_id']}" if delivery["state"] == "sent"
                        else "Подтверждён; ожидает отправки")
                self.table.item(index, 4).setText(text)

    def fetch_mail(self):
        password, accepted = QInputDialog.getText(
            self, "Только локальный ввод", f"Пароль {ADDRESS} (не сохраняется):", QLineEdit.EchoMode.Password,
        )
        if not accepted or not password:
            return
        self.fetch_worker = FetchWorker(self.store.root, password, None, latest_count=5)
        self.fetch_worker.finished_report.connect(self.fetched)
        self.fetch_worker.failed.connect(self.fetch_failed)
        self.fetch_worker.finished.connect(lambda: self.fetch_button.setEnabled(True))
        self.fetch_button.setEnabled(False)
        self.status.setText("Чтение 5 последних писем и распознавание. Почтовый ящик не изменяется.")
        self.fetch_worker.start()

    def fetched(self, summary):
        self.reload()
        self.status.setText(f"Писем проверено: {summary.messages_checked}; добавлено: "
                            f"{summary.documents_added}; повторов: {summary.duplicates_skipped}; "
                            f"ошибок: {summary.errors}. Расходы не создавались.")
        self.mail_report.setPlainText(json.dumps(summary.messages, ensure_ascii=False, indent=2))

    def fetch_failed(self, message):
        self.reload()
        self.status.setText("Получение не завершено: " + message)
        QMessageBox.warning(self, "Ошибка получения", message)

    def open_document(self, document_id):
        if self.store.delivery(document_id):
            QMessageBox.information(self, "Документ уже подтверждён",
                                    "Используйте повтор отправки или открытие сохранённого оригинала.")
            return False
        document, original = self.store.document(document_id)
        result = json.loads(document["result_json"])
        fields = copy.deepcopy(result.get("fields", {}))
        if document["state"] != "requires_review":
            raise MailImportError("Распознавание не завершено; выполните явный повтор обработки.")
        draft = self.store.latest(document_id)
        revision = draft["revision"] if draft else 0
        if not draft:
            rows = fields.get("order_proposals") or [{"net_amount": fields.get("net_amount")}]
            fields["order_proposals"] = [
                {**row, "destination": "unselected", "object_id": None} for row in rows
            ]

        def save(payload):
            self.store.save(document_id, payload, revision)

        def approve(payload):
            self.store.approve(document_id, payload, revision)

        accepted = self._review_net_invoice(
            draft["payload"] if draft else {}, [str(original)], fields, trial_save=save,
            trial_objects=self.store.objects(),
            reviewed_save=approve if self.manual_approval.isChecked() else None,
        )
        if accepted:
            self.reload()
            if self.store.delivery(document_id):
                self.send_document(document_id)
        return accepted

    def send_document(self, document_id):
        try:
            identifier = self.store.send(document_id, self.url, self.headers)
        except (requests.RequestException, OSError, ValueError, MailImportError) as error:
            self.status.setText("Отправка не подтверждена; повтор безопасен с тем же client_id.")
            QMessageBox.warning(self, "Отправка не завершена", str(error))
            return
        self.status.setText(f"Сохранена только тестовая карточка №{identifier}.")
        self.reload()

    def selected_id(self):
        index = self.table.currentRow()
        if not 0 <= index < len(self.documents):
            raise MailImportError("Выберите строку документа.")
        return self.documents[index]["id"]

    def retry_selected(self):
        try:
            document_id = self.selected_id()
            if not self.store.delivery(document_id):
                raise MailImportError("Сначала требуется ручное подтверждение в форме.")
            self.send_document(document_id)
        except MailImportError as error:
            QMessageBox.warning(self, "Повтор отправки", str(error))

    def open_saved_original(self):
        try:
            document_id = self.selected_id()
            delivery = self.store.delivery(document_id)
            if not delivery or delivery["state"] != "sent":
                raise MailImportError("Карточка ещё не сохранена.")
            with requests.get(
                f"{self.url}/api/expense-documents/{delivery['server_id']}/original",
                headers=self.headers, timeout=30, allow_redirects=False,
            ) as response:
                if response.is_redirect:
                    raise MailImportError("Перенаправление вне тестового API запрещено.")
                response.raise_for_status()
                content = response.content
            row, original = self.store.document(document_id)
            directory = self.store.root / "card-originals"
            directory.mkdir(exist_ok=True)
            path = directory / original.name
            path.write_bytes(content)
            if sha256_file(path) != row["sha256"]:
                raise MailImportError("SHA-256 скачанного оригинала не совпал.")
            gallery = DocumentGalleryDialog([path], "Оригинал тестовой карточки", self)
            try:
                gallery.exec()
            finally:
                gallery.close()
                gallery.deleteLater()
        except (MailImportError, requests.RequestException, OSError) as error:
            QMessageBox.warning(self, "Оригинал", str(error))

    def closeEvent(self, event):
        if self.fetch_worker is not None and self.fetch_worker.isRunning():
            QMessageBox.information(self, "Получение писем", "Дождитесь завершения чтения почты.")
            event.ignore()
            return
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    options = parser.parse_args()
    root = options.data_dir.resolve()
    marker = root / "local-test.json"
    if root.exists() and not marker.exists() and any(root.iterdir()):
        parser.error("Use a NEW empty test directory, never an existing mail queue.")
    if not marker.exists():
        queue = MailQueue(root)
        queue.close()
        marker.write_text(json.dumps({"since": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    register = root / "register"
    register.mkdir(exist_ok=True)
    os.environ.update({
        "DATABASE_PATH": str(register / "local-register.db"),
        "SMARTREPORT_DOCUMENTS_DIR": str(register / "documents"),
        "SMARTREPORT_UPGRADE_NET_SCHEMA": "1",
        "ADMIN_PASSWORD": "local-mail-test-only", "SECRET_KEY": uuid.uuid4().hex,
        "SYNC_API_KEY": uuid.uuid4().hex,
    })
    # Import only after isolating every server path and test credential.
    import server
    from werkzeug.serving import make_server

    server.init_cloud_db()
    store = LocalMailStore(root)
    conn = server.get_db_connection()
    try:
        for item in store.objects():
            conn.execute("""
                INSERT OR IGNORE INTO meta_objects (id,name,markning,company,is_active)
                VALUES (?,?,?,?,1)
            """, (item["id"], item["name"], item["markning"], item["company"]))
        conn.commit()
    finally:
        conn.close()
        store.close()
    http = make_server("127.0.0.1", 0, server.app)
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    application = QApplication([])
    viewer = LocalMailViewer(root, f"http://127.0.0.1:{http.server_port}",
                             {"X-Sync-Key": os.environ["SYNC_API_KEY"]})
    viewer.show()
    print(f"LOCAL MAIL TEST READY http://127.0.0.1:{http.server_port} root={root}", flush=True)
    try:
        return application.exec()
    finally:
        http.shutdown()
        worker.join(timeout=10)
        http.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
