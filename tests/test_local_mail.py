import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import requests

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEvent, QSize
from PyQt6.QtWidgets import QApplication, QCheckBox, QDialog, QLabel, QMessageBox, QPushButton
from werkzeug.serving import make_server

from desktop_ui import ReportTable
from local_mail import LocalMailStore, LocalMailViewer
from mail_import import MailImportError, MailQueue, import_inbox
from test_mail_import import FakeIMAP, NOW, message


def invoice_fields():
    return {
        "document_type": "invoice", "supplier_name": "Local Test Supplier AB",
        "invoice_number": "LOCAL-NEW-1", "document_date": "2026-10-20",
        "due_date": "2026-11-19", "due_date_kind": "explicit",
        "currency_code": "SEK", "net_amount": "100.00", "vat_amount": "25.00",
        "gross_amount": "125.00", "rounding_amount": "0.00", "amount_due": "125.00",
        "allocation_basis": "net", "raw_text": "Invoice LOCAL-NEW-1",
    }


def recognized(paths):
    return {"fields": invoice_fields(), "state": "requires_review", "methods": ["test"], "errors": []}


class LocalMailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        # Credentials and both storage paths are isolated before importing the API.
        cls.bootstrap = tempfile.TemporaryDirectory(prefix="mail-flow-bootstrap-")
        cls.env = patch.dict(os.environ, {
            "DATABASE_PATH": str(Path(cls.bootstrap.name) / "bootstrap.db"),
            "SMARTREPORT_DOCUMENTS_DIR": str(Path(cls.bootstrap.name) / "documents"),
            "ADMIN_PASSWORD": "local-mail-test-only", "SECRET_KEY": "local-mail-test-only",
            "SYNC_API_KEY": "local-mail-test-only", "SMARTREPORT_UPGRADE_NET_SCHEMA": "1",
        })
        cls.env.start()
        import server
        cls.server = server

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.bootstrap.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="local-mail-flow-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "queue"
        queue = MailQueue(self.root)
        queue.close()
        (self.root / "local-test.json").write_text(
            json.dumps({"since": (NOW - timedelta(hours=2)).isoformat()}), encoding="utf-8",
        )
        self.paths = patch.dict(os.environ, {
            "SMARTREPORT_DOCUMENTS_DIR": str(Path(self.temp.name) / "api-originals"),
            "SMARTREPORT_UPGRADE_NET_SCHEMA": "1",
        })
        self.paths.start()
        self.addCleanup(self.paths.stop)
        db_patch = patch.object(self.server, "DB_FILE", str(Path(self.temp.name) / "server.db"))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        key_patch = patch.object(self.server, "SYNC_API_KEY", "local-mail-test-only")
        key_patch.start()
        self.addCleanup(key_patch.stop)
        self.server.init_cloud_db()
        self.store = LocalMailStore(self.root)
        self.addCleanup(lambda: self.store.close())
        with closing(self.server.get_db_connection()) as conn:
            for item in self.store.objects():
                conn.execute(
                    "INSERT INTO meta_objects (id,name,markning,company) VALUES (?,?,?,?)",
                    (item["id"], item["name"], item["markning"], item["company"]),
                )
            conn.commit()
        from reportlab.pdfgen.canvas import Canvas
        stream = io.BytesIO()
        pdf = Canvas(stream)
        pdf.drawString(40, 750, "LOCAL TEST ONLY - Invoice LOCAL-NEW-1")
        pdf.showPage()
        pdf.save()
        self.original = stream.getvalue()
        self.client = FakeIMAP({"9001": message(("new-invoice.pdf", self.original))})
        self.headers = {"X-Sync-Key": "local-mail-test-only"}
        self.http = make_server("127.0.0.1", 0, self.server.app)
        self.worker = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)
        self.url = f"http://127.0.0.1:{self.http.server_port}"

    def stop_server(self):
        self.http.shutdown()
        self.worker.join(timeout=10)
        self.http.server_close()

    def receive(self, recognizer=recognized):
        return import_inbox(
            self.store.queue, "test-only", now=NOW, since=NOW - timedelta(hours=2),
            client_factory=lambda *args, **kwargs: self.client, recognizer=recognizer,
        )

    def payload(self):
        return {
            **invoice_fields(), "verification_status": "confirmed",
            "recognized_text": "Invoice LOCAL-NEW-1", "expense_category": "Materials",
            "allocations": [{"destination": "object", "object_id": 102, "net_amount": "100.00"}],
        }

    def count(self):
        with closing(self.server.get_db_connection()) as conn:
            return conn.execute("SELECT COUNT(*) FROM expense_documents").fetchone()[0]

    def test_new_mail_draft_approval_original_and_response_loss_retry(self):
        before = self.receive()
        self.assertEqual(before.documents_added, 1)
        self.assertEqual(self.count(), 0)
        initial = self.store.document(1)[0]["result_json"]
        self.assertEqual(self.receive().duplicates_skipped, 1)
        self.assertEqual(self.store.document(1)[0]["result_json"], initial)
        draft = self.payload()
        draft["verification_status"] = "draft"
        self.store.save(1, draft)
        self.assertEqual(self.count(), 0)
        with self.assertRaisesRegex(MailImportError, "No manual approval"):
            self.store.send(1, self.url, self.headers)
        self.store.approve(1, self.payload(), 1)
        client_id = self.store.delivery(1)["client_id"]
        real_post = requests.post

        def response_lost(*args, **kwargs):
            with real_post(*args, **kwargs) as response:
                self.assertEqual(response.status_code, 201, response.text)
            raise requests.ConnectionError("synthetic response loss after commit")

        with patch("local_mail.requests.post", side_effect=response_lost):
            with self.assertRaises(requests.ConnectionError):
                self.store.send(1, self.url, self.headers)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.store.delivery(1)["state"], "pending")
        self.assertTrue(self.store.delivery(1)["last_error"])
        self.store.close()
        self.store = LocalMailStore(self.root)
        identifier = self.store.send(1, self.url, self.headers)
        self.assertEqual(self.store.delivery(1)["client_id"], client_id)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.store.delivery(1)["state"], "sent")
        self.assertEqual(self.store.send(1, self.url, self.headers), identifier)
        self.receive()
        self.assertEqual(self.count(), 1)
        with requests.get(f"{self.url}/api/expense-documents/{identifier}",
                          headers=self.headers, timeout=10) as response:
            response.raise_for_status()
            card = response.json()["document"]
        self.assertEqual(card["verification_status"], "confirmed")
        self.assertEqual(card["allocations"][0]["object_id"], 102)
        with requests.get(f"{self.url}/api/expense-documents/{identifier}/original",
                          headers=self.headers, timeout=10) as response:
            response.raise_for_status()
            restored = response.content
        self.assertEqual(hashlib.sha256(restored).hexdigest(), hashlib.sha256(self.original).hexdigest())
        from PyQt6.QtPdf import QPdfDocument
        downloaded = self.root / "downloaded.pdf"
        downloaded.write_bytes(restored)
        document = QPdfDocument(None)
        self.assertEqual(document.load(str(downloaded)), QPdfDocument.Error.None_)
        self.assertEqual(document.pageCount(), 1)
        self.assertFalse(document.render(0, QSize(500, 700)).isNull())
        document.close()

    def test_missing_object_and_non_invoice_cannot_be_approved(self):
        self.receive()
        for object_id in (None, 9999):
            payload = self.payload()
            payload["allocations"][0]["object_id"] = object_id
            with self.assertRaises(MailImportError):
                self.store.approve(1, payload, 0)
        for kind in ("order_confirmation", "tax_decision", "proforma", "unclassified"):
            result = recognized([])
            result["fields"]["document_type"] = kind
            with self.store.conn:
                self.store.conn.execute("UPDATE documents SET result_json=? WHERE id=1", (json.dumps(result),))
            with self.assertRaisesRegex(MailImportError, "ordinary invoice"):
                self.store.approve(1, self.payload(), 0)
        self.assertIsNone(self.store.delivery(1))
        self.assertEqual(self.count(), 0)

    def test_no_external_endpoint_even_after_confirmation(self):
        self.receive()
        self.store.approve(1, self.payload(), 0)
        for url in ("https://example.com", "http://localhost:5000", "http://127.0.0.1.evil.test"):
            with patch("local_mail.requests.post") as post:
                with self.assertRaises(MailImportError):
                    self.store.send(1, url, self.headers)
                post.assert_not_called()

    def test_approval_is_immutable_and_incomplete_ack_preserves_retry(self):
        self.receive()
        self.store.approve(1, self.payload(), 0)
        initial = self.store.delivery(1)
        with self.assertRaisesRegex(MailImportError, "Already approved"):
            self.store.approve(1, self.payload(), 1)
        with patch("local_mail.requests.post") as post:
            response = post.return_value
            response.is_redirect = False
            response.json.return_value = {"ok": True, "document": {"id": 1}}
            with self.assertRaisesRegex(MailImportError, "Incomplete server acknowledgement"):
                self.store.send(1, self.url, self.headers)
        self.assertEqual(self.store.delivery(1)["client_id"], initial["client_id"])
        self.assertEqual(self.store.delivery(1)["payload_json"], initial["payload_json"])
        self.assertEqual(self.store.delivery(1)["state"], "pending")
        self.assertEqual(self.count(), 0)

    def test_explicit_since_skips_previous_mail_and_preserves_results(self):
        self.client.dates["9001"] = NOW - timedelta(hours=3)
        self.assertEqual(self.receive().documents_added, 0)
        self.client.dates["9001"] = NOW - timedelta(hours=1)
        self.assertEqual(self.receive().documents_added, 1)
        snapshot = self.store.document(1)[0]["result_json"]
        with patch("mail_import.recognize_invoice", side_effect=AssertionError("No implicit reprocess")):
            self.assertEqual(self.receive(recognizer=lambda paths: self.fail("implicit reprocess")).duplicates_skipped, 1)
        self.assertEqual(self.store.document(1)[0]["result_json"], snapshot)
        with self.assertRaises(ValueError):
            import_inbox(self.store.queue, "test-only", now=NOW, since=NOW + timedelta(seconds=1))

    def test_ui_uses_saved_recognition_and_requires_owner_original_object_and_yes(self):
        self.receive()
        viewer = LocalMailViewer(self.root, self.url, self.headers)
        try:
            def only_draft(dialog):
                self.assertFalse(dialog.findChild(QPushButton, "invoiceConfirm").isEnabled())
                dialog.findChild(QPushButton, "invoiceSaveDraft").click()
                return QDialog.DialogCode.Accepted

            with patch.object(QDialog, "exec", only_draft):
                self.assertTrue(viewer.open_document(1))
            self.assertEqual(self.count(), 0)
            viewer.manual_approval.setChecked(True)

            def review(dialog):
                button = dialog.findChild(QPushButton, "invoiceConfirm")
                checked = dialog.findChild(QCheckBox, "invoiceOriginalReviewed")
                self.assertFalse(button.isEnabled())
                table = dialog.findChild(ReportTable, "invoiceAllocations")
                for row in range(table.rowCount()):
                    destination = table.cellWidget(row, 2)
                    destination.setCurrentIndex(destination.findData("object"))
                    objects = table.cellWidget(row, 3)
                    objects.setCurrentIndex(objects.findData(102))
                self.assertFalse(button.isEnabled())
                checked.setChecked(True)
                self.assertTrue(button.isEnabled(), dialog.findChild(QLabel, "invoiceIssues").text())
                with patch("invoice_review.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
                    button.click()
                self.assertIsNone(viewer.store.delivery(1))
                self.assertEqual(self.count(), 0)
                with patch("invoice_review.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                    button.click()
                return QDialog.DialogCode.Accepted

            with patch.object(QDialog, "exec", review):
                self.assertTrue(viewer.open_document(1))
            self.assertEqual(self.count(), 1)
            self.assertEqual(viewer.store.delivery(1)["state"], "sent")
            with patch.object(QDialog, "exec", return_value=QDialog.DialogCode.Rejected) as opened:
                viewer.table.selectRow(0)
                viewer.open_saved_original()
                opened.assert_called_once()
        finally:
            viewer.close()
            viewer.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.qt.processEvents()

    def test_ui_requests_latest_five_without_enabling_approval(self):
        viewer = LocalMailViewer(self.root, self.url, self.headers)
        try:
            with patch("local_mail.QInputDialog.getText", return_value=("local-only", True)), \
                    patch("local_mail.FetchWorker") as worker:
                viewer.fetch_mail()
            worker.assert_called_once_with(self.root, "local-only", None, latest_count=5)
            worker.return_value.start.assert_called_once_with()
            self.assertFalse(viewer.manual_approval.isChecked())
            worker.return_value.isRunning.return_value = False
            self.assertEqual(self.count(), 0)
        finally:
            viewer.close()
            viewer.deleteLater()

    def test_queue_explicitly_labels_order_without_reprocessing_or_expense(self):
        def order(paths):
            fields = invoice_fields()
            fields.update(document_type="order_confirmation", raw_text="ORDERBEKRÄFTELSE")
            return {"fields": fields, "state": "requires_review", "errors": []}
        self.receive(order)
        before = self.store.queue.review()
        viewer = LocalMailViewer(self.root, self.url, self.headers)
        try:
            with patch("mail_viewer.recognize_invoice") as recognize:
                viewer.reload()
                recognize.assert_not_called()
            self.assertEqual(viewer.table.item(0, 5).text(), "Подтверждение заказа — не фактура")
            self.assertEqual(viewer.table.item(0, 6).text(), "Подтверждение заказа — не фактура")
            self.assertEqual(self.store.queue.review(), before)
            self.assertEqual(self.count(), 0)
        finally:
            viewer.close()
            viewer.deleteLater()

    def test_ui_reopen_does_not_reextract_or_rewrite_saved_result(self):
        self.receive()
        viewer = LocalMailViewer(self.root, self.url, self.headers)
        try:
            snapshot = self.store.document(1)[0]["result_json"]
            with patch("mail_viewer.recognize_invoice", side_effect=AssertionError("unexpected reprocess")), \
                    patch.object(QDialog, "exec", return_value=QDialog.DialogCode.Rejected):
                self.assertFalse(viewer.open_document(1))
            self.assertEqual(self.store.document(1)[0]["result_json"], snapshot)
            self.assertEqual(self.count(), 0)
        finally:
            viewer.close()
            viewer.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.qt.processEvents()


if __name__ == "__main__":
    unittest.main()
