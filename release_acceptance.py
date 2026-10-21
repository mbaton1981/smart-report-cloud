"""One isolated, explicit release scenario, also runnable inside the frozen client."""
import hashlib
import json
import os
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

import requests
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication, QCheckBox, QDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QWidget

from desktop_mail import DesktopMailViewer
from desktop_ui import ReportTable
from mail_import import import_inbox


class ReplayIMAP:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def login(self, address, password):
        self.calls.append("login")
        return "OK", [b"test"]

    def select(self, folder, readonly=False):
        assert readonly and folder == "INBOX"
        self.calls.append("readonly")
        return "OK", [b"1"]

    def response(self, name):
        return name, [b"100"]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [b"1"]
        assert command == "FETCH" and args[0] == "1"
        if args[1] == "(RFC822.SIZE INTERNALDATE)":
            stamp = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%d-%b-%Y %H:%M:%S %z")
            return "OK", [f'1 (UID 1 RFC822.SIZE {len(self.raw)} INTERNALDATE "{stamp}")'.encode()]
        assert args[1] == "(BODY.PEEK[])"
        return "OK", [(b"BODY[]", self.raw), b")"]

    def logout(self):
        return "BYE", []


def verify_mail_release(app_module, work):
    import desktop_auth

    work = work.resolve()
    for value in (app_module.DB_FILE, os.environ["SMARTREPORT_DOCUMENTS_DIR"],
                  os.environ["SMARTREPORT_DATA_DIR"]):
        assert Path(value).resolve().is_relative_to(work), "Every path must be isolated under --work"
    assert os.environ["SYNC_API_KEY"], "Use a disposable local test key"
    raw = (work / "message.eml").read_bytes()
    mail = BytesParser(policy=policy.default).parsebytes(raw)
    original = next(mail.iter_attachments()).get_payload(decode=True)
    expected = json.loads((work / "expected.json").read_text(encoding="utf-8"))
    application = QApplication.instance() or QApplication([])
    app_module.init_db_once()
    with closing(app_module.get_db_connection()) as conn:
        conn.execute("CREATE TABLE sync_dataset(id INTEGER PRIMARY KEY, dataset_id TEXT NOT NULL)")
        conn.execute("INSERT INTO sync_dataset VALUES (1,'isolated-unified-release')")
        conn.commit()

    class Host(QWidget):
        _store_expense_originals = app_module.SmartReportApp._store_expense_originals
        queue_expense_document_operation = app_module.SmartReportApp.queue_expense_document_operation
        _sync_expense_document_outbox = app_module.SmartReportApp._sync_expense_document_outbox
        _expense_document_original_paths = app_module.SmartReportApp._expense_document_original_paths

        def __init__(self):
            super().__init__()
            self.sync_result = None
            self._expense_temp_directory = type("Directory", (), {"name": str(work / "download")})()
            Path(self._expense_temp_directory.name).mkdir()

        def _expense_server_objects(self):
            return app_module.request_expense_api("GET", "/api/expense-objects")["objects"], True

        def load_expense_documents(self):
            pass

        def background_sync_with_cloud(self):
            self.sync_result = self._sync_expense_document_outbox()

    def cards():
        return app_module.request_expense_api("GET", "/api/expense-documents")["documents"]

    host = Host()
    viewer = DesktopMailViewer(host, work / "mail")
    client = ReplayIMAP(raw)

    def receive():
        return import_inbox(
            viewer.store.queue, "test-only", latest_count=5,
            client_factory=lambda *args, **kwargs: client,
        )

    def check_fields(dialog):
        for key, value in expected.items():
            assert dialog.findChild(QLineEdit, "invoice_" + key).text() == value, key

    try:
        assert not cards(), "The API must start empty"
        receipt = receive()
        assert receipt.documents_added == 1 and receipt.errors == 0
        assert not cards()

        def draft(dialog):
            check_fields(dialog)
            assert not dialog.findChild(QPushButton, "invoiceConfirm").isEnabled()
            dialog.findChild(QPushButton, "invoiceSaveDraft").click()
            return QDialog.DialogCode.Accepted

        with patch.object(QDialog, "exec", draft):
            assert viewer.open_document(1)
        assert viewer.store.latest(1)["payload"]["verification_status"] == "draft"
        assert not cards()
        assert receive().duplicates_skipped == 1

        def confirm(dialog):
            check_fields(dialog)
            button = dialog.findChild(QPushButton, "invoiceConfirm")
            assert not button.isEnabled()
            table = dialog.findChild(ReportTable, "invoiceAllocations")
            for row in range(table.rowCount()):
                destination = table.cellWidget(row, 2)
                destination.setCurrentIndex(destination.findData("object"))
                objects = table.cellWidget(row, 3)
                objects.setCurrentIndex(objects.findData(102))
            assert not button.isEnabled()
            dialog.findChild(QCheckBox, "invoiceOriginalReviewed").setChecked(True)
            assert button.isEnabled(), dialog.findChild(QLabel, "invoiceIssues").text()
            with patch("invoice_review.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
                button.click()
            assert viewer.store.handoff(1) is None and not cards()
            with patch("invoice_review.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                button.click()
            return QDialog.DialogCode.Accepted

        real_request = requests.request
        lost = []

        def lose_ack(method, url, **kwargs):
            response = real_request(method, url, **kwargs)
            if method == "POST" and url.endswith("/api/expense-documents") and not lost:
                assert response.status_code == 201, response.text
                response.close()
                lost.append(True)
                raise requests.ConnectionError("Release test: response lost after commit")
            return response

        with patch.object(QDialog, "exec", confirm), patch("requests.request", side_effect=lose_ack):
            assert viewer.open_document(1)
        assert lost and len(cards()) == 1
        client_id = viewer.store.handoff(1)["client_id"]
        viewer.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        viewer = DesktopMailViewer(host, work / "mail")
        assert viewer.store.handoff(1)["client_id"] == client_id
        viewer.transfer(1)
        assert receive().duplicates_skipped == 1
        viewer.transfer(1)
        documents = cards()
        assert len(documents) == 1
        card = documents[0]
        card = app_module.request_expense_api("GET", f"/api/expense-documents/{card['id']}")["document"]
        assert card["client_id"] == client_id and card["verification_status"] == "confirmed"
        assert {row["object_id"] for row in card["allocations"]} == {102}
        for key, value in expected.items():
            actual = card.get(key)
            if key in ("amount_due", "rounding_amount"):
                from expense_documents import decimal_text
                actual = decimal_text(actual)
            assert actual == value, (key, actual, value)
        paths = host._expense_document_original_paths(card)
        assert len(paths) == 1
        digest = hashlib.sha256(original).hexdigest()
        assert hashlib.sha256(Path(paths[0]).read_bytes()).hexdigest() == digest
        assert viewer.store.document(1)[1].is_file(), "Queue original must survive outbox cleanup"
        from document_preview import DocumentGalleryDialog
        gallery = DocumentGalleryDialog(paths, "Restored card original")
        assert gallery._pages
        gallery.close()
        gallery.deleteLater()
        with closing(app_module.get_db_connection()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM expense_document_outbox").fetchone()[0] == 0
        def reopen_card(dialog):
            reference = dialog.findChild(QLineEdit, "invoice_payment_reference")
            assert reference is not None and reference.text() == expected["payment_reference"]
            dialog.reject()
            return QDialog.DialogCode.Rejected

        with patch.object(QDialog, "exec", reopen_card):
            assert not app_module.SmartReportApp._review_net_invoice(host, card, paths, {})
        return {
            "passed": True, "receipt": "isolated read-only IMAP replay, not live mailbox",
            "invoice": expected, "cards": 1, "object_id": 102,
            "draft_before_approval": True, "no_confirmation_creates_no_card": True,
            "confirmation": "test clicks through actual form, No then Yes",
            "retry_after_lost_ack": True, "repeated_mail_duplicates": 1,
            "original_sha256": digest, "original_opened_from_card": True,
            "desktop_mail_imported": True, "main_desktop_not_started": True,
            "standard_card_reference_visible": True,
            "paired_dataset_boundary_preserved": True,
            "profile": str(desktop_auth.app_data_dir()),
        }
    finally:
        viewer.close()
        host.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()
