"""Manual desktop mail intake; drafts never enter the expense outbox."""
from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QTextEdit,
)

from expense_documents import net_invoice_issues, non_invoice_document_type
from local_mail import FetchWorker
from mail_import import ADDRESS, MailImportError, MailQueue, business_key
from mail_trial import TrialStore
from mail_viewer import MailViewer


class DesktopMailStore(TrialStore):
    def __init__(self, root: Path, catalog):
        queue = MailQueue(root)
        queue.close()
        self.catalog = catalog
        super().__init__(root, seed_test_objects=False)
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS desktop_handoffs (
                    document_id INTEGER PRIMARY KEY REFERENCES documents(id),
                    client_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('approved','queued')),
                    business_key TEXT
                )
            """)

    def objects(self):
        return list(self.catalog())

    def handoff(self, document_id):
        row = self.conn.execute(
            "SELECT * FROM desktop_handoffs WHERE document_id=?", (document_id,),
        ).fetchone()
        return dict(row) if row else None

    def approve(self, document_id, payload, revision):
        document, _ = self.document(document_id)
        fields = json.loads(document["result_json"]).get("fields", {})
        if (document["state"] != "requires_review" or fields.get("document_type") != "invoice"
                or non_invoice_document_type(fields.get("raw_text") or "")
                or payload.get("verification_status") != "confirmed"):
            raise MailImportError("Подтверждается только вручную проверенная обычная фактура.")
        rows = payload.get("allocations") or []
        if not rows or any(row.get("destination") != "object" or row.get("object_id") is None for row in rows):
            raise MailImportError("Выберите объект для каждой строки.")
        issues = net_invoice_issues(payload, rows)
        if issues:
            raise MailImportError(" ".join(issues))
        if self.handoff(document_id):
            raise MailImportError("Подтверждение уже сохранено. Используйте повтор передачи.")
        key = business_key(payload)
        if key and self.conn.execute(
            "SELECT 1 FROM desktop_handoffs WHERE business_key=?", (key,),
        ).fetchone():
            raise MailImportError(
                "В почтовой очереди уже подтверждена фактура с такими реквизитами. "
                "Сверьте существующую карточку: другой PDF не означает другую покупку."
            )
        draft = copy.deepcopy(payload)
        draft["verification_status"] = "draft"
        self.save(document_id, draft, revision)
        confirmed = copy.deepcopy(payload)
        confirmed["client_id"] = uuid.uuid4().hex
        confirmed["required_attachment_sha256"] = [document["sha256"]]
        with self.conn:
            self.conn.execute(
                "INSERT INTO desktop_handoffs VALUES (?,?,?,'approved',?)",
                (document_id, confirmed["client_id"], json.dumps(confirmed, ensure_ascii=False), key),
            )

    def enqueue(self, document_id, host):
        delivery = self.handoff(document_id)
        if delivery is None:
            raise MailImportError("Без ручного подтверждения передача запрещена.")
        if delivery["state"] == "queued":
            return delivery["client_id"]
        _, original = self.document(document_id)
        # Keep the queue original: the normal outbox removes its staged copy after ACK.
        staged = host._store_expense_originals([str(original)], delivery["client_id"])
        host.queue_expense_document_operation(
            delivery["client_id"], "upsert", json.loads(delivery["payload_json"]),
            attachment_paths=staged,
        )
        with self.conn:
            self.conn.execute(
                "UPDATE desktop_handoffs SET state='queued' WHERE document_id=?", (document_id,),
            )
        return delivery["client_id"]


class DesktopMailViewer(MailViewer):
    def __init__(self, host, root: Path):
        self.host = host
        store = DesktopMailStore(root, lambda: host._expense_server_objects()[0])
        super().__init__(root, store=store, test_mode=False)
        self.setParent(host, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.worker = None
        self.fetch_button = QPushButton("Проверить почту — последние 7 дней (только чтение)")
        self.fetch_button.clicked.connect(self.fetch_mail)
        self.layout().addWidget(self.fetch_button)
        retry = QPushButton("Повторить передачу уже подтверждённой фактуры")
        retry.clicked.connect(self.retry_selected)
        self.layout().addWidget(retry)
        self.status = QLabel("Автоматический опрос выключен. Пароль вводится локально и не сохраняется.")
        self.status.setWordWrap(True)
        self.layout().addWidget(self.status)
        self.report = QTextEdit()
        self.report.setReadOnly(True)
        self.layout().addWidget(self.report)

    def reload(self):
        super().reload()
        for index, document in enumerate(self.documents):
            delivery = self.store.handoff(document["id"])
            if delivery:
                self.table.item(index, 4).setText(
                    "Передан в реестр / очередь синхронизации" if delivery["state"] == "queued"
                    else "Подтверждён; требуется повтор передачи"
                )
            sources = self.store.conn.execute(
                "SELECT subject,filename,received_at FROM sources WHERE document_id=?",
                (document["id"],),
            ).fetchall()
            self.table.item(index, 0).setToolTip("\n".join(
                f"{row['received_at']} · {row['subject']} · {row['filename']}" for row in sources
            ))

    def open_document(self, document_id):
        if self.store.handoff(document_id):
            QMessageBox.information(
                self, "Уже подтверждено",
                "Повторный расход не создаётся. Откройте карточку в реестре "
                "или повторите передачу, если она не завершена.",
            )
            return False
        document, original = self.store.document(document_id)
        if document["state"] != "requires_review":
            raise MailImportError("Распознавание не завершено. Выполните явный повтор обработки.")
        fields = copy.deepcopy(json.loads(document["result_json"])["fields"])
        draft = self.store.latest(document_id)
        revision = draft["revision"] if draft else 0
        if not draft:
            fields["order_proposals"] = [
                {**row, "destination": "unselected", "object_id": None}
                for row in fields.get("order_proposals") or [{"net_amount": fields.get("net_amount")}]
            ]
        accepted = self._review_net_invoice(
            draft["payload"] if draft else {}, [str(original)], fields,
            trial_save=lambda payload: self.store.save(document_id, payload, revision),
            trial_objects=self.store.objects(),
            reviewed_save=lambda payload: self.store.approve(document_id, payload, revision),
            mail_queue=True,
        )
        if accepted:
            if self.store.handoff(document_id):
                self.transfer(document_id)
            self.reload()
        return accepted

    def transfer(self, document_id):
        self.store.enqueue(document_id, self.host)
        self.host.load_expense_documents()
        self.host.background_sync_with_cloud()
        self.status.setText(
            "Подтверждённая фактура передана в штатную очередь расходов. "
            "Состояние синхронизации и оригинал доступны в реестре."
        )

    def retry_selected(self):
        try:
            index = self.table.currentRow()
            if not 0 <= index < len(self.documents):
                raise MailImportError("Выберите документ.")
            self.transfer(self.documents[index]["id"])
            self.reload()
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            QMessageBox.warning(self, "Передача не завершена", str(error))

    def reprocess_selected(self):
        index = self.table.currentRow()
        if 0 <= index < len(self.documents) and self.store.handoff(self.documents[index]["id"]):
            QMessageBox.warning(self, "Уже подтверждено", "Откройте существующую карточку в реестре.")
            return
        super().reprocess_selected()

    def fetch_mail(self):
        password, accepted = QInputDialog.getText(
            self, "Почта — локальный ввод", f"Пароль {ADDRESS}:", QLineEdit.EchoMode.Password,
        )
        if not accepted or not password:
            return
        self.worker = FetchWorker(self.store.root, password, None)
        self.worker.finished_report.connect(self.fetched)
        self.worker.failed.connect(self.failed)
        self.worker.finished.connect(lambda: self.fetch_button.setEnabled(True))
        self.fetch_button.setEnabled(False)
        self.worker.start()

    def fetched(self, summary):
        self.reload()
        self.status.setText(
            f"Получение: писем {summary.messages_checked}; новых документов {summary.documents_added}; "
            f"повторов {summary.duplicates_skipped}; ошибок {summary.errors}. "
            "Распознавание — отдельно в таблице и отчёте ниже. Расходы автоматически не создавались."
        )
        self.report.setPlainText(json.dumps(summary.messages, ensure_ascii=False, indent=2))

    def failed(self, message):
        self.reload()
        self.status.setText("Получение не завершено: " + message)
        QMessageBox.warning(self, "Почта", message)

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "Почта", "Дождитесь завершения получения писем.")
            event.ignore()
            return
        super().closeEvent(event)
