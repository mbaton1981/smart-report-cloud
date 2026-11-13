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
from qt_test_support import cleanup_new_top_level_widgets


class IncomingIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(catalog_tests.CatalogSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog_tests.CatalogSyncTests.tearDownClass.__func__)
    _add_company = catalog_tests.CatalogSyncTests._add_company

    def setUp(self):
        catalog_tests.CatalogSyncTests.setUp(self)
        self.addCleanup(self._case.cleanup)
        self.addCleanup(
            cleanup_new_top_level_widgets, self.qt_app, self._qt_widgets_before)
        cloud_url = patch.object(self.desktop, "CLOUD_URL", "http://isolated-testserver")
        cloud_url.start()
        self.addCleanup(cloud_url.stop)
        for module in (self.desktop, self.server):
            sync_key = patch.object(module, "SYNC_API_KEY", "isolated-sync-test-key")
            sync_key.start()
            self.addCleanup(sync_key.stop)
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

    def test_resaving_existing_object_cost_preserves_allocation_tax(self):
        from offline_review_ui import OfflineReviewWindow

        parts = [
            {"destination": "object", "object_id": self.object_id, "gross_amount": "50.00",
             "net_amount": "40.00", "vat_amount": "10.00", "rounding_amount": "0.00"},
            {"destination": "company", "object_id": None, "gross_amount": "75.00",
             "net_amount": "60.00", "vat_amount": "15.00", "rounding_amount": "0.00"},
        ]
        revision, payload = self.store.latest(1)
        payload.update(
            supplier_name="Isolated Supplier AB",
            invoice_number="TEST-123",
            document_date="2026-10-21",
            currency_code="SEK",
            net_amount="100.00",
            vat_amount="25.00",
            gross_amount="125.00",
            rounding_amount="0.00",
            object_id=self.object_id,
            allocations=parts,
            original_checked=True,
        )
        revision = self.store.save(1, payload, revision)
        card = self.store.transfer(1, revision)
        self.assertEqual(self.sync()["synced"], 1, self.last_sync)
        self.host.incoming_register_snapshot = lambda: self.server_client.get(
            "/api/expense-documents", headers={"X-Sync-Key": "isolated-sync-test-key"},
        ).get_json()["documents"]

        window = OfflineReviewWindow(self.store, integrated=True, document_id=1)
        try:
            window.inputs["vat_amount"].setText("24.00")
            changed_totals = window.payload()["allocations"][0]
            self.assertNotIn("net_amount", changed_totals)
            self.assertNotIn("vat_amount", changed_totals)
            self.assertNotIn("rounding_amount", changed_totals)
            window.inputs["vat_amount"].setText("25.00")

            resaved = window.payload()
            self.assertEqual(resaved["allocations"], parts)
            self.assertEqual(resaved["rounding_amount"], "0.00")
            self.store.save(1, resaved, window.revision)
            self.assertEqual(self.sync()["synced"], 1, self.last_sync)

            documents = self.server_client.get(
                "/api/expense-documents", headers={"X-Sync-Key": "isolated-sync-test-key"},
            ).get_json()["documents"]
            saved = next(row for row in documents if row["client_id"] == card["client_id"])
            self.assertEqual(
                [[part[key] for key in ("net_amount", "vat_amount", "rounding_amount", "gross_amount")]
                 for part in saved["allocations"]],
                [["40.00", "10.00", "0.00", "50.00"], ["60.00", "15.00", "0.00", "75.00"]],
            )
            _revision, reopened = self.store.current_review(1)
            self.assertEqual(
                [[part[key] for key in ("net_amount", "vat_amount", "rounding_amount", "gross_amount")]
                 for part in reopened["allocations"]],
                [["40.00", "10.00", "0.00", "50.00"], ["60.00", "15.00", "0.00", "75.00"]],
            )
        finally:
            window.close()

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

    def test_incoming_copy_of_existing_document_links_and_updates_instead_of_duplicate(self):
        # Серверная запись с тем же оригиналом уже есть (сценарий Beijer №260414428128).
        content = pdf("Isolated integration 1")
        created = self.server_client.post(
            "/api/expense-documents",
            headers={"X-Sync-Key": "isolated-sync-test-key"},
            data={"payload": json.dumps({
                **{key: self.fields[key] for key in (
                    "supplier_name", "invoice_number", "document_date", "currency_code",
                    "vat_amount", "gross_amount")},
                "client_id": "existing-client", "verification_status": "draft",
                "allocations": [],
            }), "originals": [(BytesIO(content), "invoice.pdf")]},
        )
        self.assertEqual(created.status_code, 201, created.get_json())
        server_id = created.get_json()["document"]["id"]
        self.host.incoming_register_snapshot = lambda: self.server_client.get(
            "/api/expense-documents", headers={"X-Sync-Key": "isolated-sync-test-key"},
        ).get_json()["documents"]

        # Открыть документ, заполнить назначение, сохранить черновик, открыть повторно.
        revision = self.approve(1)
        reopened_revision, reopened = self.store.latest(1)
        self.assertEqual(reopened_revision, revision)
        self.assertEqual(reopened["object_id"], self.object_id)

        # Подтверждение расхода связывает карточку с существующей записью, а не создаёт дубль.
        card = self.store.transfer(1, revision)
        self.assertEqual(card["client_id"], "existing-client")
        self.assertEqual(self.sync()["synced"], 1, self.last_sync)
        self.assertIn(("PUT", f"/api/expense-documents/{server_id}"), self.calls)

        conn = self.server.get_db_connection()
        rows = conn.execute(
            "SELECT id, client_id, document_type, verification_status, sync_version, is_deleted "
            "FROM expense_documents"
        ).fetchall()
        attachments = conn.execute(
            "SELECT COUNT(*) FROM expense_document_attachments WHERE document_id=?",
            (server_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], server_id)
        self.assertEqual(rows[0]["client_id"], "existing-client")
        self.assertEqual(rows[0]["document_type"], "invoice")
        self.assertEqual(rows[0]["verification_status"], "confirmed")
        self.assertEqual(rows[0]["is_deleted"], 0)
        self.assertGreater(rows[0]["sync_version"], 1)
        self.assertEqual(attachments, 1)

        # Повторное открытие после синхронизации показывает учтённую запись.
        _after_revision, after = self.store.current_review(1)
        self.assertEqual(after["server_version"], rows[0]["sync_version"])
        status, accounted = self.store.accounting_status(1)
        self.assertTrue(accounted, status)

    def test_nordea_and_existing_register_duplicate_block_before_outbox(self):
        revision = self.approve(3)
        with self.assertRaises(ReviewError):
            self.store.transfer(3, revision)
        revision = self.approve(1)
        self.host.incoming_register_snapshot = lambda: [self.fields]
        with self.assertRaisesRegex(ReviewError, "уже есть"):
            self.store.transfer(1, revision)
        self.assertIsNone(self.store.card(1))

    def test_receipt_type_survives_transfer_and_sync(self):
        revision = self.approve(2)
        card = self.store.transfer(2, revision)
        self.assertEqual(self.sync()["synced"], 1, self.last_sync)
        conn = self.server.get_db_connection()
        row = conn.execute(
            "SELECT document_type, verification_status FROM expense_documents WHERE client_id=?",
            (card["client_id"],),
        ).fetchone()
        conn.close()
        self.assertEqual(row["document_type"], "receipt")
        self.assertEqual(row["verification_status"], "confirmed")

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
        fixture = os.environ.get("SMARTREPORT_PREVIOUS_RELEASE_FIXTURE")
        source = (Path(fixture).read_text(encoding="utf-8") if fixture else subprocess.check_output(
            ["git", "--no-pager", "show", "38bf30d:app.py"], text=True, encoding="utf-8"))
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
            self.assertTrue(window.transfer_button.isEnabled())
            self.assertTrue(window.financial_panel.isVisible())
            self.assertFalse(window.receipt_paid.isVisible())
            self.assertEqual(window.allocations.rowCount(), 1)
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
                self.assertNotIn("Открыть входящие", window.exp_actions)
                self.assertFalse((root / "incoming-documents").exists())
                self.assertIsNone(getattr(window, "_incoming_viewer", None))
            finally:
                if window:
                    window.close()
                    window.deleteLater()

    def test_expense_workbench_search_selection_scroll_and_compact_layout(self):
        from PyQt6.QtCore import QDate, Qt
        root = Path(self._case.name) / "workbench-app-data"
        root.mkdir()
        window = None
        documents = []
        for index in range(24):
            confirmed = index == 1
            allocated = index == 2
            gross = 125 + index
            documents.append({
                "id": index + 1,
                "client_id": f"synthetic-expense-{index:02d}",
                "supplier_name": f"Synthetic Supplier {index:02d}",
                "invoice_number": f"DEMO-{index:03d}",
                "document_date": f"2026-11-{index + 1:02d}",
                "gross_amount": f"{gross}.00",
                "net_amount": f"{gross - 25}.00",
                "vat_amount": "25.00",
                "currency_code": "SEK",
                "verification_status": "confirmed" if confirmed else "requires_review",
                "allocations": ([{
                    "destination": "company", "object_id": None,
                    "object_name": "Общие расходы фирмы", "gross_amount": f"{gross}.00",
                }] if allocated else []),
                "unallocated_gross": "0.00" if allocated else f"{gross}.00",
                "has_original": False,
            })
        documents[18]["expense_category"] = "Long synthetic category " * 120
        with patch.dict(os.environ, {"SMARTREPORT_INCOMING_DOCUMENTS": "0"}), \
                patch.object(self.desktop.desktop_auth, "app_data_dir", return_value=root), \
                patch.object(self.desktop.SmartReportApp, "_schedule_expense_read"), \
                patch.object(self.desktop.requests, "request", side_effect=self.request):
            try:
                window = self.desktop.SmartReportApp()
                window.auto_sync_timer.stop()
                window._expense_register_snapshot = lambda *_args, **_kwargs: (documents, True)
                window.tabs.setCurrentIndex(3)
                window.resize(1240, 850)
                window.show()
                window.load_expense_documents()
                self.qt_app.processEvents()

                self.assertEqual(window.table_supplier_documents.rowCount(), 24)
                self.assertIn("SEK", window.exp_metrics["accounted"][0].text())
                self.assertIn("с НДС", window.exp_metrics["accounted"][1].text())
                self.assertIn("DEMO-000", window.table_supplier_documents.item(0, 2).text())
                self.assertEqual(window.exp_metrics["review"][0].text(), "SEK 3,150.00")

                window.exp_search.setText("DEMO-007")
                self.assertEqual(window.table_supplier_documents.rowCount(), 1)
                self.assertEqual(window.table_supplier_documents.item(0, 2).text().splitlines()[0],
                                 "Synthetic Supplier 07")
                window.exp_search.clear()
                window.exp_filter_object.setCurrentIndex(0)
                window.exp_filter_status.setCurrentIndex(
                    window.exp_filter_status.findData("all"))
                window.exp_filter_unallocated.setChecked(False)
                self.assertEqual(window.table_supplier_documents.rowCount(), 24,
                                 window.exp_api_status_lbl.text())
                row = 18
                self.assertIn("Synthetic Supplier 18",
                              window.table_supplier_documents.item(row, 2).text())
                window.table_supplier_documents.setCurrentCell(row, 1)
                self.qt_app.processEvents()
                scrollbar = window.table_supplier_documents.verticalScrollBar()
                scrollbar.setValue(scrollbar.maximum())
                selected_id = window._selected_expense_document(
                    window.table_supplier_documents, notify=False)["client_id"]
                scroll_value = scrollbar.value()
                detail_scrollbar = window.exp_detail_scroll.verticalScrollBar()
                detail_scrollbar.setValue(detail_scrollbar.maximum())
                detail_scroll_value = detail_scrollbar.value()
                self.assertGreater(detail_scrollbar.maximum(), 0)
                window.load_expense_documents()
                self.qt_app.processEvents()
                self.assertEqual(
                    window._selected_expense_document(
                        window.table_supplier_documents, notify=False)["client_id"],
                    selected_id,
                )
                self.assertEqual(scrollbar.value(), scroll_value)
                self.assertEqual(detail_scrollbar.value(), detail_scroll_value)
                self.assertIn("Synthetic Supplier 18", window.exp_detail_supplier.text())

                manager = self.qt_app._smartreport_theme
                original_theme = manager.name
                manager.name = "dark"
                manager.apply()
                self.assertEqual(manager.name, "dark")
                self.assertIn("#f6ad55", window.exp_metrics["review"][0].styleSheet())
                manager.name = "light"
                manager.apply()
                self.assertEqual(manager.name, "light")
                self.assertIn("#885c21", window.exp_metrics["review"][0].styleSheet())
                manager.name = original_theme
                manager.apply()

                window.exp_date_from.setDate(QDate(2026, 11, 30))
                window.exp_date_to.setDate(QDate(2026, 11, 1))
                self.assertEqual(window.table_supplier_documents.rowCount(), 0)
                self.assertIn("начало позже", window.exp_api_status_lbl.text())
                window.exp_date_from.setDate(QDate(2026, 11, 1))
                window.exp_date_to.setDate(QDate(2026, 11, 30))
                self.assertEqual(window.table_supplier_documents.rowCount(), 24)

                window.resize(760, 600)
                self.qt_app.processEvents()
                self.assertEqual(
                    window.expense_splitter.orientation(), Qt.Orientation.Vertical,
                    (window.width(), window.expense_splitter.width(),
                     window.expense_splitter.window() is window,
                     window.expense_splitter._compact_orientation,
                     window.expense_splitter._watched_window.width()),
                )
                self.assertTrue(window.exp_primary_action.isVisible())
                self.assertGreaterEqual(window.exp_detail_scroll.minimumWidth(), 260)
            finally:
                if window:
                    window.close()
                    window.deleteLater()

    def test_assign_action_saves_allocation_without_confirming_expense(self):
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QComboBox, QDialogButtonBox

        root = Path(self._case.name) / "assignment-app-data"
        root.mkdir()
        document = {
            "id": 41,
            "client_id": "synthetic-assignment-only",
            "supplier_name": "Isolated Supplier AB",
            "invoice_number": "DEMO-ASSIGN-001",
            "document_date": "2026-11-09",
            "gross_amount": "125.00",
            "net_amount": "100.00",
            "vat_amount": "25.00",
            "currency_code": "SEK",
            "verification_status": "requires_review",
            "allocations": [],
            "local_attachment_paths": [],
        }
        captured = []
        window = None
        with patch.dict(os.environ, {"SMARTREPORT_INCOMING_DOCUMENTS": "0"}), \
                patch.object(self.desktop.desktop_auth, "app_data_dir", return_value=root), \
                patch.object(self.desktop.SmartReportApp, "_schedule_expense_read"), \
                patch.object(self.desktop.requests, "request", side_effect=self.request):
            try:
                window = self.desktop.SmartReportApp()
                window.auto_sync_timer.stop()
                window._expense_register_snapshot = lambda *_args, **_kwargs: ([document], True)
                window.load_expense_documents()
                window.queue_expense_document_operation = (
                    lambda *args, **kwargs: captured.append((args, kwargs)) or 1)
                window.load_expense_documents = lambda *_args: None
                window.background_sync_with_cloud = lambda: None

                def fill_assignment_and_save():
                    dialog = self.qt_app.activeModalWidget()
                    status = dialog.findChild(QComboBox, "expenseVerificationStatus")
                    self.assertIsNotNone(status)
                    self.assertFalse(status.isEnabled())
                    destination = dialog.findChild(QComboBox, "expenseObject")
                    destination.setCurrentIndex(destination.findData("company"))
                    save = dialog.findChild(QDialogButtonBox).button(
                        QDialogButtonBox.StandardButton.Save
                    )
                    self.assertEqual(save.text(), "Сохранить назначение")
                    save.click()

                from PyQt6.QtWidgets import QMessageBox
                with patch.object(QMessageBox, "information"):
                    QTimer.singleShot(0, fill_assignment_and_save)
                    window.assign_selected_expense_document()

                self.assertEqual(len(captured), 1)
                payload = captured[0][0][2]
                self.assertEqual(payload["verification_status"], "requires_review")
                self.assertEqual(payload["gross_amount"], "125.00")
                self.assertEqual(payload["vat_amount"], "25.00")
                self.assertIsNone(payload["allocations"][0]["object_id"])
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
