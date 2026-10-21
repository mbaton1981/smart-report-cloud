import copy
import ast
import json
import os
import socket
import subprocess
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import test_catalog_sync as catalog_tests
from test_mail_automation import pdf
from offline_review import ReviewError, digest, packed
from incoming_documents import IncomingStore, enabled, open_incoming


class IncomingIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(catalog_tests.CatalogSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog_tests.CatalogSyncTests.tearDownClass.__func__)
    _add_company = catalog_tests.CatalogSyncTests._add_company

    def setUp(self):
        catalog_tests.CatalogSyncTests.setUp(self)
        self.addCleanup(self._case.cleanup)
        root = Path(self._case.name)
        self.source = root / "snapshot"
        originals = self.source / "intake" / "originals"
        originals.mkdir(parents=True)
        self.fields = {
            "document_type": "invoice", "supplier_name": "Isolated Supplier AB",
            "invoice_number": "TEST-123", "document_date": "2026-10-21",
            "due_date": None, "net_amount": "100.00", "vat_amount": "25.00",
            "rounding_amount": None, "gross_amount": "125.00", "amount_due": "125.00",
            "currency_code": "SEK", "payment_reference": "000123",
            "raw_text": "Faktura\nFakturanummer TEST-123",
        }
        drafts = []
        for index in (1, 2, 3):
            content = pdf(f"Isolated integration {index}")
            sha = digest(content)
            (originals / (sha + ".pdf")).write_bytes(content)
            fields = copy.deepcopy(self.fields)
            if index == 2:
                fields.update(document_type="receipt", invoice_number=None, amount_due=None,
                              raw_text="Kvitto\nBetalt med kort")
            if index == 3:
                fields["raw_text"] = "Nordea\nPaminnelse\nFaktura-/Avinummer\nTEST-123"
            drafts.append({"id": index, "sha256": sha, "original": sha + ".pdf",
                           "result": {"fields": fields, "state": "requires_review"}})
        (self.source / "result.json").write_text(packed({"drafts": drafts}), encoding="utf-8")
        conn = self.server.get_db_connection()
        from expense_documents import migrate_invoice_net_schema
        migrate_invoice_net_schema(conn)
        conn.commit()
        self.object_id = conn.execute("SELECT id FROM meta_objects WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()[0]
        conn.close()
        self.host = SimpleNamespace(_expense_objects=[{"id": self.object_id, "name": "Isolated object"}])
        self.host._incoming_costs_supported = True
        self.host.incoming_delivery_pending = lambda client_id: None
        self.host.incoming_register_snapshot = lambda: []
        self.host._store_expense_originals = self.stage
        self.host.queue_expense_document_operation = lambda *a, **kw: (
            self.desktop.SmartReportApp.queue_expense_document_operation(self.host, *a, **kw))
        self.store = IncomingStore(root / "incoming", self.host)
        self.addCleanup(self.store.close)
        self.store.import_snapshot(self.source)
        self.mode = None
        self.calls = []

    def stage(self, paths, client_id):
        folder = Path(self._case.name) / "staged" / client_id
        folder.mkdir(parents=True, exist_ok=True)
        results = []
        for source in paths:
            target = folder / Path(source).name
            target.write_bytes(Path(source).read_bytes())
            results.append(str(target))
        return results

    def approve(self, document_id=1):
        revision, payload = self.store.latest(document_id)
        payload.update(confirmed_type="receipt" if document_id == 2 else "invoice",
                       object_id=self.object_id, original_checked=True)
        return self.store.save(document_id, payload, revision)

    def request(self, method, url, headers=None, **kwargs):
        assert urlsplit(url).hostname == "isolated-testserver"
        self.calls.append((method, urlsplit(url).path))
        data = dict(kwargs.get("data") or {})
        if kwargs.get("files"):
            data["originals"] = [(BytesIO(stream.read()), filename, mime)
                                 for _, (filename, stream, mime) in kwargs["files"]]
            response = self.server_client.open(urlsplit(url).path, method=method, headers=headers, data=data)
        else:
            response = self.server_client.open(urlsplit(url).path, method=method, headers=headers,
                                               json=kwargs.get("json"))
        if method in ("POST", "PUT") and self.mode == "lost_ack":
            self.mode = None
            raise self.desktop.requests.ConnectionError("isolated lost acknowledgement")
        return SimpleNamespace(status_code=response.status_code, json=response.get_json)

    def sync(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")), \
                patch.object(self.desktop.requests, "request", side_effect=self.request):
            result = self.desktop.SmartReportApp._sync_expense_document_outbox(self.host)
        conn = self.desktop.get_db_connection()
        result["test_errors"] = [row[0] for row in conn.execute(
            "SELECT last_error FROM expense_document_outbox WHERE last_error IS NOT NULL")]
        conn.close()
        self.last_sync = result
        return result

    def test_default_off_has_no_profile_or_mail_or_network(self):
        from PyQt6.QtWidgets import QWidget
        host = QWidget()
        root = Path(self._case.name) / "disabled"
        with patch.dict(os.environ, {}, clear=False), \
                patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")):
            os.environ["SMARTREPORT_INCOMING_DOCUMENTS"] = "0"
            self.assertFalse(enabled())
            with self.assertRaisesRegex(ReviewError, "выключена"):
                open_incoming(host, root)
        self.assertFalse(root.exists())
        host.close()

    def test_invoice_normal_outbox_server_original_lost_ack_and_retry(self):
        revision = self.approve()
        card = self.store.transfer(1, revision)
        self.store.transfer(1, revision)
        conn = self.desktop.get_db_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM expense_document_outbox").fetchone()[0], 1)
        conn.close()
        self.mode = "lost_ack"
        self.assertEqual(self.sync()["synced"], 0)
        self.assertEqual(self.sync()["synced"], 1, self.last_sync)
        conn = self.server.get_db_connection()
        rows = conn.execute("SELECT * FROM expense_documents").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["client_id"], card["client_id"])
        self.assertEqual(rows[0]["payment_reference"], "000123")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM expense_allocations").fetchone()[0], 1)
        conn.close()
        response = self.server_client.get("/api/expense-documents",
                                         headers={"X-Sync-Key": "isolated-sync-test-key"})
        document = response.get_json()["documents"][0]
        self.assertTrue(document["has_original"])
        self.assertEqual(document["attachments"][0]["sha256"], card["sha256"])
        self.assertEqual(self.sync()["synced"], 0)

    def test_unsupported_server_keeps_receipt_and_retries_safe(self):
        self.host._incoming_costs_supported = False
        revision = self.approve(2)
        for _ in range(2):
            with self.assertRaisesRegex(ReviewError, "Сервер ещё не поддерживает"):
                self.store.transfer(2, revision)
        self.assertIsNone(self.store.card(2))
        conn = self.desktop.get_db_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM expense_document_outbox").fetchone()[0], 0)
        conn.close()
        # Verify the actual current server rejects a prepaid receipt, not just a UI assumption.
        payload = {**self.fields, "client_id": "isolated-receipt", "invoice_number": "",
                   "amount_due": None, "allocation_basis": "net", "verification_status": "confirmed",
                   "recognized_text": "Kvitto\nBetalt med kort", "allocations": []}
        response = self.server_client.post("/api/expense-documents", json=payload,
                                          headers={"X-Sync-Key": "isolated-sync-test-key"})
        self.assertEqual(response.status_code, 400)
        conn = self.server.get_db_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM expense_documents").fetchone()[0], 0)
        conn.close()

    def test_nordea_and_existing_register_duplicate_block_before_outbox(self):
        revision = self.approve(3)
        with self.assertRaises(ReviewError):
            self.store.transfer(3, revision)
        revision = self.approve(1)
        self.host.incoming_register_snapshot = lambda: [self.fields]
        with self.assertRaisesRegex(ReviewError, "уже есть"):
            self.store.transfer(1, revision)
        self.assertIsNone(self.store.card(1))

    def test_handoff_crash_then_retry_same_client_and_original(self):
        revision = self.approve()
        queue = self.host.queue_expense_document_operation

        def commit_then_fail(*args, **kwargs):
            queue(*args, **kwargs)
            raise OSError("isolated crash after queue commit")

        self.host.queue_expense_document_operation = commit_then_fail
        with self.assertRaises(OSError):
            self.store.transfer(1, revision)
        first = self.store.card(1)
        self.host.queue_expense_document_operation = queue
        self.assertEqual(self.store.transfer(1, revision)["client_id"], first["client_id"])
        self.assertEqual(self.sync()["synced"], 1, self.last_sync)

    def test_rollback_off_preserves_schema_queue_and_old_reader(self):
        conn = self.desktop.get_db_connection()
        schema = list(conn.execute("SELECT sql FROM sqlite_master ORDER BY name"))
        conn.close()
        revision = self.approve()
        card = self.store.transfer(1, revision)
        # Rollback is disabling the new entry point, not restoring a stale DB.
        with patch.dict(os.environ, {"SMARTREPORT_INCOMING_DOCUMENTS": "0"}):
            self.assertFalse(enabled())
            conn = self.desktop.get_db_connection()
            self.assertEqual([tuple(r) for r in schema],
                             [tuple(r) for r in conn.execute("SELECT sql FROM sqlite_master ORDER BY name")])
            saved = conn.execute("SELECT client_id FROM expense_document_outbox").fetchall()
            conn.close()
            self.assertEqual([r[0] for r in saved], [card["client_id"]])
        # Execute the previous release's actual sync function, not a mock of an old reader.
        source = subprocess.check_output(
            ["git", "--no-pager", "show", "38bf30d:app.py"], text=True, encoding="utf-8")
        tree = ast.parse(source)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SmartReportApp")
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_sync_expense_document_outbox")
        namespace = dict(vars(self.desktop))
        exec(compile(ast.Module(body=[method], type_ignores=[]), "<previous-release-sync>", "exec"), namespace)
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")), \
                patch.object(self.desktop.requests, "request", side_effect=self.request):
            result = namespace["_sync_expense_document_outbox"](self.host)
        self.assertEqual(result["synced"], 1, result)

    def test_integrated_window_receipt_blocked_and_no_background_requests(self):
        from PyQt6.QtWidgets import QMainWindow
        widget = QMainWindow()
        widget._expense_objects = self.host._expense_objects
        widget.refresh_incoming_capabilities = lambda: None
        widget._schedule_expense_read = lambda key: None
        widget.incoming_delivery_pending = lambda client_id: None
        root = Path(self._case.name) / "ui-profile"
        with patch.dict(os.environ, {"SMARTREPORT_INCOMING_DOCUMENTS": "1"}), \
                patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")), \
                patch.object(self.desktop.requests, "request", side_effect=AssertionError("No auto API calls")):
            window = open_incoming(widget, root)
            window.show()
            self.qt_app.processEvents()
            self.assertFalse(window.save_button.isEnabled())
            self.assertFalse(window.transfer_button.isEnabled())
            window.store.import_snapshot(self.source)
            window.close()
            window = open_incoming(widget, root)
            window.show()
            self.qt_app.processEvents()
            self.assertTrue(window.integrated)
            window.selector.setCurrentIndex(1)
            self.assertFalse(window.transfer_button.isEnabled())
            self.assertFalse(window.financial_panel.isVisible())
            window.expense_button.click()
            self.assertTrue(window.transfer_button.isEnabled())
            self.assertFalse(window.receipt_paid.isVisible())
            self.assertIsNone(window.object.currentData())
            window.close()
            widget.close()

    def test_ordinary_register_shows_pending_integrated_invoice(self):
        revision = self.approve()
        card = self.store.transfer(1, revision)
        host = SimpleNamespace(
            _expense_objects=self.host._expense_objects,
            exp_api_status_lbl=SimpleNamespace(setText=lambda _value: None))
        with patch.object(self.desktop, "request_expense_api", return_value={"documents": []}):
            documents, _online = self.desktop.SmartReportApp._expense_document_snapshot(host)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["client_id"], card["client_id"])
        self.assertEqual(documents[0]["allocations"][0]["object_id"], self.object_id)

    def test_actual_main_window_default_off_has_no_incoming_entry_or_profile(self):
        from PyQt6.QtWidgets import QPushButton
        root = Path(self._case.name) / "test-app-data"
        root.mkdir()
        window = None
        with patch.dict(os.environ, {"SMARTREPORT_INCOMING_DOCUMENTS": "0"}), \
                patch.object(self.desktop.desktop_auth, "app_data_dir", return_value=root), \
                patch.object(self.desktop.SmartReportApp, "_schedule_expense_read"), \
                patch.object(self.desktop.requests, "request", side_effect=self.request), \
                patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")):
            try:
                window = self.desktop.SmartReportApp()
                window.auto_sync_timer.stop()
                buttons = [button for button in window.findChildren(QPushButton)
                           if button.text() == "Входящие документы"]
                self.assertEqual(len(buttons), 1)
                self.assertTrue(buttons[0].isHidden())
                self.assertFalse((root / "incoming-documents").exists())
                self.assertIsNone(getattr(window, "_incoming_viewer", None))
            finally:
                if window:
                    window.close()
                    window.deleteLater()

    def test_due_date_provenance_not_silently_confirmed(self):
        base = self.store.base(1)
        base.update(due_date="2026-11-20", due_date_kind="calculated",
                    due_date_review_required=True, due_date_reviewed=False)
        with self.store.conn:
            self.store.conn.execute("UPDATE documents SET base_json=? WHERE id=1", (packed(base),))
        revision, payload = self.store.latest(1)
        payload.update(due_date="2026-11-20", object_id=self.object_id, confirmed_type="invoice",
                       original_checked=True)
        revision = self.store.save(1, payload, revision)
        self.assertIsNotNone(self.store.transfer(1, revision))
