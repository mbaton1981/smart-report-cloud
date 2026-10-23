import os
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
from werkzeug.security import generate_password_hash


class CatalogSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._previous_env = {
            key: os.environ.get(key)
            for key in (
                "DATABASE_PATH", "ADMIN_PASSWORD", "SECRET_KEY", "SYNC_API_KEY",
                "SMARTREPORT_DB_FILE", "SMARTREPORT_DOCUMENTS_DIR",
                "SMARTREPORT_MAIL_SETTINGS_FILE", "SMARTREPORT_CLOUD_URL",
                "SMARTREPORT_TRIAL_MODE", "QT_QPA_PLATFORM",
            )
        }
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        cls._bootstrap = tempfile.TemporaryDirectory(prefix="smartreport-sync-import-")
        bootstrap = Path(cls._bootstrap.name)
        os.environ.update({
            "DATABASE_PATH": str(bootstrap / "server.db"),
            "ADMIN_PASSWORD": "isolated-test-only",
            "SECRET_KEY": "isolated-test-only",
            "SYNC_API_KEY": "isolated-sync-test-key",
            "SMARTREPORT_DB_FILE": str(bootstrap / "desktop.db"),
            "SMARTREPORT_DOCUMENTS_DIR": str(bootstrap / "documents"),
            "SMARTREPORT_MAIL_SETTINGS_FILE": str(bootstrap / "mail.json"),
            "SMARTREPORT_CLOUD_URL": "http://isolated-testserver",
            "SMARTREPORT_TRIAL_MODE": "1",
        })

        import server
        import app as desktop
        from PyQt6.QtWidgets import QApplication

        cls.server = server
        cls.desktop = desktop
        cls.qt_app = QApplication.instance() or QApplication([])
        from PyQt6.QtGui import QFont, QFontDatabase
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
        if font_path.is_file():
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                cls.qt_app.setFont(QFont(families[0], 9))

    @classmethod
    def tearDownClass(cls):
        cls._bootstrap.cleanup()
        for key, value in cls._previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def setUp(self):
        self._case = tempfile.TemporaryDirectory(prefix="smartreport-catalog-test-")
        root = Path(self._case.name)
        self.server.DB_FILE = str(root / "server.db")
        self.desktop.DB_FILE = str(root / "desktop.db")
        self.server.init_cloud_db()
        self.desktop.init_db_once()
        self.server_client = self.server.app.test_client()
        self._add_company("Renatur")

    def tearDown(self):
        self._case.cleanup()

    def _add_company(self, name):
        server_conn = self.server.get_db_connection()
        server_conn.execute("INSERT OR IGNORE INTO meta_companies (name) VALUES (?)", (name,))
        server_conn.commit()
        server_conn.close()
        desktop_conn = self.desktop.get_db_connection()
        desktop_conn.execute("INSERT OR IGNORE INTO companies (name) VALUES (?)", (name,))
        desktop_conn.commit()
        desktop_conn.close()

    def _server_post(self, payload):
        return self.server_client.post(
            "/sync-desktop-data",
            headers={"X-Sync-Key": "isolated-sync-test-key"},
            json=payload,
        )

    def _confirm_fixture_customer_prices(self, conn):
        """Explicit fixture contract, not a production current-price fallback."""
        for row in conn.execute("SELECT * FROM shifts").fetchall():
            obj = conn.execute("SELECT * FROM objects WHERE company=? AND name=?",
                               (row["company"], row["object_name"])).fetchone()
            if obj is None:
                continue
            base = row["rate"] if row["entry_type"] == "work" and row["rate"] > 0 else obj["rate"]
            quote = {"version": 1, "company": row["company"], "object_name": row["object_name"],
                     "date_from": row["date"], "date_to": row["date"], "source": "Synthetic fixture contract",
                     "base": base, "travel": obj["transport_rate"], "ob": {}}
            conn.execute("UPDATE shifts SET customer_pricing=? WHERE id=?", (json.dumps(quote), row["id"]))

    def _worker(self, failure=None):
        mode = {"value": failure}

        class Response:
            def __init__(self, response):
                self.response = response
                self.status_code = response.status_code
                self.ok = response.status_code < 400

            def json(self):
                return self.response.get_json()

            def raise_for_status(self):
                if self.status_code >= 400:
                    import requests
                    raise requests.HTTPError(f"isolated HTTP {self.status_code}")

        def fake_get(url, **kwargs):
            response = self.server_client.get(
                urlsplit(url).path,
                query_string=kwargs.get("params"),
                headers=kwargs.get("headers", {}),
            )
            return Response(response)

        def fake_post(url, **kwargs):
            path = urlsplit(url).path
            if path == "/sync-desktop-data" and mode["value"] == "offline":
                mode["value"] = None
                import requests
                raise requests.ConnectionError("simulated offline")
            response = Response(self.server_client.post(
                path,
                json=kwargs.get("json"),
                headers=kwargs.get("headers", {}),
            ))
            if path == "/sync/push" and mode["value"] == "lost_shift_ack":
                mode["value"] = None
                import requests
                raise requests.ConnectionError("simulated lost shift ACK after commit")
            if path == "/sync-desktop-data" and mode["value"] == "lost_ack":
                mode["value"] = None
                import requests
                raise requests.ConnectionError("simulated lost ACK after commit")
            return response

        def fake_request(method, url, **kwargs):
            response = self.server_client.open(
                urlsplit(url).path,
                method=method,
                query_string=kwargs.get("params"),
                headers=kwargs.get("headers", {}),
                json=kwargs.get("json"),
                data=kwargs.get("data"),
            )
            return Response(response)

        with patch.object(self.desktop.requests, "get", side_effect=fake_get), \
             patch.object(self.desktop.requests, "post", side_effect=fake_post), \
             patch.object(self.desktop.requests, "request", side_effect=fake_request):
            harness = SimpleNamespace(_manual_sync_requested=False)
            harness.apply_remote_shift = lambda cursor, shift: self.desktop.SmartReportApp.apply_remote_shift(
                harness, cursor, shift
            )
            harness._sync_expense_document_outbox = lambda: self.desktop.SmartReportApp._sync_expense_document_outbox(
                harness
            )
            return self.desktop.SmartReportApp.perform_sync_logic(harness)

    def test_expense_create_is_idempotent_by_client_id(self):
        desktop_conn = self.desktop.get_db_connection()
        outbox_columns = {
            row["name"] for row in desktop_conn.execute(
                "PRAGMA table_info(expense_document_outbox)"
            )
        }
        desktop_conn.close()
        self.assertTrue({
            "client_id", "operation", "server_document_id", "revision",
            "payload_json", "attachment_paths_json", "state", "last_error",
        }.issubset(outbox_columns))

        client_id = str(uuid.uuid4())
        payload = {
            "client_id": client_id,
            "supplier_name": "Isolated test supplier",
            "document_date": "2026-10-17",
            "currency_code": "SEK",
            "gross_amount": "125.00",
            "verification_status": "draft",
            "allocations": [],
        }
        headers = {"X-Sync-Key": "isolated-sync-test-key"}
        first = self.server_client.post(
            "/api/expense-documents", headers=headers, json=payload
        )
        second = self.server_client.post(
            "/api/expense-documents", headers=headers, json=payload
        )
        self.assertEqual(first.status_code, 201, first.get_json())
        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertTrue(second.get_json()["duplicate_request"])
        self.assertIsNone(first.get_json()["document"]["vat_amount"])
        self.assertIsNone(first.get_json()["document"]["net_amount"])
        self.assertEqual(
            first.get_json()["document"]["id"],
            second.get_json()["document"]["id"],
        )
        server_conn = self.server.get_db_connection()
        self.assertEqual(server_conn.execute(
            "SELECT COUNT(*) FROM expense_documents WHERE client_id = ?",
            (client_id,),
        ).fetchone()[0], 1)
        server_conn.close()

    def test_expense_api_duplicate_original_with_new_client_id_is_rejected(self):
        import hashlib

        original = b"isolated repeated original PDF content"
        headers = {"X-Sync-Key": "isolated-sync-test-key"}
        client_id = str(uuid.uuid4())
        payload = {"client_id": client_id, "supplier_name": "Hash supplier",
                   "document_date": "2026-10-17", "currency_code": "SEK",
                   "gross_amount": "125.00", "vat_amount": "25.00", "verification_status": "confirmed",
                   "allocations": [{"object_id": None, "gross_amount": "125.00", "vat_amount": "25.00"}]}

        def upload(identifier, name):
            return self.server_client.post("/api/expense-documents", headers=headers, data={
                "payload": json.dumps({**payload, "client_id": identifier}),
                "original": (BytesIO(original), name),
            })

        first = upload(client_id, "original.pdf")
        self.assertEqual(first.status_code, 201, first.get_json())
        document = first.get_json()["document"]
        self.assertEqual(document["attachments"][0]["sha256"], hashlib.sha256(original).hexdigest())
        retry = upload(client_id, "retry.pdf")
        self.assertEqual(retry.status_code, 200, retry.get_json())
        self.assertTrue(retry.get_json()["duplicate_request"])
        for copy in ("original (1).pdf", "original (2).pdf"):
            duplicate = upload(str(uuid.uuid4()), copy)
            self.assertEqual(duplicate.status_code, 409, duplicate.get_json())
            self.assertEqual(duplicate.get_json()["existing_document_id"], document["id"])
        connection = self.server.get_db_connection()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM expense_documents WHERE verification_status='confirmed'").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM expense_document_attachments").fetchone()[0], 1)
        connection.close()

    def test_expense_document_allocates_full_invoice_to_one_object(self):
        server_conn = self.server.get_db_connection()
        object_row = server_conn.execute(
            "SELECT id, sync_id FROM meta_objects WHERE is_active = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        server_conn.close()
        object_id = int(object_row["id"])
        client_id = str(uuid.uuid4())
        response = self.server_client.post(
            "/api/expense-documents",
            headers={"X-Sync-Key": "isolated-sync-test-key"},
            json={
                "client_id": client_id, "supplier_name": "Single object supplier",
                "document_date": "2026-10-17", "currency_code": "SEK",
                "gross_amount": "125.00", "vat_amount": "25.00",
                "verification_status": "confirmed",
                "allocations": [{
                    "object_id": object_id,
                    "gross_amount": "125.00",
                    "vat_amount": "25.00",
                }],
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        document = response.get_json()["document"]
        self.assertEqual(len(document["allocations"]), 1)
        self.assertEqual(document["allocations"][0]["object_id"], object_id)
        self.assertEqual(document["allocated_gross"], "125.00")
        self.assertEqual(document["unallocated_gross"], "0.00")
        summary = self.server_client.get(
            f"/api/expense-objects/{object_id}/expenses",
            headers={"X-Sync-Key": "isolated-sync-test-key"},
        ).get_json()
        self.assertEqual(summary["summary"]["SEK"]["gross"], "125.00")
        server_conn = self.server.get_db_connection()
        self.assertEqual(server_conn.execute(
            "SELECT COUNT(*) FROM expense_allocations WHERE document_id = ? AND object_id = ?",
            (document["id"], object_id),
        ).fetchone()[0], 1)
        server_conn.close()

    def test_expense_update_rejects_stale_server_version(self):
        client_id = str(uuid.uuid4())
        headers = {"X-Sync-Key": "isolated-sync-test-key"}
        payload = {
            "client_id": client_id, "supplier_name": "Version supplier",
            "document_date": "2026-10-17", "currency_code": "SEK",
            "gross_amount": "125.00", "vat_amount": "25.00",
            "verification_status": "requires_review", "allocations": [],
        }
        created = self.server_client.post("/api/expense-documents", headers=headers, json=payload)
        self.assertEqual(created.status_code, 201, created.get_json())
        document = created.get_json()["document"]
        self.assertEqual(document["sync_version"], 1)

        newer = {**payload, "document_comment": "edited on another client", "expected_version": 1}
        updated = self.server_client.put(
            f"/api/expense-documents/{document['id']}", headers=headers, json=newer
        )
        self.assertEqual(updated.status_code, 200, updated.get_json())
        self.assertEqual(updated.get_json()["document"]["sync_version"], 2)

        stale = {**payload, "document_comment": "stale local edit", "expected_version": 1}
        rejected = self.server_client.put(
            f"/api/expense-documents/{document['id']}", headers=headers, json=stale
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.get_json()["current_version"], 2)
        stale_delete = self.server_client.delete(
            f"/api/expense-documents/by-client/{client_id}",
            headers=headers, json={"expected_version": 1},
        )
        self.assertEqual(stale_delete.status_code, 409)
        self.assertEqual(stale_delete.get_json()["current_version"], 2)
        current = self.server_client.get(
            f"/api/expense-documents/{document['id']}", headers=headers
        ).get_json()["document"]
        self.assertEqual(current["document_comment"], "edited on another client")
        self.assertEqual(current["sync_version"], 2)

        harness = SimpleNamespace()
        self.desktop.SmartReportApp.queue_expense_document_operation(
            harness, client_id, "upsert",
            {**payload, "document_comment": "stale local edit"},
            server_document_id=int(document["id"]), server_version=1,
        )

        class Response:
            def __init__(self, response):
                self.response = response
                self.status_code = response.status_code
                self.ok = response.status_code < 400

            def json(self):
                return self.response.get_json()

        def fake_request(method, url, **kwargs):
            response = self.server_client.open(
                urlsplit(url).path, method=method,
                headers=kwargs.get("headers", {}), json=kwargs.get("json"),
                data=kwargs.get("data"),
            )
            return Response(response)

        with patch.object(self.desktop.requests, "request", side_effect=fake_request):
            sync_result = self.desktop.SmartReportApp._sync_expense_document_outbox(harness)
            self.assertEqual(sync_result["conflicts"], 1)
            view = SimpleNamespace(exp_api_status_lbl=self.desktop.QLabel(), _expense_objects=[])
            documents, online = self.desktop.SmartReportApp._expense_document_snapshot(view)
        self.assertTrue(online)
        self.assertEqual(documents[0]["sync_state"], "conflict")
        self.assertEqual(documents[0]["document_comment"], "edited on another client")
        self.assertEqual(
            documents[0]["local_conflict_payload"]["document_comment"],
            "stale local edit",
        )

    def test_legacy_gross_only_split_is_preserved_for_manual_vat_review(self):
        conn = self.server.get_db_connection()
        object_ids = [conn.execute("""
            INSERT INTO meta_objects (company,name,markning,rate,transport_rate,is_active)
            VALUES ('Renatur', ?, ?, 0, 0, 1)
        """, (name, marking)).lastrowid for name, marking in (
            ("Legacy split A", "p-legacy-split-a"),
            ("Legacy split B", "p-legacy-split-b"),
        )]
        conn.commit()
        conn.close()
        client_id = str(uuid.uuid4())
        response = self.server_client.post(
            "/api/expense-documents",
            headers={"X-Sync-Key": "isolated-sync-test-key"},
            json={
                "client_id": client_id, "supplier_name": "Old client supplier",
                "document_date": "2026-10-17", "currency_code": "SEK",
                "gross_amount": "125.00", "vat_amount": "25.00",
                "verification_status": "confirmed",
                "allocations": [
                    {"object_id": object_ids[0], "gross_amount": "60.00"},
                    {"object_id": object_ids[1], "gross_amount": "65.00"},
                ],
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        document = response.get_json()["document"]
        self.assertEqual(document["verification_status"], "requires_review")
        self.assertEqual(document["allocated_gross"], "125.00")
        self.assertIsNone(document["allocated_vat"])
        self.assertTrue(all(item["vat_amount"] is None for item in document["allocations"]))

    def test_expense_parser_keeps_document_vat_total_across_repeated_pages(self):
        parsed = self.desktop.parse_document_text("""
            Leverantör: Test Supplier
            Fakturadatum: 2026-10-17
            Netto: 100.00
            Moms totalt: 25.00
            Total inkl moms: 125.00

            Page 2 repeated footer
            Moms 25%: 25.00
            Moms totalt: 25.00
            Total inkl moms: 125.00
        """)
        self.assertEqual(parsed["vat_amount"], "25.00")
        self.assertEqual(parsed["gross_amount"], "125.00")

    def test_invoice_headers_and_product_prices_are_not_field_values(self):
        from expense_documents import parse_document_text

        result = parse_document_text(
            "Leverantör företagsinfo\nFakturanummer Fakturans valuta\n"
            "Artikel 123 Verktyg 25 % 280,96\n25 % 280,96\n"
        )
        self.assertIsNone(result["supplier_name"])
        self.assertIsNone(result["invoice_number"])
        self.assertIsNone(result["vat_amount"])
        self.assertIsNone(result["net_amount"])
        self.assertTrue(result["review_reasons"])

    def test_project_order_parser_carries_page_state_deduplicates_and_keeps_code_prefix(self):
        from expense_documents import parse_project_order_allocations

        parsed = parse_project_order_allocations([
            {"source": "invoice.pdf", "page": 1, "text": "Proj.nr.: P1022\nOrder nr: A-1\nOrder rows continue"},
            {"source": "invoice.pdf", "page": 2, "text": "Summa order: 100.00\nProj.nr.: 1022\nOrder nr: B-1\nSumma order: 50.00"},
            {"source": "invoice.pdf", "page": 3, "text": "Proj.nr.: P1022\nOrder nr: A-1\nSumma order: 100.00"},
        ])
        self.assertEqual(len(parsed["orders"]), 2)
        self.assertEqual(parsed["orders"][0]["project_code"], "P1022")
        self.assertEqual(parsed["orders"][0]["net_amount"], "100.00")
        self.assertEqual([source["page"] for source in parsed["orders"][0]["sources"]], [2, 3])
        self.assertEqual(parsed["orders"][1]["project_code"], "1022")
        self.assertEqual(parsed["net_total"], "150.00")
        self.assertTrue(parsed["requires_review"])
        self.assertTrue(any("НДС для заказа" in warning for warning in parsed["warnings"]))

    def test_project_order_parser_uses_only_explicit_order_vat_and_flags_ambiguous_repeat(self):
        from expense_documents import parse_project_order_allocations

        explicit = parse_project_order_allocations([
            {"source": "vat-invoice.pdf", "page": 1, "text": "Proj.nr.: P1022\nOrder nr: A-10\nSumma order: 100.00\nMoms order: 25.00"},
            {"source": "vat-invoice.pdf", "page": 2, "text": "Proj.nr.: P1022\nOrder nr: B-20\nSumma order: 50.00\nMoms order: 12.50"},
        ])
        self.assertEqual(explicit["net_total"], "150.00")
        self.assertEqual([order["vat_amount"] for order in explicit["orders"]], ["25.00", "12.50"])
        self.assertEqual([order["gross_amount"] for order in explicit["orders"]], ["125.00", "62.50"])
        self.assertFalse(explicit["requires_review"])

        ambiguous = parse_project_order_allocations([
            {"source": "repeat.pdf", "page": 1, "text": "Proj.nr.: P1022\nSumma order: 100.00"},
            {"source": "repeat.pdf", "page": 2, "text": "Proj.nr.: P1022\nSumma order: 100.00"},
        ])
        self.assertEqual(len(ambiguous["orders"]), 1)
        self.assertEqual(ambiguous["net_total"], "100.00")
        self.assertTrue(ambiguous["requires_review"])
        self.assertEqual([source["page"] for source in ambiguous["orders"][0]["sources"]], [1, 2])

    def test_project_order_parser_handles_rates_conflicts_and_new_order_vat(self):
        from expense_documents import parse_project_order_allocations

        rates = parse_project_order_allocations([
            {"source": "rates.pdf", "page": 1, "text":
             "Proj.nr.: P1022\nOrder nr: A\nSumma order: 200.00\nMoms order 25%: 25.00\nMoms order 12%: 25.00"},
            {"source": "rates.pdf", "page": 2, "text":
             "Order nr: A\nSumma order: 200.00\nMoms order 25%: 25.00\nMoms order 12%: 25.00"},
        ])
        self.assertEqual(rates["orders"][0]["vat_amount"], "50.00")
        self.assertEqual(rates["orders"][0]["gross_amount"], "250.00")
        self.assertFalse(rates["requires_review"])
        conflict = parse_project_order_allocations([{
            "source": "conflict.pdf", "page": 1, "text":
            "Proj.nr.: P1022\nOrder nr: A\nSumma order: 100.00\nMoms order: 25.00\nMoms order: 12.00",
        }])
        self.assertTrue(conflict["requires_review"])
        self.assertIsNone(conflict["orders"][0]["vat_amount"])
        self.assertNotIn("gross_amount", conflict["orders"][0])
        new_order = parse_project_order_allocations([{
            "source": "orders.pdf", "page": 1, "text":
            "Proj.nr.: P1022\nOrder nr: A\nSumma order: 100.00\nMoms order: 25.00\n"
            "Order nr: B\nMoms order: 6.00\nSumma order: 50.00",
        }])
        self.assertEqual(new_order["orders"][0]["vat_amount"], "25.00")
        self.assertIsNone(new_order["orders"][1]["vat_amount"])
        self.assertTrue(new_order["requires_review"])

    def test_project_order_parser_never_reads_partial_money_tokens(self):
        from expense_documents import parse_project_order_allocations

        for amount in ("1 234,56", "1.234,56", "1,234.56", "1234.56"):
            with self.subTest(amount=amount):
                result = parse_project_order_allocations([{
                    "page": 1, "text":
                    f"Proj.nr.: P1022\nOrder nr: A\nSumma order: {amount}\nMoms order: 0.00",
                }])
                self.assertEqual(result["orders"][0]["net_amount"], "1234.56")
                self.assertFalse(result["requires_review"])
        for amount in ("1.234", "1 23", "100.00.00"):
            with self.subTest(ambiguous=amount):
                result = parse_project_order_allocations([{
                    "page": 1, "text": f"Proj.nr.: P1022\nOrder nr: A\nSumma order: {amount}",
                }])
                self.assertEqual(result["orders"], [])
                self.assertTrue(result["requires_review"])

    def test_project_order_allocations_group_objects_and_require_explicit_vat(self):
        from expense_documents import build_project_order_allocations

        orders = [
            {"project_code": "P1022", "order_id": "A", "object_id": 11,
             "net_amount": "100.00", "vat_amount": "25.00", "sources": [{"page": 1}]},
            {"project_code": "P1022", "order_id": "B", "object_id": 11,
             "net_amount": "100.00", "vat_amount": "12.00", "sources": [{"page": 2}]},
        ]
        allocations = build_project_order_allocations(orders, "200.00", "37.00", "237.00")
        self.assertEqual(len(allocations), 1)
        self.assertEqual(allocations[0]["object_id"], 11)
        self.assertEqual(allocations[0]["net_amount"], "200.00")
        self.assertEqual(allocations[0]["vat_amount"], "37.00")
        self.assertEqual(allocations[0]["gross_amount"], "237.00")
        self.assertIn("Order B", allocations[0]["note"])
        with self.assertRaisesRegex(ValueError, "точно совпасть"):
            build_project_order_allocations(orders, "200.01", "37.00", "237.01")
        orders[1]["vat_amount"] = None
        with self.assertRaisesRegex(ValueError, "явные net и VAT"):
            build_project_order_allocations(orders, "200.00", "37.00", "237.00")

    def test_expense_order_api_roundtrip_confirmation_and_supplier_code_links(self):
        connection = self.server.get_db_connection()
        connection.executemany(
            "INSERT INTO meta_objects (company, name, markning, rate, transport_rate, is_active) "
            "VALUES ('Renatur', ?, ?, 0, 0, 1)",
            [("Order object one", "P1022"), ("Order object two", "1022")],
        )
        connection.commit()
        objects = connection.execute(
            "SELECT id FROM meta_objects WHERE name IN ('Order object one', 'Order object two') ORDER BY id"
        ).fetchall()
        connection.close()
        first_id, second_id = [int(row["id"]) for row in objects]
        source = {"source": "orders.pdf", "page": 2, "line": 3,
                  "text": "Summa order: 20.00", "attachment_index": 0}
        orders = [
            {"project_code": "P1022", "order_id": "A", "object_id": first_id,
             "net_amount": "100.00", "vat_amount": "25.00", "sources": []},
            {"project_code": "P1022", "order_id": "B", "object_id": first_id,
             "net_amount": "50.00", "vat_amount": "6.00", "sources": []},
            {"project_code": "1022", "order_id": "C", "object_id": second_id,
             "net_amount": "20.00", "vat_amount": None, "sources": [source]},
        ]
        headers = {"X-Sync-Key": "isolated-sync-test-key"}
        payload = {
            "client_id": str(uuid.uuid4()), "supplier_name": "Order supplier",
            "document_date": "2026-10-17", "currency_code": "SEK",
            "net_amount": "170.00", "vat_amount": "31.00", "gross_amount": "201.00",
            "verification_status": "confirmed", "order_proposals": orders, "allocations": [],
        }
        blocked = self.server_client.post("/api/expense-documents", headers=headers, json=payload)
        self.assertEqual(blocked.status_code, 400, blocked.get_json())
        payload["verification_status"] = "requires_review"
        saved = self.server_client.post("/api/expense-documents", headers=headers, json=payload)
        self.assertEqual(saved.status_code, 201, saved.get_json())
        document = saved.get_json()["document"]
        self.assertEqual(document["allocations"], [])
        self.assertEqual(document["order_proposals"][2]["sources"], [source])
        links_endpoint = "/api/expense-project-code-links?supplier_name=Order%20supplier"
        self.assertEqual(self.server_client.get(links_endpoint, headers=headers).get_json()["links"], [])

        orders[2]["vat_amount"] = "0.00"
        payload["verification_status"] = "confirmed"
        payload["expected_version"] = document["sync_version"]
        updated = self.server_client.put(
            f"/api/expense-documents/{document['id']}", headers=headers, json=payload,
        )
        self.assertEqual(updated.status_code, 200, updated.get_json())
        confirmed = updated.get_json()["document"]
        self.assertEqual(confirmed["verification_status"], "confirmed")
        self.assertEqual(len(confirmed["allocations"]), 2)
        self.assertEqual(confirmed["allocations"][0]["gross_amount"], "181.00")
        self.assertEqual(confirmed["allocations"][0]["vat_amount"], "31.00")
        self.assertEqual(confirmed["allocations"][1]["gross_amount"], "20.00")
        links = self.server_client.get(links_endpoint, headers=headers).get_json()["links"]
        self.assertEqual({row["project_code"]: row["object_id"] for row in links}, {
            "P1022": first_id, "1022": second_id,
        })
        other_supplier = self.server_client.get(
            "/api/expense-project-code-links?supplier_name=Other", headers=headers,
        ).get_json()
        self.assertEqual(other_supplier["links"], [])
        stale = self.server_client.put(
            f"/api/expense-documents/{document['id']}", headers=headers, json=payload,
        )
        self.assertEqual(stale.status_code, 409, stale.get_json())

    def test_expense_company_allocation_attachments_and_delete_tombstone(self):
        client_id = str(uuid.uuid4())
        server_conn = self.server.get_db_connection()
        object_row = server_conn.execute(
            "SELECT id, sync_id, name FROM meta_objects WHERE is_active = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(object_row)
        stable_object_id = int(object_row["id"])
        stable_sync_id = object_row["sync_id"]
        payload = {
            "client_id": client_id,
            "supplier_name": "Shared expense supplier",
            "document_date": "2026-10-17",
            "currency_code": "SEK",
            "gross_amount": "125.00",
            "vat_amount": "25.00",
            "verification_status": "requires_review",
            "allocations": [{
                "object_id": None,
                "gross_amount": "25.00",
                "vat_amount": "5.00",
            }, {
                "object_id": stable_object_id,
                "gross_amount": "75.00",
                "vat_amount": "15.00",
            }],
        }
        server_conn.close()

        response = self.server_client.post(
            "/api/expense-documents",
            headers={"X-Sync-Key": "isolated-sync-test-key"},
            data={
                "payload": json.dumps(payload),
                "originals": [
                    (BytesIO(b"first synthetic image"), "page-1.png"),
                    (BytesIO(b"second synthetic image"), "page-2.png"),
                ],
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        document = response.get_json()["document"]
        self.assertEqual(document["net_amount"], "100.00")
        self.assertEqual(document["allocated_gross"], "100.00")
        self.assertEqual(document["unallocated_gross"], "25.00")
        self.assertEqual(document["allocated_vat"], "20.00")
        self.assertEqual(document["unallocated_vat"], "5.00")
        self.assertEqual(len(document["attachments"]), 2)
        self.assertTrue(document["allocations"][0]["company_expense"])
        self.assertIsNone(document["allocations"][0]["object_id"])
        self.assertEqual(document["allocations"][1]["object_id"], stable_object_id)
        server_conn = self.server.get_db_connection()
        server_conn.execute(
            "UPDATE meta_objects SET name = ? WHERE id = ? AND sync_id IS ?",
            ("Expense object renamed", stable_object_id, stable_sync_id),
        )
        server_conn.commit()
        server_conn.close()
        renamed = self.server_client.get(
            f"/api/expense-documents/{document['id']}",
            headers={"X-Sync-Key": "isolated-sync-test-key"},
        ).get_json()["document"]
        self.assertEqual(renamed["allocations"][1]["object_id"], stable_object_id)
        self.assertEqual(renamed["allocations"][1]["object_name"], "Expense object renamed")
        object_summary = self.server_client.get(
            f"/api/expense-objects/{stable_object_id}/expenses",
            headers={"X-Sync-Key": "isolated-sync-test-key"},
        ).get_json()
        self.assertEqual(
            object_summary["documents"][0]["object_gross"], "75.00"
        )
        self.assertNotEqual(
            object_summary["documents"][0]["object_gross"],
            object_summary["documents"][0]["gross_amount"],
        )

        attachment = document["attachments"][1]
        original = self.server_client.get(
            f"/api/expense-documents/{document['id']}/attachments/{attachment['id']}/original",
            headers={"X-Sync-Key": "isolated-sync-test-key"},
        )
        self.assertEqual(original.status_code, 200)
        self.assertEqual(original.data, b"second synthetic image")
        original.close()

        deleted = self.server_client.delete(
            f"/api/expense-documents/by-client/{client_id}",
            headers={"X-Sync-Key": "isolated-sync-test-key"},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.server_client.delete(
            f"/api/expense-documents/by-client/{client_id}",
            headers={"X-Sync-Key": "isolated-sync-test-key"},
        ).status_code, 200)
        listed = self.server_client.get(
            "/api/expense-documents",
            headers={"X-Sync-Key": "isolated-sync-test-key"},
        )
        self.assertEqual(listed.get_json()["documents"], [])
        self.assertEqual(self.server_client.post(
            "/api/expense-documents",
            headers={"X-Sync-Key": "isolated-sync-test-key"},
            json=payload,
        ).status_code, 410)
        server_conn = self.server.get_db_connection()
        self.assertEqual(server_conn.execute(
            "SELECT is_deleted FROM expense_documents WHERE client_id = ?", (client_id,)
        ).fetchone()[0], 1)
        self.assertEqual(server_conn.execute(
            "SELECT COUNT(*) FROM expense_document_attachments WHERE document_id = ?",
            (document["id"],),
        ).fetchone()[0], 2)
        server_conn.close()

    def test_expense_schema_upgrade_preserves_allocations_and_original_metadata(self):
        conn = self.server.get_db_connection()
        object_id = conn.execute("""
            INSERT INTO meta_objects (company,name,markning,rate,transport_rate,is_active)
            VALUES ('Renatur','Migration expense object','p-migration',0,0,1)
        """).lastrowid
        document_id = conn.execute("""
            INSERT INTO expense_documents (
                client_id,supplier_name,document_date,currency_code,gross_amount,
                verification_status,original_name,stored_name,file_sha256,created_at,updated_at
            ) VALUES ('migration-client','Legacy supplier','2026-10-17','SEK','100.00',
                      'draft','legacy.pdf','legacy-stored.pdf',?, 'created','updated')
        """, ("a" * 64,)).lastrowid
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP INDEX IF EXISTS idx_expense_allocations_object")
        conn.execute("ALTER TABLE expense_allocations RENAME TO expense_allocations_before_company_costs")
        conn.execute("""
            CREATE TABLE expense_allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES expense_documents(id) ON DELETE CASCADE,
                object_id INTEGER NOT NULL REFERENCES meta_objects(id) ON DELETE RESTRICT,
                allocated_gross TEXT NOT NULL,
                allocated_net TEXT,
                allocated_vat TEXT,
                allocation_note TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            INSERT INTO expense_allocations (id,document_id,object_id,allocated_gross,allocated_net,allocated_vat)
            VALUES (77,?,?, '100.00','80.00','20.00')
        """, (document_id, object_id))
        conn.execute("DROP TABLE expense_allocations_before_company_costs")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.close()

        with patch.object(self.server, "create_database_backup", return_value=None):
            self.server.init_cloud_db()
            self.server.init_cloud_db()

        conn = self.server.get_db_connection()
        allocation_columns = {
            row["name"]: row["notnull"]
            for row in conn.execute("PRAGMA table_info(expense_allocations)")
        }
        self.assertEqual(allocation_columns["object_id"], 0)
        self.assertEqual(tuple(conn.execute(
            "SELECT id,document_id,object_id,allocated_gross,allocated_net,allocated_vat "
            "FROM expense_allocations WHERE id=77"
        ).fetchone()), (77, document_id, object_id, "100.00", "80.00", "20.00"))
        self.assertEqual(tuple(conn.execute(
            "SELECT original_name,stored_name,file_sha256 FROM expense_document_attachments "
            "WHERE document_id=?", (document_id,)
        ).fetchone()), ("legacy.pdf", "legacy-stored.pdf", "a" * 64))
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM expense_document_attachments WHERE document_id = ?",
            (document_id,),
        ).fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        conn.close()

    def test_expense_outbox_offline_lost_ack_attachment_and_delete(self):
        from reportlab.pdfgen import canvas

        client_id = str(uuid.uuid4())
        attachment_path = Path(self._case.name) / "expense-original.pdf"
        pdf = canvas.Canvas(str(attachment_path))
        for page_number in range(3):
            pdf.drawString(40, 760, f"Expense original page {page_number + 1}")
            pdf.showPage()
        pdf.save()
        expected_attachment = attachment_path.read_bytes()
        payload = {
            "client_id": client_id,
            "supplier_name": "Offline supplier",
            "document_date": "2026-10-17",
            "currency_code": "SEK",
            "net_amount": "100.00",
            "gross_amount": "125.00",
            "vat_amount": "25.00",
            "verification_status": "requires_review",
            "allocations": [{
                "object_id": None, "gross_amount": "50.00", "vat_amount": "10.00",
            }],
            "order_proposals": [{
                "project_code": "P1022", "order_id": "Offline-order", "object_id": None,
                "net_amount": "100.00", "vat_amount": "25.00", "gross_amount": "125.00",
                "ambiguous": False,
                "sources": [{"source": attachment_path.name, "page": 3, "line": 7,
                             "text": "Summa order: 100.00", "attachment_index": 0}],
            }],
        }
        harness = SimpleNamespace()
        self.desktop.SmartReportApp.queue_expense_document_operation(
            harness, client_id, "upsert", payload, attachment_paths=[attachment_path]
        )
        restart_code = (
            "import app,sys; app.DB_FILE=sys.argv[1]; "
            "conn=app.get_db_connection(); "
            "print(conn.execute('SELECT COUNT(*) FROM expense_document_outbox').fetchone()[0]); "
            "conn.close()"
        )
        self.assertEqual(subprocess.check_output(
            [sys.executable, "-c", restart_code, self.desktop.DB_FILE], text=True
        ).strip(), "1")
        mode = {"value": "offline"}

        def fake_request(method, url, headers=None, timeout=None, **kwargs):
            path = urlsplit(url).path
            if mode["value"] == "offline":
                mode["value"] = "lost_ack"
                raise self.desktop.requests.ConnectionError("simulated offline")
            data = dict(kwargs.get("data") or {})
            files = kwargs.get("files") or []
            if files:
                data["originals"] = [
                    (stream, filename, content_type)
                    for _field_name, (filename, stream, content_type) in files
                ]
                response = self.server_client.open(
                    path, method=method, headers=headers, data=data,
                )
            else:
                response = self.server_client.open(
                    path, method=method, headers=headers, json=kwargs.get("json"),
                )
            if mode["value"] == "lost_ack" and method == "POST":
                mode["value"] = None
                raise self.desktop.requests.ConnectionError("simulated lost ACK")
            return SimpleNamespace(
                status_code=response.status_code,
                json=response.get_json,
            )

        with patch.object(self.desktop.requests, "request", side_effect=fake_request):
            result = self.desktop.SmartReportApp._sync_expense_document_outbox(harness)
            self.assertEqual(result["synced"], 0)
            desktop_conn = self.desktop.get_db_connection()
            self.assertEqual(desktop_conn.execute(
                "SELECT state FROM expense_document_outbox WHERE client_id = ?", (client_id,)
            ).fetchone()[0], "pending")
            desktop_conn.close()

            result = self.desktop.SmartReportApp._sync_expense_document_outbox(harness)
            self.assertEqual(result["synced"], 0)
            desktop_conn = self.desktop.get_db_connection()
            self.assertEqual(desktop_conn.execute(
                "SELECT state FROM expense_document_outbox WHERE client_id = ?", (client_id,)
            ).fetchone()[0], "pending")
            desktop_conn.close()

            result = self.desktop.SmartReportApp._sync_expense_document_outbox(harness)
            if result["synced"] != 1:
                desktop_conn = self.desktop.get_db_connection()
                pending = dict(desktop_conn.execute(
                    "SELECT state, last_error, server_document_id FROM expense_document_outbox WHERE client_id = ?",
                    (client_id,),
                ).fetchone())
                desktop_conn.close()
                self.fail(f"Upsert sync was not acknowledged: {pending}")

            server_conn = self.server.get_db_connection()
            document = server_conn.execute(
                "SELECT id, is_deleted, sync_version FROM expense_documents WHERE client_id = ?",
                (client_id,),
            ).fetchone()
            self.assertEqual(tuple(document)[1], 0)
            self.assertEqual(server_conn.execute(
                "SELECT COUNT(*) FROM expense_documents WHERE client_id = ?", (client_id,)
            ).fetchone()[0], 1)
            attachment_id = server_conn.execute(
                "SELECT id FROM expense_document_attachments WHERE document_id = ?",
                (document["id"],),
            ).fetchone()[0]
            server_conn.close()
            second_client_document = self.server.app.test_client().get(
                f"/api/expense-documents/{document['id']}",
                headers={"X-Sync-Key": "isolated-sync-test-key"},
            ).get_json()["document"]
            self.assertEqual(second_client_document["order_proposals"], payload["order_proposals"])
            original = self.server_client.get(
                f"/api/expense-documents/{document['id']}/attachments/{attachment_id}/original",
                headers={"X-Sync-Key": "isolated-sync-test-key"},
            )
            self.assertEqual(original.data, expected_attachment)
            original.close()

            updated_payload = {**payload, "document_comment": "corrected manually"}
            self.desktop.SmartReportApp.queue_expense_document_operation(
                harness, client_id, "upsert", updated_payload,
                server_document_id=int(document["id"]),
                server_version=int(document["sync_version"]), attachment_paths=[],
            )
            result = self.desktop.SmartReportApp._sync_expense_document_outbox(harness)
            self.assertEqual(result["synced"], 1)
            server_conn = self.server.get_db_connection()
            self.assertEqual(server_conn.execute(
                "SELECT COUNT(*) FROM expense_documents WHERE client_id = ?", (client_id,)
            ).fetchone()[0], 1)
            self.assertEqual(server_conn.execute(
                "SELECT document_comment FROM expense_documents WHERE client_id = ?", (client_id,)
            ).fetchone()[0], "corrected manually")
            self.assertEqual(server_conn.execute(
                "SELECT COUNT(*) FROM expense_document_attachments WHERE document_id = ?",
                (document["id"],),
            ).fetchone()[0], 1)
            delete_version = server_conn.execute(
                "SELECT sync_version FROM expense_documents WHERE id = ?", (document["id"],)
            ).fetchone()[0]
            server_conn.close()

            self.desktop.SmartReportApp.queue_expense_document_operation(
                harness, client_id, "delete", {"client_id": client_id},
                server_document_id=int(document["id"]),
                server_version=int(delete_version), attachment_paths=[],
            )
            result = self.desktop.SmartReportApp._sync_expense_document_outbox(harness)
            if result["synced"] != 1:
                desktop_conn = self.desktop.get_db_connection()
                pending = dict(desktop_conn.execute(
                    "SELECT state, last_error, server_document_id FROM expense_document_outbox WHERE client_id = ?",
                    (client_id,),
                ).fetchone())
                desktop_conn.close()
                self.fail(f"Delete sync was not acknowledged: {pending}")
            self.assertEqual(self.desktop.SmartReportApp._sync_expense_document_outbox(harness)["synced"], 0)

        server_conn = self.server.get_db_connection()
        self.assertEqual(tuple(server_conn.execute(
            "SELECT is_deleted, COUNT(*) FROM expense_documents WHERE client_id = ?", (client_id,)
        ).fetchone()), (1, 1))
        server_conn.close()
        listed = self.server_client.get(
            "/api/expense-documents", headers={"X-Sync-Key": "isolated-sync-test-key"}
        )
        self.assertEqual(listed.get_json()["documents"], [])

    def test_expense_ack_closes_upload_before_cleanup_and_counts_saved_document(self):
        source = Path(self._case.name) / "windows-upload.pdf"
        source.write_bytes(b"isolated original")
        client_id = str(uuid.uuid4())
        payload = {"client_id": client_id, "supplier_name": "Test", "gross_amount": "125.00",
                   "currency_code": "SEK", "verification_status": "draft", "allocations": []}
        harness = SimpleNamespace()
        streams = []

        def request(method, url, **kwargs):
            if method == "GET":
                return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "api_version": 2,
                    "features": ["client_id", "soft_delete", "attachments", "nullable_company_allocations", "versioned_updates"]})
            streams.extend(value[1] for _name, value in kwargs.get("files") or [])
            return SimpleNamespace(status_code=201, json=lambda: {"ok": True,
                "document": {"id": 1, "client_id": client_id, "sync_version": 1, "has_original": True,
                             "attachments": [{"sha256": self.desktop.sha256_file(source), "has_original": True}]}})

        original_unlink = Path.unlink

        def checked_unlink(path, **kwargs):
            self.assertTrue(streams)
            self.assertTrue(all(stream.closed for stream in streams))
            return original_unlink(path, **kwargs)

        self.desktop.SmartReportApp.queue_expense_document_operation(harness, client_id, "upsert", payload,
                                                                   attachment_paths=[str(source)])
        with patch.object(self.desktop.requests, "request", side_effect=request), patch.object(Path, "unlink", new=checked_unlink):
            result = self.desktop.SmartReportApp._sync_expense_document_outbox(harness)
        self.assertEqual(result["synced"], 1)
        self.assertFalse(source.exists())

        source.write_bytes(b"retained but uploaded original")
        self.desktop.SmartReportApp.queue_expense_document_operation(harness, client_id, "upsert", payload,
                                                                   attachment_paths=[str(source)])
        with patch.object(self.desktop.requests, "request", side_effect=request), \
             patch.object(Path, "unlink", side_effect=PermissionError("external lock")):
            result = self.desktop.SmartReportApp._sync_expense_document_outbox(harness)
        self.assertEqual(result, {"synced": 1, "conflicts": 0, "cleanup_warnings": 1})
        self.assertTrue(source.is_file())
        connection = self.desktop.get_db_connection()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM expense_document_outbox").fetchone()[0], 0)
        connection.close()

    def test_expense_ack_requires_correct_document_and_all_attachment_hashes(self):
        source = Path(self._case.name) / "mandatory.pdf"
        source.write_bytes(b"isolated mandatory attachment")
        extra = Path(self._case.name) / "second-mandatory.pdf"
        extra.write_bytes(b"isolated second attachment")
        digest = self.desktop.sha256_file(source)
        extra_digest = self.desktop.sha256_file(extra)
        identifier = str(uuid.uuid4())
        payload = {"client_id": identifier, "supplier_name": "Isolated", "gross_amount": "125.00",
                   "currency_code": "SEK", "verification_status": "draft", "allocations": []}
        harness = SimpleNamespace()
        self.desktop.SmartReportApp.queue_expense_document_operation(harness, identifier, "upsert", payload,
                 server_document_id=7, server_version=1, attachment_paths=[str(source), str(extra)])
        correct = {"id": 7, "client_id": identifier, "sync_version": 2, "has_original": True,
                     "attachments": [{"sha256": digest, "has_original": True}, {"sha256": extra_digest, "has_original": True}]}
        responses = [[], {"ok": True, "document": "wrong"}]
        for change in ({"id": 8}, {"client_id": "another"}, {"sync_version": 0}, {"sync_version": 1},
                       {"attachments": []}, {"attachments": None}, {"attachments": {}}, {"has_original": False},
                       {"attachments": [{"sha256": digest, "has_original": True}]},
                       {"attachments": [{"sha256": "wrong", "has_original": True}]}):
            responses.append({"ok": True, "document": {**correct, **change}})

        def request(body):
            def answer(method, _url, **_kwargs):
                if method == "GET":
                    return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "api_version": 2,
                        "features": ["client_id", "soft_delete", "attachments", "nullable_company_allocations", "versioned_updates"]})
                return SimpleNamespace(status_code=200, json=lambda: body)
            return answer

        for body in responses:
            with patch.object(self.desktop.requests, "request", side_effect=request(body)):
                result = self.desktop.SmartReportApp._sync_expense_document_outbox(harness)
            self.assertEqual(result["synced"], 0)
            self.assertTrue(source.is_file())
            self.assertTrue(extra.is_file())
            self.assertEqual(self.desktop.sha256_file(source), digest)
            connection = self.desktop.get_db_connection()
            self.assertEqual(connection.execute("SELECT state FROM expense_document_outbox").fetchone()[0], "pending")
            connection.close()
        with patch.object(self.desktop.requests, "request", side_effect=request({"ok": True, "document": correct})):
            result = self.desktop.SmartReportApp._sync_expense_document_outbox(harness)
        self.assertEqual(result["synced"], 1)
        self.assertFalse(source.exists())
        self.assertFalse(extra.exists())

    def test_expense_outbox_runs_through_existing_sync_worker(self):
        client_id = str(uuid.uuid4())
        payload = {
            "client_id": client_id, "supplier_name": "Worker supplier",
            "document_date": "2026-10-17", "currency_code": "SEK",
            "gross_amount": "50.00", "vat_amount": None,
            "verification_status": "requires_review", "allocations": [],
        }
        conn = self.desktop.get_db_connection()
        self.desktop.SmartReportApp.queue_expense_document_operation(
            SimpleNamespace(), client_id, "upsert", payload,
        )
        conn.close()
        result = self._worker()
        self.assertEqual(result["conflicts"], 0)
        conn = self.desktop.get_db_connection()
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM expense_document_outbox WHERE client_id = ?", (client_id,)
        ).fetchone())
        conn.close()
        server_conn = self.server.get_db_connection()
        document = server_conn.execute(
            "SELECT client_id, gross_amount, vat_amount, net_amount FROM expense_documents WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        server_conn.close()
        self.assertEqual(tuple(document), (client_id, "50.00", None, None))

    def test_expense_outbox_blocks_mutations_when_server_is_old(self):
        client_id = str(uuid.uuid4())
        attachment_path = Path(self._case.name) / "pending-on-old-server.pdf"
        attachment_path.write_bytes(b"pending original")
        harness = SimpleNamespace()
        self.desktop.SmartReportApp.queue_expense_document_operation(
            harness, client_id, "upsert", {
                "client_id": client_id, "supplier_name": "Old server test",
                "currency_code": "SEK", "gross_amount": "25.00",
                "verification_status": "requires_review", "allocations": [],
                "order_proposals": [{"project_code": "P1022", "net_amount": "20.00"}],
            }, attachment_paths=[attachment_path],
        )
        calls = []

        class OldServerResponse:
            status_code = 404

            @staticmethod
            def json():
                return {"error": "capabilities route is missing"}

        def old_server_request(method, _url, **_kwargs):
            calls.append(method)
            return OldServerResponse()

        with patch.object(self.desktop.requests, "request", side_effect=old_server_request):
            result = self.desktop.SmartReportApp._sync_expense_document_outbox(harness)
        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(calls, ["GET"])
        self.assertTrue(attachment_path.is_file())
        desktop_conn = self.desktop.get_db_connection()
        pending = desktop_conn.execute(
            "SELECT state,last_error FROM expense_document_outbox WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        desktop_conn.close()
        self.assertEqual(pending["state"], "pending")
        self.assertIn("старый", pending["last_error"])
        calls.clear()

        def version_two_without_orders(method, _url, **_kwargs):
            calls.append(method)
            return SimpleNamespace(status_code=200, json=lambda: {
                "ok": True, "api_version": 2, "features": [
                    "client_id", "soft_delete", "attachments",
                    "nullable_company_allocations", "versioned_updates",
                ],
            })

        with patch.object(self.desktop.requests, "request", side_effect=version_two_without_orders):
            result = self.desktop.SmartReportApp._sync_expense_document_outbox(harness)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(calls, ["GET"])
        self.assertTrue(attachment_path.is_file())

    def test_expense_ui_has_one_section_with_two_views(self):
        class Response:
            def __init__(self, response):
                self.response = response
                self.status_code = response.status_code
                self.ok = response.status_code < 400

            def json(self):
                return self.response.get_json()

        def fake_request(method, url, **kwargs):
            response = self.server_client.open(
                urlsplit(url).path,
                method=method,
                headers=kwargs.get("headers", {}),
                query_string=kwargs.get("params"),
                json=kwargs.get("json"),
                data=kwargs.get("data"),
            )
            return Response(response)

        window = None
        scheduler = patch.object(self.desktop.SmartReportApp, "_schedule_expense_read")
        scheduler.start()
        try:
            with patch.object(self.desktop.requests, "request", side_effect=fake_request):
                window = self.desktop.SmartReportApp()
            window.auto_sync_timer.stop()
            self.assertEqual(window.exp_document_tabs.count(), 2)
            self.assertEqual(
                [window.exp_document_tabs.tabText(index) for index in range(2)],
                ["Все документы", "По объектам"],
            )
            main_titles = [window.tabs.tabText(index) for index in range(window.tabs.count())]
            self.assertEqual(main_titles.count("Расходы"), 1)
            self.assertNotIn("Материалы и расходы по объектам", main_titles)
            self.assertEqual(window.table_supplier_documents.columnCount(), 7)
            self.assertEqual(window.object_expenses_table.columnCount(), 7)
            self.assertEqual(window.object_expenses_object.currentIndex(), 0)
            server_conn = self.server.get_db_connection()
            stable_object = server_conn.execute(
                "SELECT id, sync_id FROM meta_objects WHERE is_active = 1 ORDER BY id LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(stable_object)
            object_id = int(stable_object["id"])
            objects = self.server_client.get("/api/expense-objects", headers={"X-Sync-Key": "isolated-sync-test-key"}).get_json()["objects"]
            window._on_expense_read_completed((None, None), objects, [], "")
            window.load_expense_document_filters()
            window.exp_filter_object.setCurrentIndex(window.exp_filter_object.findData(object_id))
            window.object_expenses_object.setCurrentIndex(
                window.object_expenses_object.findData(object_id)
            )
            server_conn.execute(
                "UPDATE meta_objects SET name = 'Stable expense object renamed' WHERE id = ?",
                (object_id,),
            )
            server_conn.commit()
            server_conn.close()
            with patch.object(self.desktop.requests, "request", side_effect=fake_request):
                objects = self.server_client.get("/api/expense-objects", headers={"X-Sync-Key": "isolated-sync-test-key"}).get_json()["objects"]
                window._on_expense_read_completed((None, None), objects, [], "")
                window.load_expense_document_filters()
            self.assertEqual(window.exp_filter_object.currentData(), object_id)
            self.assertEqual(window.object_expenses_object.currentData(), object_id)
            self.assertIn("Stable expense object renamed", window.object_expenses_object.currentText())
        finally:
            scheduler.stop()
            if window is not None:
                window.auto_sync_timer.stop()
                window.close()
                window.deleteLater()
                window._expense_temp_directory.cleanup()

    def test_markning_editor_survives_background_table_refresh(self):
        from PyQt6.QtTest import QTest

        sync_id = str(uuid.uuid4())
        server_conn = self.server.get_db_connection()
        server_conn.execute("""
            INSERT INTO meta_objects (id, company, name, markning, rate, transport_rate,
                                      sync_id, sync_version, is_active)
            VALUES (9001, 'Renatur', 'Editor test', 'p1038', 520.12345, 450.98765, ?, 1, 1)
        """, (sync_id,))
        server_conn.commit()
        server_conn.close()
        desktop_conn = self.desktop.get_db_connection()
        object_id = desktop_conn.execute("""
            INSERT INTO objects (company, name, markning, rate, transport_rate,
                                 sync_id, server_id, sync_version, is_active)
            VALUES ('Renatur', 'Editor test', 'p1038', 520.12345, 450.98765, ?, 9001, 1, 1)
        """, (sync_id,)).lastrowid
        desktop_conn.execute("""
            INSERT INTO shifts (date, employee, company, object_name, hours, rate, transport)
            VALUES ('2026-10-16', 'History worker', 'Renatur', 'Editor test', 8, 510, 440)
        """)
        document_id = desktop_conn.execute("""
            INSERT INTO supplier_documents (document_type, currency_code, gross_amount,
                                            expense_category, created_at, updated_at)
            VALUES ('manual', 'SEK', '100.00', 'Test allocation', 'test', 'test')
        """).lastrowid
        desktop_conn.execute("""
            INSERT INTO supplier_document_allocations (document_id, object_id, allocated_gross)
            VALUES (?, ?, '100.00')
        """, (document_id, object_id))
        original_history = [tuple(row) for row in desktop_conn.execute("SELECT * FROM shifts")]
        original_allocations = [tuple(row) for row in desktop_conn.execute("SELECT * FROM supplier_document_allocations")]
        desktop_conn.commit()
        desktop_conn.close()
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_sprav = self.desktop.QWidget()
        window.setup_sprav_tab()
        window.setCentralWidget(window.tab_sprav)
        window.show()
        try:
            window.load_objects_table()
            role = self.desktop.Qt.ItemDataRole.UserRole
            row = next(row for row in range(window.table_objects.rowCount())
                       if window.table_objects.item(row, 0).data(role) == object_id)
            item = window.table_objects.item(row, 2)
            window.table_objects.setCurrentItem(item)
            window.table_objects.editItem(item)
            editor = window.table_objects.findChild(self.desktop.QLineEdit)
            self.assertIsNotNone(editor)
            editor.setText("isolated-confirmed-code")
            window.load_objects_table()
            self.assertEqual(window.table_objects.state(), self.desktop.QTableWidget.State.EditingState)
            self.assertEqual(editor.text(), "isolated-confirmed-code")
            with patch.object(window, "background_sync_with_cloud") as sync:
                QTest.keyClick(editor, self.desktop.Qt.Key.Key_Return)
                self.qt_app.processEvents()
                sync.assert_called_once()
            desktop_conn = self.desktop.get_db_connection()
            self.assertEqual(tuple(desktop_conn.execute(
                "SELECT markning, rate, transport_rate, sync_version FROM objects WHERE id = ?", (object_id,)
            ).fetchone()), ("isolated-confirmed-code", 520.12345, 450.98765, 2))
            self.assertEqual(desktop_conn.execute(
                "SELECT COUNT(*) FROM object_sync_outbox WHERE object_id = ?", (object_id,)
            ).fetchone()[0], 1)
            desktop_conn.close()
            with patch.object(self.server, "apply_object_sync_event", side_effect=ValueError("Test revision refusal")):
                with self.assertRaisesRegex(RuntimeError, "Test revision refusal") as failure:
                    self._worker()
            window._manual_sync_requested = False
            with patch.object(self.desktop.QMessageBox, "warning") as warning:
                window._on_sync_completed(False, 0, 0, 0, 0, 0, "", "", str(failure.exception))
                warning.assert_called_once()
                self.assertIn("Test revision refusal", warning.call_args.args[2])
                self.assertIn("очереди", warning.call_args.args[2])
            with self.assertRaises(self.desktop.requests.ConnectionError):
                self._worker("offline")
            with self.assertRaises(self.desktop.requests.ConnectionError):
                self._worker("lost_ack")
            self.assertEqual(self._worker()["conflicts"], 0)
            self.assertEqual(self._worker()["conflicts"], 0)
            window.load_objects_table()
            row = next(row for row in range(window.table_objects.rowCount())
                       if window.table_objects.item(row, 0).data(role) == object_id)
            self.assertEqual(window.table_objects.item(row, 2).text(), "isolated-confirmed-code")
            desktop_conn = self.desktop.get_db_connection()
            self.assertEqual(tuple(desktop_conn.execute(
                "SELECT id,sync_id,server_id,rate,transport_rate FROM objects WHERE id = ?", (object_id,)
            ).fetchone()), (object_id, sync_id, 9001, 520.12345, 450.98765))
            self.assertEqual([tuple(row) for row in desktop_conn.execute("SELECT * FROM shifts")], original_history)
            self.assertEqual([tuple(row) for row in desktop_conn.execute("SELECT * FROM supplier_document_allocations")], original_allocations)
            self.assertEqual(desktop_conn.execute("SELECT COUNT(*) FROM object_sync_outbox").fetchone()[0], 0)
            self.assertEqual(desktop_conn.execute("SELECT COUNT(*) FROM objects WHERE sync_id = ?", (sync_id,)).fetchone()[0], 1)
            desktop_conn.close()
            server_conn = self.server.get_db_connection()
            self.assertEqual(tuple(server_conn.execute(
                "SELECT id,sync_id,markning,rate,transport_rate,sync_version FROM meta_objects WHERE sync_id = ?",
                (sync_id,),
            ).fetchone()), (9001, sync_id, "isolated-confirmed-code", 520.12345, 450.98765, 2))
            self.assertEqual(server_conn.execute("SELECT COUNT(*) FROM meta_objects WHERE sync_id = ?", (sync_id,)).fetchone()[0], 1)
            self.assertEqual(server_conn.execute("SELECT COUNT(*) FROM object_sync_events WHERE sync_id = ?", (sync_id,)).fetchone()[0], 1)
            server_conn.close()
            restart_code = (
                "import app,sys; app.DB_FILE=sys.argv[1]; app.init_db_once(); "
                "connection=app.get_db_connection(); "
                "print(connection.execute('SELECT markning FROM objects WHERE id = ?', (int(sys.argv[2]),)).fetchone()[0]); "
                "connection.close()"
            )
            persisted = subprocess.check_output(
                [sys.executable, "-c", restart_code, self.desktop.DB_FILE, str(object_id)], text=True
            )
            self.assertEqual(persisted.strip(), "isolated-confirmed-code")
            server_conn = self.server.get_db_connection()
            server_conn.execute(
                "UPDATE meta_objects SET markning = 'newer-server-code', sync_version = 4 WHERE id = 9001"
            )
            server_conn.commit()
            server_conn.close()
            with patch.object(window, "background_sync_with_cloud"):
                window.table_objects.item(row, 2).setText("second-proposal")
            result = self._worker()
            self.assertEqual(result["conflicts"], 1)
            with patch.object(window, "load_shifts_history"), \
                 patch.object(self.desktop.QMessageBox, "warning") as warning:
                window._on_sync_completed(True, **result, error="")
                warning.assert_called_once()
                self.assertIn("revision", warning.call_args.args[2])
            window.load_objects_table()
            self.assertEqual(window.table_objects.item(row, 2).text(), "newer-server-code")
        finally:
            window.close()
            window.deleteLater()

    def test_markning_edit_rejects_stale_revision_with_visible_reason(self):
        desktop_conn = self.desktop.get_db_connection()
        object_id = desktop_conn.execute("""
            INSERT INTO objects (company,name,markning,rate,transport_rate,sync_id,server_id,sync_version,is_active)
            VALUES ('Renatur', 'Stale editor test', 'p1038', 520, 450, ?, 9001, 1, 1)
        """, (str(uuid.uuid4()),)).lastrowid
        desktop_conn.commit()
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_sprav = self.desktop.QWidget()
        window.setup_sprav_tab()
        try:
            window.load_objects_table()
            row = next(row for row in range(window.table_objects.rowCount())
                       if window.table_objects.item(row, 0).data(self.desktop.Qt.ItemDataRole.UserRole) == object_id)
            desktop_conn.execute(
                "UPDATE objects SET markning = 'remote-change', sync_version = 2 WHERE id = ?", (object_id,)
            )
            desktop_conn.commit()
            with patch.object(self.desktop.QMessageBox, "warning") as warning, \
                 patch.object(window, "background_sync_with_cloud") as sync:
                window.table_objects.item(row, 2).setText("attempted-code")
                self.qt_app.processEvents()
                warning.assert_called_once()
                self.assertIn("изменился", warning.call_args.args[2])
                self.assertIn("attempted-code", warning.call_args.args[2])
                sync.assert_not_called()
            self.assertEqual(desktop_conn.execute(
                "SELECT markning FROM objects WHERE id = ?", (object_id,)
            ).fetchone()[0], "remote-change")
            self.assertEqual(desktop_conn.execute("SELECT COUNT(*) FROM object_sync_outbox").fetchone()[0], 0)
        finally:
            desktop_conn.close()
            window.close()
            window.deleteLater()

    def test_balance_employee_popup_preserves_bottom_scroll_during_refresh(self):
        from PyQt6.QtCore import QTimer, Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QComboBox

        conn = self.desktop.get_db_connection()
        try:
            conn.executemany("INSERT INTO employees (name,is_active) VALUES (?,?)",
                             [(f"ZZ balance employee {i:03d}", i % 2) for i in range(90)])
            conn.commit()
        finally:
            conn.close()
        combo = QComboBox()
        combo.setEditable(True)
        combo.setMaxVisibleItems(10)
        combo.resize(300, 30)
        host = SimpleNamespace(bal_emp_cb=combo)
        refresh = lambda: self.desktop.SmartReportApp.load_dropdowns(host, refresh_tables=False)
        refresh()
        try:
            combo.show()
            for name in ("ZZ balance employee 088", "ZZ balance employee 089"):
                combo.setCurrentIndex(0)
                combo.showPopup()
                self.qt_app.processEvents()
                view = combo.view()
                target = combo.model().index(combo.findText(name), 0)
                view.setCurrentIndex(target)
                view.scrollTo(target, view.ScrollHint.PositionAtBottom)
                self.qt_app.processEvents()
                scroll = view.verticalScrollBar().value()
                self.assertGreater(scroll, 0)
                timer = QTimer()
                timer.setInterval(2000)
                timer.timeout.connect(refresh)
                timer.start()
                try:
                    QTest.qWait(2200)
                finally:
                    timer.stop()
                self.assertTrue(view.isVisible())
                self.assertEqual(view.verticalScrollBar().value(), scroll)
                self.assertEqual(view.currentIndex(), target)
                QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                                 pos=view.visualRect(target).center())
                self.assertEqual(combo.currentText(), name)
                self.assertFalse(view.isVisible())
        finally:
            combo.close()
            combo.deleteLater()

    def test_balance_employee_catalog_changes_wait_until_popup_closes(self):
        from PyQt6.QtWidgets import QComboBox

        combo = QComboBox()
        combo.setEditable(True)
        host = SimpleNamespace(bal_emp_cb=combo)
        refresh = lambda: self.desktop.SmartReportApp.load_dropdowns(host, refresh_tables=False)
        refresh()
        try:
            combo.setCurrentText("Local editable contact")
            refresh()
            self.assertEqual(combo.currentText(), "Local editable contact")
            combo.show()
            combo.showPopup()
            self.qt_app.processEvents()
            count = combo.count()
            conn = self.desktop.get_db_connection()
            try:
                conn.execute("INSERT INTO employees (name,is_active) VALUES ('ZZ added during selection',0)")
                conn.commit()
            finally:
                conn.close()
            refresh()
            self.assertEqual(combo.count(), count)
            combo.hidePopup()
            refresh()
            self.assertGreaterEqual(combo.findText("ZZ added during selection"), 0)
            self.assertEqual(combo.currentText(), "Local editable contact")
            combo.blockSignals(True)
            refresh()
            self.assertTrue(combo.signalsBlocked())
        finally:
            combo.close()
            combo.deleteLater()

    def test_invoice_object_selection_survives_dropdown_refresh(self):
        sync_id = str(uuid.uuid4())
        desktop_conn = self.desktop.get_db_connection()
        desktop_conn.execute("""
            INSERT INTO objects (company,name,markning,rate,transport_rate,sync_id,is_active)
            VALUES ('Renatur', 'ZZ selected invoice object', 'invoice-selection', 520, 450, ?, 1)
        """, (sync_id,))
        desktop_conn.commit()
        desktop_conn.close()
        server_conn = self.server.get_db_connection()
        server_conn.execute("""
            INSERT INTO meta_objects (id,company,name,markning,rate,transport_rate,sync_id,sync_version,is_active)
            VALUES (9002,'Renatur','ZZ selected invoice object','invoice-selection',520,450,?,0,1)
        """, (sync_id,))
        server_conn.commit()
        server_conn.close()
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_invoice = self.desktop.QWidget()
        window.setup_invoice_tab()
        try:
            window.inv_comp_cb.setCurrentText("Renatur")
            index = window.inv_obj_cb.findText("invoice-selection", self.desktop.Qt.MatchFlag.MatchContains)
            self.assertGreaterEqual(index, 0)
            window.inv_obj_cb.setCurrentIndex(index)
            selected = window.inv_obj_cb.currentText()
            object_id = window.inv_obj_cb.currentData()
            for _iteration in range(3):
                self.assertEqual(self._worker()['conflicts'], 0)
                window.load_dropdowns()
                self.assertEqual(window.inv_obj_cb.currentText(), selected)
                self.assertEqual(window.inv_obj_cb.currentData(), object_id)
            server_conn = self.server.get_db_connection()
            server_conn.execute("""
                UPDATE meta_objects SET name='AA renamed invoice object', markning='new-code', sync_version=1
                WHERE id=9002
            """)
            server_conn.commit()
            server_conn.close()
            self.assertEqual(self._worker()['conflicts'], 0)
            window.load_dropdowns()
            self.assertEqual(window.inv_obj_cb.currentData(), object_id)
            self.assertIn('new-code', window.inv_obj_cb.currentText())
            desktop_conn = self.desktop.get_db_connection()
            desktop_conn.execute('DELETE FROM objects WHERE id=?', (object_id,))
            desktop_conn.commit()
            desktop_conn.close()
            window.load_dropdowns()
            self.assertEqual(window.inv_obj_cb.currentIndex(), -1)
        finally:
            window.close()
            window.deleteLater()

    def test_invoice_pdf_matches_table_and_recalculates_changed_filters(self):
        desktop_conn = self.desktop.get_db_connection()
        identities = {}
        for name, markning, rate in (('Invoice work', 'p1038', 425), ('Invoice hotel', 'p1039', 520)):
            identities[name] = desktop_conn.execute("""
                INSERT INTO objects (company,name,markning,rate,transport_rate,sync_id,is_active)
                VALUES ('Renatur',?,?,?,450,?,1)
            """, (name, markning, rate, str(uuid.uuid4()))).lastrowid
        desktop_conn.executemany("""
            INSERT INTO shifts (date,employee,company,object_name,hours,rate,transport,invoiced,entry_type)
            VALUES (?,?,'Renatur',?,?,400,?,?,?)
        """, (
            ('2026-10-01', 'Alice', 'Invoice work', 2, 1, 0, 'work'),
            ('2026-10-31', 'Alice', 'Invoice work', 3, 0, 1, 'work'),
            ('2026-10-31', 'Bob', 'Invoice work', 1, 1, 1, 'work'),
            ('2026-11-01', 'Alice', 'Invoice work', 9, 0, 0, 'work'),
            ('2026-09-30', 'Alice', 'Invoice work', 9, 0, 0, 'work'),
            ('2026-10-31', 'Carol', 'Invoice hotel', 4, 1, 0, 'work'),
            ('2026-10-31', 'Excluded', 'Invoice work', 8, 0, 0, 'atf'),
            ('2026-10-31', 'Excluded', 'Invoice work', 8, 0, 0, 'sick'),
        ))
        ob_id = str(uuid.uuid4())
        result = self.desktop.calculate_work_interval('2026-10-31', '18:00', '19:00', 400)
        desktop_conn.execute("""
            INSERT INTO shifts (date,employee,company,object_name,hours,rate,transport,invoiced,
                                entry_type,sync_id,start_time,end_time,ob_base_rate)
            VALUES ('2026-10-31','Alice','Renatur','Invoice work',1,0,0,0,'ob',?,'18:00','19:00',400)
        """, (ob_id,))
        self.desktop.replace_ob_segments(desktop_conn.cursor(), ob_id, result['segments'])
        self._confirm_fixture_customer_prices(desktop_conn)
        desktop_conn.commit()
        original_shifts = [tuple(row) for row in desktop_conn.execute('SELECT * FROM shifts ORDER BY id')]
        desktop_conn.close()
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_invoice = self.desktop.QWidget()
        window.setup_invoice_tab()
        try:
            window.inv_comp_cb.setCurrentText('Renatur')
            window.inv_obj_cb.setCurrentIndex(window.inv_obj_cb.findData(identities['Invoice work']))
            report = window.generate_invoice()
            self.assertEqual([row[0] for row in report['rows']], ['Alice', 'Bob'])
            self.assertEqual([row[1] for row in report['rows']], [6, 1])
            self.assertEqual([row[3] for row in report['rows']], [2, 1])
            self.assertEqual(report['rows'][0][4], 1)
            self.assertAlmostEqual(report['rows'][0][5], result['total_amount'])
            summary = {
                window.invoice_summary_table.item(index, 0).text():
                window.invoice_summary_table.item(index, 1).text()
                for index in range(window.invoice_summary_table.rowCount())
            }
            self.assertIn('Preliminärt totalbelopp', summary)
            self.assertEqual(report['totals']['work_amount'], 2825)
            self.assertIsNone(report['totals']['invoice_total'])
            self.assertEqual(
                report['ob_rule_descriptions'],
                {
                    'OB1': 'Måndag–Fredag 18:00–20:00',
                    'OB2': 'Måndag–Fredag 20:00–24:00',
                },
            )
            self.assertEqual(
                window.invoice_ob_rules_lbl.text(),
                'OB1 (18:00–20:00) — Måndag–Fredag 18:00–20:00; '
                'OB2 (20:00–24:00) — Måndag–Fredag 20:00–24:00',
            )

            def check_pdf(build, object_name):
                story = build.call_args.args[0]
                heading = '\n'.join(flow.getPlainText() for flow in story if hasattr(flow, 'getPlainText'))
                self.assertIn(object_name, heading)
                self.assertIn('Renatur', heading)
                self.assertIn(window.inv_date_to.text(), heading)
                pdf_table = next(flow for flow in story if isinstance(flow, self.desktop.LongTable))
                self.assertAlmostEqual(sum(pdf_table._colWidths), self.desktop.landscape(self.desktop.A4)[0] - 72)
                for row_index in range(window.table_invoice.rowCount()):
                    pdf_row = pdf_table._cellvalues[row_index + 1]
                    self.assertEqual(pdf_row[0].getPlainText(), window.table_invoice.item(row_index, 0).text())
                    for column in (1, 2, 3):
                        self.assertEqual(
                            pdf_row[column].getPlainText().replace(",", "."),
                            window.table_invoice.item(row_index, column).text(),
                        )
                self.assertIn('Arbetsjournal', heading)
                journal_table = [
                    flow for flow in story if isinstance(flow, self.desktop.LongTable)
                ][-1]
                self.assertEqual(len(journal_table._cellvalues) - 1, len(window._invoice_report['journal_rows']))
                self.assertNotIn('Löneunderlag', heading)

            with patch.object(self.desktop, 'REPORTS_DIR', str(Path(self._case.name) / 'reports')), \
                 patch.object(self.desktop.SimpleDocTemplate, 'build') as build, \
                 patch.object(self.desktop.os, 'startfile', create=True), \
                 patch.object(self.desktop.QMessageBox, 'information'), \
                 patch.object(self.desktop.QMessageBox, 'critical') as critical, \
                 patch.object(self.desktop.QMessageBox, 'warning') as warning:
                window.export_invoice_to_pdf()
                check_pdf(build, 'Invoice work')
                window.inv_obj_cb.setCurrentIndex(window.inv_obj_cb.findData(identities['Invoice hotel']))
                window.export_invoice_to_pdf()
                check_pdf(build, 'Invoice hotel')
                self.assertEqual(window.table_invoice.rowCount(), 1)
                self.assertEqual(window.table_invoice.item(0, 0).text(), 'Carol')
                self.assertEqual(
                    window._invoice_report['totals']['invoice_total'], 2050
                )
                window.inv_obj_cb.setCurrentIndex(window.inv_obj_cb.findData(identities['Invoice work']))
                window.inv_date_from.setDate(self.desktop.QDate(2026, 10, 31))
                window.export_invoice_to_pdf()
                check_pdf(build, 'Invoice work')
                self.assertEqual([row[1] for row in window._invoice_report['rows']], [4, 1])
                self.assertEqual([row[3] for row in window._invoice_report['rows']], [2, 1])
                window.inv_date_from.setDate(self.desktop.QDate(2026, 11, 2))
                window.inv_date_to.setDate(self.desktop.QDate(2026, 11, 3))
                build.reset_mock()
                window.export_invoice_to_pdf()
                build.assert_not_called()
                warning.assert_called_once()
                self.assertIn('Invoice work', warning.call_args.args[2])
                self.assertIn('Закрытые и незакрытые', warning.call_args.args[2])
                self.assertEqual(window.table_invoice.rowCount(), 0)
                critical.assert_not_called()
            desktop_conn = self.desktop.get_db_connection()
            self.assertEqual([tuple(row) for row in desktop_conn.execute('SELECT * FROM shifts ORDER BY id')], original_shifts)
            desktop_conn.executemany("""
                INSERT INTO shifts (date,employee,company,object_name,hours,rate,transport,invoiced,entry_type)
                VALUES ('2026-10-15',?,'Renatur','Invoice work',8,400,0,0,'work')
            """, ((f'Pagination worker {index:02}',) for index in range(45)))
            desktop_conn.commit()
            desktop_conn.close()
            window.inv_date_from.setDate(self.desktop.QDate(2026, 10, 1))
            window.inv_date_to.setDate(self.desktop.QDate(2026, 10, 31))
            reports_dir = Path(self._case.name) / 'actual-pdf'
            with patch.object(self.desktop, 'REPORTS_DIR', str(reports_dir)), \
                 patch.object(self.desktop.os, 'startfile', create=True), \
                 patch.object(self.desktop.QMessageBox, 'information'), \
                 patch.object(self.desktop.QMessageBox, 'critical') as critical:
                window.export_invoice_to_pdf()
                generated_pdfs = list(reports_dir.glob('*.pdf'))
                self.assertEqual(len(generated_pdfs), 1)
                pdf_bytes = generated_pdfs[0].read_bytes()
                self.assertTrue(pdf_bytes.startswith(b'%PDF-'))
                self.assertGreater(len(pdf_bytes), 1000)
                from pypdf import PdfReader

                pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(str(generated_pdfs[0])).pages)
                self.assertIn('Varav OB1', pdf_text)
                self.assertIn('Varav OB2', pdf_text)
                self.assertIn('Arbetsjournal', pdf_text)
                critical.assert_not_called()
        finally:
            window.close()
            window.deleteLater()

    def test_invoice_ob_categories_are_compact_and_not_added_twice(self):
        desktop_conn = self.desktop.get_db_connection()
        object_id = desktop_conn.execute("""
            INSERT INTO objects (company,name,markning,rate,transport_rate,sync_id,is_active)
            VALUES ('Renatur','Compact OB test','p1038',200,50,?,1)
        """, (str(uuid.uuid4()),)).lastrowid
        desktop_conn.executemany("""
            INSERT INTO shifts (date,employee,company,object_name,hours,rate,transport,invoiced,entry_type)
            VALUES ('2026-10-05',?,'Renatur','Compact OB test',?,200,?,0,'work')
        """, (('Alice', 8, 2), ('Bob', 6, 1), ('Carol', 3, 0)))
        for employee, start_time, end_time in (
            ('Alice', '18:00', '20:00'),
            ('Alice', '19:00', '20:00'),
            ('Bob', '18:00', '21:00'),
        ):
            sync_id = str(uuid.uuid4())
            result = self.desktop.calculate_work_interval(
                '2026-10-05', start_time, end_time, 100
            )
            desktop_conn.execute("""
                INSERT INTO shifts (date,employee,company,object_name,hours,rate,transport,invoiced,
                                    entry_type,sync_id,start_time,end_time,ob_base_rate)
                VALUES ('2026-10-05',?,'Renatur','Compact OB test',?,0,0,0,'ob',?,?,?,100)
            """, (employee, result['total_hours'], sync_id, start_time, end_time))
            self.desktop.replace_ob_segments(desktop_conn.cursor(), sync_id, result['segments'])
        self._confirm_fixture_customer_prices(desktop_conn)
        desktop_conn.commit()
        desktop_conn.close()

        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_invoice = self.desktop.QWidget()
        window.setup_invoice_tab()
        try:
            window.inv_comp_cb.setCurrentText('Renatur')
            window.inv_obj_cb.setCurrentIndex(window.inv_obj_cb.findData(object_id))
            report = window.generate_invoice()
            self.assertEqual(report['totals']['hours'], 23)
            self.assertEqual(report['totals']['ob_categories']['weekday_early_evening'], 4)
            self.assertEqual(report['totals']['ob_categories']['weekday_late_evening_night'], 1)
            self.assertEqual(report['totals']['work_amount'], 4600)
            self.assertEqual(report['totals']['transport_amount'], 150)
            self.assertIsNone(report['totals']['invoice_total'])
            self.assertIsNone(report['totals']['ob_amount'])
            self.assertEqual(window.table_invoice.columnCount(), 6)
            self.assertNotIn('18:00', '\n'.join(
                window.table_invoice.item(row, column).text()
                for row in range(window.table_invoice.rowCount())
                for column in range(window.table_invoice.columnCount())
            ))
            summary = {
                window.invoice_summary_table.item(row, 0).text():
                window.invoice_summary_table.item(row, 1).text()
                for row in range(window.invoice_summary_table.rowCount())
            }
            self.assertEqual(summary['Arbetade timmar totalt'], '23.0 tim')
            self.assertEqual(summary['Varav utan OB'], '18.0 tim')
            self.assertEqual(summary['Varav OB1 (18:00–20:00)'], '4.0 tim')
            self.assertEqual(summary['Varav OB2 (20:00–24:00)'], '1.0 tim')
            self.assertIn('OB-timmar ingår i arbetade timmar', window.invoice_summary_note.text())
            self.assertIn('PRELIMINÄRT', window.invoice_summary_note.text())
            self.assertIn('OB-tillägg', window.invoice_summary_note.text())
        finally:
            window.close()
            window.deleteLater()

    def test_invoice_ob_only_shift_is_full_work_without_payroll_double_billing(self):
        conn = self.desktop.get_db_connection()
        identities = {}
        for name in ("Invoice Tomteboda ÄTA", "Invoice HOTEL Djurgården"):
            identities[name] = conn.execute("""
                INSERT INTO objects (company,name,markning,rate,transport_rate,sync_id,is_active)
                VALUES ('Renatur',?,'report-test',520,450,?,1)
            """, (name, str(uuid.uuid4()))).lastrowid
        ob_id = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO shifts (date,employee,company,object_name,hours,rate,transport,
                                invoiced,entry_type,sync_id,start_time,end_time,ob_base_rate,comment)
            VALUES ('2026-10-21','Report worker','Renatur','Invoice Tomteboda ÄTA',4,0,1,
                    0,'ob',?,'16:00','20:00',245.8,'Keep this manual comment')
        """, (ob_id,))
        conn.execute("""
            INSERT INTO shifts (date,employee,company,object_name,hours,rate,transport,invoiced,entry_type)
            VALUES ('2026-10-21','Report worker','Renatur','Invoice HOTEL Djurgården',8,0,1,1,'work')
        """)
        calculation = self.desktop.calculate_work_interval('2026-10-21', '16:00', '20:00', 245.8)
        self.desktop.replace_ob_segments(conn.cursor(), ob_id, calculation['segments'])
        self.assertEqual(calculation['total_amount'], 1229)
        self._confirm_fixture_customer_prices(conn)
        conn.commit()
        before = list(conn.iterdump())
        conn.close()
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_invoice = self.desktop.QWidget()

        def readonly_connection():
            import sqlite3
            connection = sqlite3.connect(Path(self.desktop.DB_FILE).as_uri() + "?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            return connection

        try:
            with patch.object(self.desktop, 'get_db_connection', readonly_connection):
                window.setup_invoice_tab()
                window.inv_comp_cb.setCurrentText('Renatur')
                for field in (window.inv_date_from, window.inv_date_to):
                    field.setDate(self.desktop.QDate(2026, 10, 21))
                window.inv_obj_cb.setCurrentIndex(window.inv_obj_cb.findData(identities["Invoice Tomteboda ÄTA"]))
                report = window.generate_invoice()
                totals = report['totals']
                self.assertEqual(totals['hours'], 4)
                self.assertEqual(totals['without_ob_hours'], 2)
                self.assertEqual(totals['ob_categories'], {'weekday_early_evening': 2})
                self.assertEqual(totals['work_amount'], 2080)
                self.assertEqual(totals['transport_amount'], 450)
                self.assertEqual(totals['known_subtotal'], 2530)
                self.assertIsNone(totals['ob_amount'])
                self.assertIsNone(totals['invoice_total'])
                self.assertEqual(report['prepared_rows'][0]['rate'], 520)
                self.assertIn('⏳ В работе', report['prepared_rows'][0]['status'])
                reports_dir = Path(self._case.name) / 'ob-only-pdf'
                with patch.object(self.desktop, 'REPORTS_DIR', str(reports_dir)), \
                     patch.object(window, '_collect_journal_translations', return_value={ob_id: 'Bevarad manuell kommentar'}), \
                     patch.object(self.desktop.os, 'startfile', create=True), \
                     patch.object(self.desktop.QMessageBox, 'information'), \
                     patch.object(self.desktop.QMessageBox, 'critical') as errors:
                    window.export_invoice_to_pdf()
                    from pypdf import PdfReader
                    pdf = next(reports_dir.glob('*.pdf'))
                    text = '\n'.join(page.extract_text() or '' for page in PdfReader(pdf).pages)
                    self.assertIn('Pågående', text)
                    self.assertIn('OB-tillägg', text)
                    self.assertIn('Ej fastställt', text)
                    self.assertIn('2 080,00', text)
                    self.assertNotIn('HOTEL', text)
                    self.assertNotIn('1,229.00 SEK', text)
                    self.assertNotIn('Начисления сотруднику', text)
                    self.assertIn('Arbetsjournal', text)
                    self.assertIn('PRELIMINÄRT', text)
                    self.assertIn('Preliminärt totalbelopp', text)
                    self.assertIn('utan ej fastställda priser eller OB-tillägg', text)
                    self.assertIn('2 530,00 SEK', text)
                    payroll = report['payroll']['Report worker']
                    self.assertEqual(payroll['base_amount'], 983.2)
                    self.assertEqual(payroll['ob_premium'], 245.8)
                    self.assertEqual(payroll['total'], 1229)
                    self.assertNotIn('1 229', text)
                    errors.assert_not_called()
                window.inv_obj_cb.setCurrentIndex(window.inv_obj_cb.findData(identities["Invoice HOTEL Djurgården"]))
                hotel = window.generate_invoice()
                self.assertEqual(hotel['totals']['hours'], 8)
                self.assertEqual(hotel['totals']['work_amount'], 4160)
                self.assertEqual(hotel['totals']['invoice_total'], 4610)
                self.assertEqual(totals['hours'] + hotel['totals']['hours'], 12)
                self.assertIn('✅ Выставлен', hotel['prepared_rows'][0]['status'])
            conn = self.desktop.get_db_connection()
            self.assertEqual(list(conn.iterdump()), before)
            conn.close()
        finally:
            window.close()
            window.deleteLater()

    def test_invoice_unknown_prices_and_missing_ob_segments_remain_viewable(self):
        conn = self.desktop.get_db_connection()
        object_id = conn.execute("""
            INSERT INTO objects (company,name,rate,transport_rate,sync_id,is_active)
            VALUES ('Renatur','Unpriced interval',0,0,?,1)
        """, (str(uuid.uuid4()),)).lastrowid
        conn.execute("""
            INSERT INTO shifts (date,employee,company,object_name,hours,rate,transport,invoiced,entry_type,sync_id)
            VALUES ('2026-10-21','Unpriced worker','Renatur','Unpriced interval',4,0,1,0,'ob',?)
        """, (str(uuid.uuid4()),))
        conn.commit()
        before = list(conn.iterdump())
        conn.close()
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_invoice = self.desktop.QWidget()
        try:
            window.setup_invoice_tab()
            window.inv_comp_cb.setCurrentText('Renatur')
            window.inv_obj_cb.setCurrentIndex(window.inv_obj_cb.findData(object_id))
            report = window.generate_invoice()
            self.assertEqual(report['totals']['hours'], 4)
            for field in ('work_amount', 'transport_amount', 'ob_amount', 'invoice_total', 'without_ob_hours'):
                self.assertIsNone(report['totals'][field])
            self.assertIn('Ej fastställbart', window.table_invoice.item(0, 3).text())
            self.assertTrue(report['warnings'])
            self.assertIn('PRELIMINÄRT', window.invoice_summary_note.text())
            self.assertTrue(any('segment' in warning for warning in report['warnings']))
            self.assertTrue(any('Historiska kundpriser' in warning for warning in report['warnings']))
            conn = self.desktop.get_db_connection()
            self.assertEqual(list(conn.iterdump()), before)
            conn.execute('DROP TABLE ob_segments')
            conn.commit()
            before = list(conn.iterdump())
            conn.close()
            missing_schema = window.generate_invoice()
            self.assertEqual(missing_schema['totals']['hours'], 4)
            self.assertIsNone(missing_schema['totals']['without_ob_hours'])
            self.assertIsNone(missing_schema['totals']['invoice_total'])
            window.fast_pris_check.setChecked(True)
            window.fast_pris_input.setText('5000')
            fixed = window.generate_invoice()
            self.assertEqual(fixed['totals']['invoice_total'], 5000)
            self.assertIsNone(fixed['totals']['ob_amount'])
            window.fast_pris_input.clear()
            with patch.object(self.desktop.QMessageBox, 'warning') as warning:
                self.assertIsNone(window.generate_invoice())
                warning.assert_called_once()
            conn = self.desktop.get_db_connection()
            self.assertEqual(list(conn.iterdump()), before)
            conn.close()
        finally:
            window.close()
            window.deleteLater()

    def test_invoice_rename_keeps_historical_shifts_without_rewriting_them(self):
        desktop_conn = self.desktop.get_db_connection()
        sync_id = str(uuid.uuid4())
        object_id = desktop_conn.execute("""
            INSERT INTO objects (company,name,markning,rate,transport_rate,sync_id,sync_version,is_active)
            VALUES ('Renatur','Invoice old name','p1038',425,450,?,1,1)
        """, (sync_id,)).lastrowid
        desktop_conn.execute("""
            INSERT INTO shifts (date,employee,company,object_name,hours,rate,transport,entry_type,
                                sync_id,work_rate_cache,work_transport_cache)
            VALUES ('2026-10-31','Historical worker','Renatur','Invoice old name',8,425,1,'work',?,425,1)
        """, (str(uuid.uuid4()),))
        desktop_conn.commit()
        original = [tuple(row) for row in desktop_conn.execute('SELECT * FROM shifts')]
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_invoice = self.desktop.QWidget()
        window.setup_invoice_tab()
        try:
            window.inv_comp_cb.setCurrentText('Renatur')
            window.inv_obj_cb.setCurrentIndex(window.inv_obj_cb.findData(object_id))
            self.desktop.merge_server_object_catalog(desktop_conn.cursor(), [{
                'id': 9003, 'sync_id': sync_id, 'company': 'Renatur', 'name': 'Invoice new name',
                'markning': 'p1039', 'rate': 425, 'transport_rate': 450, 'is_active': 1, 'sync_version': 2,
            }])
            desktop_conn.commit()
            self.desktop.init_db_once()
            window.load_dropdowns()
            report = window.generate_invoice()
            self.assertEqual(report['object_id'], object_id)
            self.assertEqual(report['object'], 'Invoice new name')
            self.assertEqual(report['rows'][0][:3], ('Historical worker', 8, 1))
            self.assertEqual([tuple(row) for row in desktop_conn.execute('SELECT * FROM shifts')], original)
        finally:
            desktop_conn.close()
            window.close()
            window.deleteLater()

    def test_invoice_ob_rule_description_uses_period_and_holiday_rules(self):
        categories = {
            'weekday_early_evening': 'OB1',
            'weekday_late_evening_night': 'OB2',
        }
        weekday_description = self.desktop.describe_category_time_ranges(
            '2026-10-19', '2026-10-19', categories
        )
        self.assertEqual(weekday_description['OB1'], 'Måndag 18:00–20:00')
        self.assertEqual(weekday_description['OB2'], 'Måndag 20:00–24:00')

        saturday_description = self.desktop.describe_category_time_ranges(
            '2026-10-24', '2026-10-24', categories
        )
        self.assertEqual(
            saturday_description['OB1'],
            'Ej tillämpligt enligt reglerna för vald period',
        )
        self.assertEqual(
            saturday_description['OB2'],
            'Ej tillämpligt enligt reglerna för vald period',
        )

        from ob_calculator import load_ob_rules

        holiday_rules = load_ob_rules()
        holiday_rules['holidays']['2026-10-19'] = [
            {'start': '00:00', 'end': '19:00', 'category': 'weekday_day',
             'premium_percent': 0, 'multiplier': 1.0},
            {'start': '19:00', 'end': '21:00', 'category': 'weekday_early_evening',
             'premium_percent': 50, 'multiplier': 1.5},
            {'start': '21:00', 'end': '24:00', 'category': 'weekday_late_evening_night',
             'premium_percent': 70, 'multiplier': 1.7},
        ]
        holiday_description = self.desktop.describe_category_time_ranges(
            '2026-10-19', '2026-10-19', categories, rules=holiday_rules
        )
        self.assertEqual(holiday_description['OB1'], 'Helgdag 2026-10-19 19:00–21:00')
        self.assertEqual(holiday_description['OB2'], 'Helgdag 2026-10-19 21:00–24:00')
        self.assertEqual(
            self.desktop.SmartReportApp._invoice_ob_rule_caption('OB1', holiday_description['OB1']),
            'OB1 (19:00–21:00) — Helgdag 2026-10-19 19:00–21:00',
        )

    def test_expense_order_upload_reviews_pages_and_queues_explicit_allocations(self):
        from expense_documents import ExtractionResult

        connection = self.server.get_db_connection()
        connection.execute(
            "INSERT INTO meta_objects (company, name, markning, rate, transport_rate, is_active) "
            "VALUES ('Renatur', 'Order UI object', 'P1022', 0, 0, 1)"
        )
        object_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.commit()
        connection.close()

        class Response:
            def __init__(self, response):
                self.response = response
                self.status_code = response.status_code
                self.ok = response.status_code < 400

            def json(self):
                return self.response.get_json()

        def fake_request(method, url, **kwargs):
            return Response(self.server_client.open(
                urlsplit(url).path, method=method, headers=kwargs.get("headers", {}),
                query_string=kwargs.get("params"), json=kwargs.get("json"), data=kwargs.get("data"),
            ))

        pages = (
            "Proj.nr.: P1022\nOrder nr: A\nSumma order: 100.00\nMoms order: 25.00",
            "Proj.nr.: P1022\nOrder nr: B\nSumma order: 50.00\nMoms order: 6.00",
            "Proj.nr.: 1022\nOrder nr: C\nSumma order: 20.00",
        )
        extraction = ExtractionResult("\n".join(pages), "test text", 1.0, pages)
        source = Path(self._case.name) / "orders.pdf"
        source.write_bytes(b"isolated synthetic invoice")
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_expenses = self.desktop.QWidget()
        window.tab_object_expenses = self.desktop.QWidget()
        try:
            with patch.object(self.desktop.requests, "request", side_effect=fake_request):
                window.setup_expenses_tab()
                window.setup_object_expenses_tab()

            def review_and_confirm(dialog):
                table = dialog.findChild(self.desktop.QTableWidget, "invoiceAllocations")
                self.assertIsNotNone(table)
                self.assertEqual(table.rowCount(), 3)
                dialog.findChild(self.desktop.QPushButton, "invoiceAddAllocation").click()
                self.assertEqual(table.rowCount(), 4)
                table.setCurrentCell(3, 0)
                dialog.findChild(self.desktop.QPushButton, "invoiceRemoveAllocation").click()
                self.assertEqual(table.rowCount(), 3)
                self.assertEqual(table.cellWidget(0, 3).currentData(), object_id)
                self.assertEqual(table.cellWidget(2, 2).currentData(), "unselected")
                confirm = dialog.findChild(self.desktop.QPushButton, "invoiceConfirm")
                self.assertFalse(confirm.isEnabled())
                confirm.click()
                connection = self.desktop.get_db_connection()
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM expense_document_outbox").fetchone()[0], 0)
                connection.close()
                table.cellWidget(2, 2).setCurrentIndex(table.cellWidget(2, 2).findData("company"))
                table.cellWidget(2, 4).setText("Инструмент")
                self.assertTrue(confirm.isEnabled())
                confirm.click()
                return dialog.result()

            with patch.object(self.desktop.QFileDialog, "getOpenFileNames", return_value=([str(source)], "")), \
                 patch("expense_documents.extract_local_document", return_value=extraction), \
                 patch.object(self.desktop, "parse_invoice_pages", return_value={
                     "supplier_name": "UI order supplier", "document_date": "2026-10-17",
                     "currency_code": "SEK", "net_amount": "170.00", "vat_amount": "31.00",
                     "gross_amount": "201.00", "review_reasons": [], "allocation_basis": "net",
                     "rounding_amount": "0.00", "amount_due": "201.00", "invoice_number": "UI-123",
                 }), \
                 patch.object(self.desktop, "load_document_preview_pixmap", return_value=self.desktop.QPixmap()), \
                 patch.object(self.desktop.QDialog, "exec", new=review_and_confirm), \
                 patch.object(self.desktop.QMessageBox, "warning") as warnings, \
                 patch.object(self.desktop.QMessageBox, "information"), \
                 patch.object(window, "background_sync_with_cloud"), \
                 patch.object(self.desktop.requests, "request", side_effect=fake_request):
                window.upload_expense_document()

            connection = self.desktop.get_db_connection()
            queued = connection.execute("SELECT payload_json, attachment_paths_json FROM expense_document_outbox").fetchone()
            connection.close()
            self.assertIsNotNone(queued)
            payload = json.loads(queued["payload_json"])
            self.assertEqual(payload["verification_status"], "confirmed")
            self.assertEqual(len(payload["allocations"]), 3)
            self.assertEqual(payload["allocations"][0]["net_amount"], "100.00")
            self.assertIsNone(payload["allocations"][2]["vat_amount"])
            self.assertEqual(payload["allocations"][2]["destination"], "company")
            self.assertEqual([item["sources"][0]["page"] for item in payload["order_proposals"]], [1, 2, 3])
            self.assertEqual(payload["order_proposals"][2]["project_code"], "1022")
            self.assertEqual(len(json.loads(queued["attachment_paths_json"])), 1)
        finally:
            window.close()
            window.deleteLater()

    def test_background_expense_read_is_nonblocking_cached_and_keeps_offline_data(self):
        import threading
        from PyQt6.QtCore import QEventLoop, QThread, QThreadPool, QTimer

        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window._async_expense_reads = True
        window._expense_read_cache = {}
        window._expense_read_attempts = {}
        window._expense_read_busy = False
        window._expense_read_task = None
        window._expense_online = False
        window.exp_api_status_lbl = self.desktop.QLabel()
        gate = threading.Event()
        calls = []
        document = {"id": 9, "client_id": "cached-doc", "supplier_name": "Cached Supplier", "allocations": []}

        def request(method, path, **kwargs):
            self.assertNotEqual(QThread.currentThread(), self.qt_app.thread())
            calls.append(path)
            if path == "/api/expense-objects":
                self.assertTrue(gate.wait(1))
                return {"objects": []}
            return {"documents": [document]}

        try:
            with patch.object(self.desktop, "request_expense_api", side_effect=request):
                first, online = window._expense_document_snapshot("2026-10-01", "2026-10-31")
                self.assertEqual(first, [])
                self.assertFalse(online)
                self.assertTrue(window._expense_read_busy)
                loop = QEventLoop()
                window._expense_read_task.signals.completed.connect(loop.quit)
                heartbeat = []
                QTimer.singleShot(0, lambda: (heartbeat.append(True), gate.set()))
                QTimer.singleShot(2000, loop.quit)
                loop.exec()
                self.qt_app.processEvents()
                self.assertTrue(heartbeat)
                self.assertFalse(window._expense_read_busy)
                second, online = window._expense_document_snapshot("2026-10-01", "2026-10-31")
                self.assertEqual(second[0]["id"], 9)
                self.assertTrue(online)
                self.assertEqual(calls, ["/api/expense-objects", "/api/expense-documents"])
                window._on_expense_read_completed(("2026-10-01", "2026-10-31"), [], [], "test offline")
                cached, online = window._expense_document_snapshot("2026-10-01", "2026-10-31")
                self.assertEqual(cached[0]["id"], 9)
                self.assertFalse(online)
                self.assertIn("test offline", window.exp_api_status_lbl.text())
                with patch.object(self.desktop.QThreadPool, "globalInstance") as pool:
                    window._invalidate_expense_reads()
                    window._schedule_expense_read(("2026-10-01", "2026-10-31"))
                    pool.return_value.start.assert_called_once()
                    window._invalidate_expense_reads()
                    self.assertTrue(window._expense_refresh_again)
                    window._on_expense_read_completed(("2026-10-01", "2026-10-31"), [], [document], "")
                    self.assertEqual(pool.return_value.start.call_count, 2)
        finally:
            gate.set()
            QThreadPool.globalInstance().waitForDone(2000)
            window.close()
            window.deleteLater()

    def test_table_bulk_fill_does_not_remeasure_each_insert(self):
        from PyQt6.QtWidgets import QHeaderView
        from desktop_ui import TableDelegate
        table = self.desktop.QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(["Объект", "Сумма (SEK)"])
        try:
            table.show()
            self.qt_app.processEvents()
            original = TableDelegate.sizeHint
            calls = []

            def measured(delegate, option, index):
                calls.append(index.row())
                return original(delegate, option, index)

            with patch.object(TableDelegate, "sizeHint", new=measured):
                table.setRowCount(80)
                for row in range(80):
                    table.setItem(row, 0, self.desktop.QTableWidgetItem("Synthetic object"))
                    table.setItem(row, 1, self.desktop.QTableWidgetItem("1234.50"))
                self.assertEqual(calls, [])
                self.qt_app.processEvents()
                self.assertEqual(table.horizontalHeader().sectionResizeMode(0), QHeaderView.ResizeMode.Stretch)
                self.assertEqual(table.horizontalHeader().sectionResizeMode(1), QHeaderView.ResizeMode.ResizeToContents)
                self.assertLess(len(calls), 3000)
        finally:
            table.close()
            table.deleteLater()

    def test_object_report_labels_net_and_reads_known_part_vat(self):
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_expenses = self.desktop.QWidget()
        window.tab_object_expenses = self.desktop.QWidget()
        document = {"id": 12, "client_id": "vat-report", "document_date": "2026-10-19", "supplier_name": "Beijer",
                    "invoice_number": "260993714021", "currency_code": "SEK", "verification_status": "confirmed",
                    "allocation_basis": "net", "net_amount": "73181.14", "vat_amount": "18295.31", "gross_amount": "91476.45",
                    "unallocated_gross": None, "allocations": [{"object_id": 5, "destination": "object",
                    "net_amount": "72495.92", "vat_amount": None, "gross_amount": None}]}
        objects = [{"id": 5, "name": "Object 5", "markning": "p1022", "company": "Privat"}]
        try:
            with patch.object(window, "_expense_server_objects", return_value=(objects, True)):
                window.setup_expenses_tab()
                window.setup_object_expenses_tab()
            with patch.object(window, "_expense_document_snapshot", return_value=([document], True)):
                combo = window.object_expenses_object
                combo.setCurrentIndex(combo.findData(5))
                window.load_object_expense_overview()
                self.assertEqual(window.object_expenses_table.horizontalHeaderItem(4).text(), "Итого с НДС")
                self.assertEqual(window.object_expenses_table.item(0, 4).text(), "Неизвестно")
                self.assertIn("72,495.92", window.object_expenses_table.item(0, 4).toolTip())
                self.assertEqual(window.object_expenses_table.item(0, 5).text(), "Неизвестно")
                document["allocations"][0].update(vat_amount="18124.00", gross_amount="90619.92")
                window.load_object_expense_overview()
                self.assertIn("90,619.92", window.object_expenses_table.item(0, 4).text())
                self.assertNotIn("без НДС", window.object_expenses_table.item(0, 4).text())
                self.assertIn("18,124.00", window.object_expenses_table.item(0, 5).text())
                self.assertIn("18,124.00", window.object_expenses_vat_lbl.text())
                self.assertIn("90,619.92", window.object_expenses_total_lbl.text())
                document["allocations"].append({"object_id": 13, "destination": "object", "net_amount": "685.22",
                                                "vat_amount": "171.31", "gross_amount": "856.53"})
                combo.addItem("Object 13", 13)
                combo.setCurrentIndex(combo.findData(13))
                window.load_object_expense_overview()
                self.assertIn("856.53", window.object_expenses_table.item(0, 4).text())
                self.assertIn("171.31", window.object_expenses_table.item(0, 5).text())
                self.assertIn("685.22", window.object_expenses_net_lbl.text())
        finally:
            window.close()
            window.deleteLater()

    def test_allocation_vat_uses_each_item_rate_and_reconciles_invoice(self):
        from expense_documents import allocation_vat_from_items, normalize_net_allocations, net_invoice_issues

        def cell(text, left, top):
            return {"text": text, "x0": left, "x1": left + len(text) * 4, "y0": top, "y1": top + 8}

        rows = [cell("Rad", 10, 20), cell("Pris", 130, 20), cell("Moms %", 220, 20), cell("Summa", 320, 20),
                cell("Order nr: A-1", 10, 50)]
        for top, number, net, rate in ((80, "10", "100.00", "25.00"), (100, "20", "50.00", "12.00")):
            rows.extend([cell(number, 10, top), cell("999.00", 130, top), cell(rate, 220, top), cell(net, 320, top)])
        rows += [cell("Summa order", 10, 120), cell("150.00", 320, 120), cell("Order nr: B-1", 10, 140)]
        for top, number, net, rate in ((160, "10", "100.00", "0.00"), (180, "20", "33.33", "6.00")):
            rows.extend([cell(number, 10, top), cell("999.00", 130, top), cell(rate, 220, top), cell(net, 320, top)])
        rows += [cell("Summa order", 10, 200), cell("133.33", 320, 200)]
        orders = [{"order_id": "A-1", "net_amount": "150.00", "destination": "object", "object_id": 1},
                  {"order_id": "B-1", "net_amount": "133.33", "destination": "company", "object_id": None}]
        pages = [{"page": 1, "elements": rows}]
        result = allocation_vat_from_items(pages, orders, "283.33", "33.00")
        self.assertEqual([row["vat_amount"] for row in result], ["31.00", "2.00"])
        self.assertEqual([row["gross_amount"] for row in result], ["181.00", "135.33"])
        self.assertEqual(result[1]["vat_items"][0]["vat_amount"], "0.00")
        self.assertEqual(len(normalize_net_allocations(result)), 2)
        with self.assertRaisesRegex(ValueError, "не совпадает"):
            allocation_vat_from_items(pages, orders, "283.33", "33.01")
        rows[:] = [value for value in rows if not (value["x0"] == 220 and value["y0"] == 180)]
        with self.assertRaises(ValueError):
            allocation_vat_from_items(pages, orders, "283.33", "33.00")
        issues = net_invoice_issues({"net_amount": "283.33", "vat_amount": "33.01", "amount_due": "316.34"}, result)
        self.assertTrue(any("НДС распределённых частей" in issue for issue in issues))

    def test_table_presentation_keeps_model_signals_and_active_editor(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QColor
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QHeaderView, QStyleOptionViewItem

        table = self.desktop.QTableWidget(1, 4)
        table.setStyleSheet(self.desktop.DARK_THEME_QSS)
        table.setHorizontalHeaderLabels(["Дата", "Объект", "Сумма (SEK)", "Статус"])
        changes = []
        table.cellChanged.connect(lambda row, column: changes.append((row, column)))
        values = ["2026-10-19", "Очень длинное название объекта и примечание для проверки переноса текста " * 2,
                  "1234567890.12", "Требует проверки"]
        for column, value in enumerate(values):
            item = self.desktop.QTableWidgetItem(value)
            item.setData(Qt.ItemDataRole.UserRole, "stable-record-id")
            table.setItem(0, column, item)
        table.item(0, 3).setBackground(QColor("#276749"))
        baseline = len(changes)
        try:
            table.resize(900, 360)
            table.show()
            self.qt_app.processEvents()
            self.assertEqual(len(changes), baseline)
            self.assertTrue(table.alternatingRowColors())
            self.assertGreaterEqual(table.rowHeight(0), 32)
            self.assertEqual(table.horizontalScrollBar().maximum(), 0)
            self.assertEqual(table.horizontalHeader().sectionResizeMode(1), QHeaderView.ResizeMode.Stretch)
            self.assertEqual(table.horizontalHeader().sectionResizeMode(2), QHeaderView.ResizeMode.ResizeToContents)
            table.resize(300, 360)
            self.qt_app.processEvents()
            table.refresh_layout()
            self.assertEqual(table.horizontalHeader().sectionResizeMode(1), QHeaderView.ResizeMode.ResizeToContents)
            self.assertGreaterEqual(table.columnWidth(1), 120)
            self.assertGreater(table.horizontalScrollBar().maximum(), 0)
            table.resize(900, 360)
            self.qt_app.processEvents()
            self.assertEqual(table.horizontalHeader().sectionResizeMode(1), QHeaderView.ResizeMode.Stretch)
            for column, flag in ((0, Qt.AlignmentFlag.AlignHCenter), (1, Qt.AlignmentFlag.AlignLeft),
                                 (2, Qt.AlignmentFlag.AlignRight), (3, Qt.AlignmentFlag.AlignHCenter)):
                option = QStyleOptionViewItem()
                table.itemDelegate().initStyleOption(option, table.model().index(0, column))
                self.assertTrue(option.displayAlignment & flag)
            table.setCurrentCell(0, 2)
            table.editItem(table.item(0, 2))
            self.qt_app.processEvents()
            editor = table.findChild(self.desktop.QLineEdit)
            self.assertIsNotNone(editor)
            table.resize(760, 360)
            self.qt_app.processEvents()
            self.assertTrue(editor.isVisible())
            editor.setText("88.90")
            QTest.keyClick(editor, Qt.Key.Key_Return)
            self.qt_app.processEvents()
            self.assertEqual(table.item(0, 2).text(), "88.90")
            self.assertEqual(len(changes), baseline + 1)
            self.assertEqual(table.item(0, 2).data(Qt.ItemDataRole.UserRole), "stable-record-id")
            self.assertEqual(table.item(0, 3).background().color(), QColor("#276749"))
            self.assertEqual(table.item(0, 1).text(), values[1])
        finally:
            table.close()
            table.deleteLater()

    def test_existing_receipt_parser_reads_stated_totals_without_inventing_vat_or_debt(self):
        from expense_documents import parse_invoice_pages

        def cell(text, left, top):
            return {"text": text, "x0": left, "x1": left + len(text) * 4, "y0": top, "y1": top + 8}

        for gross, vat, net, rate in (("12761.25", "2552.25", "10209.00", "25"), ("112.00", "12.00", "100.00", "12")):
            with self.subTest(gross=gross):
                page = {"page": 1, "text": "Kvitto", "elements": [
                    cell("Kvitto", 250, 60), cell("2026-10-16", 510, 60),
                    cell("Totalt", 30, 670), cell("SEK " + gross, 470, 671),
                    cell(f"Varav moms ({rate}%)", 30, 690), cell("SEK " + vat, 490, 694),
                ]}
                fields = parse_invoice_pages([page])
                self.assertEqual(fields["document_type"], "receipt")
                self.assertEqual(fields["document_date"], "2026-10-16")
                self.assertEqual(fields["gross_amount"], gross)
                self.assertEqual(fields["vat_amount"], vat)
                self.assertEqual(fields["net_amount"], net)
                self.assertIsNone(fields["amount_due"])
                self.assertIsNone(fields["invoice_number"])
                self.assertEqual(fields["field_sources"]["net_amount"][0]["operation"], "gross_minus_vat")
                page["elements"] = page["elements"][:-2]
                missing = parse_invoice_pages([page])
                self.assertEqual(missing["gross_amount"], gross)
                self.assertIsNone(missing["vat_amount"])
                self.assertIsNone(missing["net_amount"])

    def test_credit_note_reads_negative_credit_and_explicit_zero_vat(self):
        from expense_documents import parse_invoice_pages

        fields = parse_invoice_pages([{"page": 1, "text": "Kreditfaktura\nSumma exklusive moms: -68,80 kr\nMoms: 0,00 kr\nSumma er tillgodo: -68,80 kr"}])
        self.assertEqual(fields["document_type"], "credit_note")
        self.assertEqual(fields["net_amount"], "-68.80")
        self.assertEqual(fields["vat_amount"], "0.00")
        self.assertEqual(fields["amount_due"], "-68.80")

    def test_currency_summary_totals_and_seller_registration_ignore_operator_details(self):
        from expense_documents import parse_invoice_pages

        def cell(text, left, top):
            return {"text": text, "x0": left, "x1": left + len(text) * 4, "y0": top, "y1": top + 8}

        def pages(vat):
            summary = [
                cell("Buyer Builds AB", 350, 130), cell("Buyer Road 4, 123 45 City", 350, 142),
                cell("Fakturanummer", 45, 170), cell("9042", 220, 170),
                cell("Totalbelopp momspliktigt", 45, 400), cell("701,23", 330, 400),
                cell("Totalbelopp ej momspliktigt", 45, 415), cell("40,10", 330, 415),
                cell("Totalbelopp inkl. moms i SEK", 45, 460), cell("818,93", 330, 460),
                cell("Totalbelopp i SEK", 45, 305), cell("818,93", 230, 305),
                cell("Ledger Services AB", 45, 710), cell("Examplegatan 9, 123 45 City", 45, 723),
                cell("Ledger Services org. nummer", 45, 741), cell("556000-1111", 45, 752),
            ]
            if vat is not None:
                summary += [cell("Momsbelopp i SEK", 45, 435), cell(vat, 330, 435)]
            details = [cell("Other Parking AB", 45, 570), cell("Box 18, 123 45 City", 45, 583),
                       cell("Moms", 45, 400), cell("9,99", 200, 400)]
            return [{"page": 1, "attachment_index": 0, "text": "", "elements": summary},
                    {"page": 2, "attachment_index": 0, "text": "", "elements": details}]

        result = parse_invoice_pages(pages("77,60"))
        self.assertEqual(result["supplier_name"], "Ledger Services AB")
        self.assertEqual(result["net_amount"], "741.33")
        self.assertEqual(result["vat_amount"], "77.60")
        self.assertEqual(result["amount_due"], "818.93")
        self.assertEqual(result["currency_code"], "SEK")
        self.assertEqual(result["field_sources"]["vat_amount"][0]["caption"], "Momsbelopp i SEK")
        result = parse_invoice_pages(pages(None))
        self.assertIsNone(result["vat_amount"])
        self.assertTrue(any("vat_amount" in issue for issue in result["review_reasons"]))

    def test_desktop_eleven_files_dispatches_one_invoice_per_file(self):
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        paths = [str(Path(self._case.name) / f"invoice-{index}.pdf") for index in range(11)]
        try:
            with patch.object(self.desktop.QFileDialog, "getOpenFileNames", return_value=(paths, "")), \
                 patch.object(self.desktop.QInputDialog, "getItem", return_value=("Отдельная фактура для каждого файла", True)), \
                 patch.object(window, "_upload_expense_document_paths") as upload:
                window.upload_expense_document()
            self.assertEqual([call.args[0] for call in upload.call_args_list], [[path] for path in paths])
        finally:
            window.close()
            window.deleteLater()

    def test_expense_upload_same_content_never_creates_second_confirmed_expense(self):
        source = Path(self._case.name) / "invoice.pdf"
        renamed = Path(self._case.name) / "invoice (2).pdf"
        source.write_bytes(b"same isolated original content")
        renamed.write_bytes(source.read_bytes())
        client_id = str(uuid.uuid4())
        payload = {"client_id": client_id, "verification_status": "confirmed", "supplier_name": "Isolated supplier",
                   "gross_amount": "125.00", "vat_amount": "25.00", "allocations": []}
        self.desktop.SmartReportApp.queue_expense_document_operation(SimpleNamespace(), client_id, "upsert", payload,
                                                                   attachment_paths=[str(source)])
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        local_document = {**payload, "local_attachment_paths": [str(source)]}
        try:
            with patch.object(window, "_expense_document_snapshot", return_value=([local_document], False)), \
                 patch.object(self.desktop.QFileDialog, "getOpenFileNames", return_value=([str(renamed)], "")), \
                 patch.object(self.desktop.QMessageBox, "information") as duplicate_message, \
                 patch.object(window, "_expense_document_review_dialog") as existing_review, \
                 patch("expense_documents.extract_local_document") as extract:
                window.upload_expense_document()
                window.upload_expense_document()
                self.assertEqual(existing_review.call_count, 2)
            self.assertEqual(duplicate_message.call_count, 2)
            extract.assert_not_called()
            connection = self.desktop.get_db_connection()
            records = connection.execute("SELECT payload_json FROM expense_document_outbox").fetchall()
            connection.close()
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0][0])["verification_status"], "confirmed")
            remote_document = {**payload, "attachments": [{"sha256": self.desktop.sha256_file(source)}]}
            with patch.object(window, "_expense_document_snapshot", return_value=([remote_document], True)), \
                 patch.object(self.desktop.QFileDialog, "getOpenFileNames", return_value=([str(renamed)], "")), \
                 patch.object(self.desktop.QMessageBox, "information") as remote_duplicate, \
                 patch.object(window, "_expense_document_review_dialog") as existing_review, \
                 patch("expense_documents.extract_local_document") as extract:
                window.upload_expense_document()
                existing_review.assert_called_once_with(document=remote_document)
            remote_duplicate.assert_called_once()
            extract.assert_not_called()
        finally:
            window.close()
            window.deleteLater()

    def test_expense_form_queues_manual_company_document(self):
        class Response:
            def __init__(self, response):
                self.response = response
                self.status_code = response.status_code
                self.ok = response.status_code < 400

            def json(self):
                return self.response.get_json()

        def fake_request(method, url, **kwargs):
            response = self.server_client.open(
                urlsplit(url).path,
                method=method,
                headers=kwargs.get("headers", {}),
                query_string=kwargs.get("params"),
                json=kwargs.get("json"),
                data=kwargs.get("data"),
            )
            return Response(response)

        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_expenses = self.desktop.QWidget()
        window.tab_object_expenses = self.desktop.QWidget()
        try:
            with patch.object(self.desktop.requests, "request", side_effect=fake_request):
                window.setup_expenses_tab()
                window.setup_object_expenses_tab()

            def fill_and_accept(dialog):
                dialog.findChild(self.desktop.QLineEdit, "expenseSupplier").setText("Manual supplier")
                dialog.findChild(self.desktop.QLineEdit, "expenseGrossInput").setText("125.00")
                dialog.findChild(self.desktop.QLineEdit, "expenseVatInput").setText("25.00")
                object_combo = dialog.findChild(self.desktop.QComboBox, "expenseObject")
                object_combo.setCurrentIndex(object_combo.findData("company"))
                window.load_expense_document_filters()
                self.assertEqual(
                    dialog.findChild(self.desktop.QLineEdit, "expenseGrossInput").text(),
                    "125.00",
                )
                self.assertEqual(
                    dialog.findChild(self.desktop.QLineEdit, "expenseSupplier").text(),
                    "Manual supplier",
                )
                dialog.accept()
                return self.desktop.QDialog.DialogCode.Accepted

            with patch.object(self.desktop.QDialog, "exec", new=fill_and_accept), \
                 patch.object(window, "background_sync_with_cloud"), \
                 patch.object(self.desktop.QMessageBox, "information"), \
                 patch.object(self.desktop.requests, "request", side_effect=fake_request):
                self.assertTrue(window._expense_document_review_dialog(
                    parsed_fields={"currency_code": "SEK", "document_type": "manual"},
                    recognition_message="Распознавание не запускалось; заполните поля вручную.",
                ))

            desktop_conn = self.desktop.get_db_connection()
            outbox = desktop_conn.execute(
                "SELECT client_id, operation, payload_json FROM expense_document_outbox"
            ).fetchone()
            self.assertEqual(outbox["operation"], "upsert")
            payload = __import__("json").loads(outbox["payload_json"])
            self.assertEqual(payload["supplier_name"], "Manual supplier")
            self.assertEqual(payload["gross_amount"], "125.00")
            self.assertEqual(payload["vat_amount"], "25.00")
            self.assertEqual(payload["net_amount"], "100.00")
            self.assertEqual(payload["allocations"], [{
                "object_id": None, "gross_amount": "125.00", "vat_amount": "25.00",
            }])
            desktop_conn.close()
            self.assertEqual(window.table_supplier_documents.rowCount(), 1)
            self.assertIn("Общие расходы фирмы", window.table_supplier_documents.item(0, 3).text())
        finally:
            window.close()
            window.deleteLater()

    def test_expense_ocr_failure_still_saves_manual_correction_and_original(self):
        class Response:
            def __init__(self, response):
                self.response = response
                self.status_code = response.status_code
                self.ok = response.status_code < 400

            def json(self):
                return self.response.get_json()

        def fake_request(method, url, **kwargs):
            response = self.server_client.open(
                urlsplit(url).path, method=method,
                headers=kwargs.get("headers", {}),
                query_string=kwargs.get("params"),
                json=kwargs.get("json"), data=kwargs.get("data"),
            )
            return Response(response)

        source = Path(self._case.name) / "unreadable.png"
        source.write_bytes(b"synthetic unreadable image")
        expected_original = source.read_bytes()
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_expenses = self.desktop.QWidget()
        window.tab_object_expenses = self.desktop.QWidget()
        try:
            with patch.object(self.desktop.requests, "request", side_effect=fake_request):
                window.setup_expenses_tab()
                window.setup_object_expenses_tab()

            def fill_manual_values(dialog):
                dialog.findChild(self.desktop.QLineEdit, "expenseSupplier").setText("Corrected supplier")
                dialog.findChild(self.desktop.QLineEdit, "expenseGrossInput").setText("50.00")
                dialog.findChild(self.desktop.QLineEdit, "expenseVatInput").setText("10.00")
                dialog.findChild(self.desktop.QComboBox, "expenseObject").setCurrentIndex(
                    dialog.findChild(self.desktop.QComboBox, "expenseObject").findData("company")
                )
                dialog.accept()
                return self.desktop.QDialog.DialogCode.Accepted

            with patch.object(self.desktop.QFileDialog, "getOpenFileNames", return_value=([str(source)], "")), \
                 patch("expense_documents.extract_local_document", side_effect=ValueError("simulated OCR failure")), \
                 patch.object(self.desktop.QDialog, "exec", new=fill_manual_values), \
                 patch.object(window, "background_sync_with_cloud"), \
                 patch.object(self.desktop.QMessageBox, "information"), \
                 patch.object(self.desktop.requests, "request", side_effect=fake_request):
                window.upload_expense_document()

            desktop_conn = self.desktop.get_db_connection()
            outbox = desktop_conn.execute(
                "SELECT payload_json, attachment_paths_json FROM expense_document_outbox"
            ).fetchone()
            payload = json.loads(outbox["payload_json"])
            attachment_paths = json.loads(outbox["attachment_paths_json"])
            desktop_conn.close()
            self.assertEqual(payload["supplier_name"], "Corrected supplier")
            self.assertEqual(payload["gross_amount"], "50.00")
            self.assertEqual(payload["vat_amount"], "10.00")
            self.assertEqual(payload["verification_status"], "requires_review")
            self.assertEqual(len(attachment_paths), 1)
            self.assertEqual(Path(attachment_paths[0]).read_bytes(), expected_original)
        finally:
            window.close()
            window.deleteLater()

    def test_expense_multiple_photos_are_one_editable_document(self):
        class Response:
            def __init__(self, response):
                self.response = response
                self.status_code = response.status_code
                self.ok = response.status_code < 400

            def json(self):
                return self.response.get_json()

        def fake_request(method, url, **kwargs):
            response = self.server_client.open(
                urlsplit(url).path, method=method,
                headers=kwargs.get("headers", {}), query_string=kwargs.get("params"),
                json=kwargs.get("json"), data=kwargs.get("data"),
            )
            return Response(response)

        sources = []
        for index in range(2):
            path = Path(self._case.name) / f"page-{index + 1}.jpg"
            path.write_bytes(f"synthetic image {index + 1}".encode())
            sources.append(path)
        from expense_documents import ExtractionResult

        extraction_results = [
            ExtractionResult(
                text="Leverantör: OCR Supplier\nFakturadatum: 2026-10-17\nTotal inkl moms: 125.00",
                method="test OCR", confidence=0.9,
            ),
            ExtractionResult(
                text="Moms totalt: 25.00\nTotal inkl moms: 125.00",
                method="test OCR", confidence=0.9,
            ),
        ]
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_expenses = self.desktop.QWidget()
        window.tab_object_expenses = self.desktop.QWidget()
        try:
            with patch.object(self.desktop.requests, "request", side_effect=fake_request):
                window.setup_expenses_tab()
                window.setup_object_expenses_tab()

            def correct_and_accept(dialog):
                dialog.findChild(self.desktop.QLineEdit, "invoice_supplier_name").setText("Corrected supplier")
                dialog.findChild(self.desktop.QLineEdit, "invoice_net_amount").setText("100.00")
                dialog.findChild(self.desktop.QLineEdit, "invoice_vat_amount").setText("25.00")
                dialog.findChild(self.desktop.QLineEdit, "invoice_amount_due").setText("125.00")
                table = dialog.findChild(self.desktop.QTableWidget, "invoiceAllocations")
                table.item(0, 1).setText("100.00")
                table.cellWidget(0, 2).setCurrentIndex(table.cellWidget(0, 2).findData("company"))
                dialog.findChild(self.desktop.QPushButton, "invoiceSaveDraft").click()
                return dialog.result()

            with patch.object(self.desktop.QFileDialog, "getOpenFileNames", return_value=(list(map(str, sources)), "")), \
                  patch.object(self.desktop.QInputDialog, "getItem", return_value=("Страницы одной фактуры", True)), \
                 patch("expense_documents.extract_local_document", side_effect=extraction_results), \
                 patch.object(self.desktop.QDialog, "exec", new=correct_and_accept), \
                 patch.object(window, "background_sync_with_cloud"), \
                 patch.object(self.desktop.QMessageBox, "information"), \
                 patch.object(self.desktop.requests, "request", side_effect=fake_request):
                window.upload_expense_document()

            desktop_conn = self.desktop.get_db_connection()
            outbox = desktop_conn.execute(
                "SELECT payload_json, attachment_paths_json FROM expense_document_outbox"
            ).fetchone()
            payload = json.loads(outbox["payload_json"])
            stored_paths = json.loads(outbox["attachment_paths_json"])
            desktop_conn.close()
            self.assertEqual(payload["supplier_name"], "Corrected supplier")
            self.assertEqual(payload["gross_amount"], "125.00")
            self.assertEqual(payload["vat_amount"], "25.00")
            self.assertIn("page-1.jpg", payload["recognized_text"])
            self.assertIn("page-2.jpg", payload["recognized_text"])
            self.assertEqual(len(stored_paths), 2)
            self.assertEqual(
                [Path(path).read_bytes() for path in stored_paths],
                [path.read_bytes() for path in sources],
            )
        finally:
            window.close()
            window.deleteLater()

    def test_document_gallery_shows_all_pdf_pages_and_multiple_photos(self):
        from reportlab.pdfgen import canvas

        for page_count in (1, 3, 4):
            pdf_path = Path(self._case.name) / f"pages-{page_count}.pdf"
            document = canvas.Canvas(str(pdf_path))
            for page in range(page_count):
                document.drawString(40, 760, f"Invoice page {page + 1}")
                document.showPage()
            document.save()

            gallery = self.desktop.DocumentGalleryDialog([pdf_path], pdf_path.name)
            try:
                self.assertEqual(len(gallery._pages), page_count)
                for page in range(page_count):
                    gallery.show_page(page)
                    self.assertIn(f"{page + 1} / {page_count}", gallery.page_label.text())
                    self.assertFalse(gallery.preview._image.pixmap().isNull())
            finally:
                gallery.close()

        photo_paths = []
        for index in range(2):
            path = Path(self._case.name) / f"photo-{index + 1}.png"
            pixmap = self.desktop.QPixmap(24, 24)
            pixmap.fill(self.desktop.QColor("#2b6cb0" if index == 0 else "#2f855a"))
            self.assertTrue(pixmap.save(str(path), "PNG"))
            photo_paths.append(path)
        gallery = self.desktop.DocumentGalleryDialog(photo_paths, "Photo bundle", initial_attachment=1)
        try:
            self.assertEqual(len(gallery._pages), 2)
            self.assertIn("2 / 2", gallery.page_label.text())
            gallery.show_page(1)
            self.assertIn("2 / 2", gallery.page_label.text())
            self.assertFalse(gallery.preview._image.pixmap().isNull())
        finally:
            gallery.close()

    def test_expense_form_splits_one_document_by_stable_object_ids(self):
        class Response:
            def __init__(self, response):
                self.response = response
                self.status_code = response.status_code
                self.ok = response.status_code < 400

            def json(self):
                return self.response.get_json()

        def fake_request(method, url, **kwargs):
            response = self.server_client.open(
                urlsplit(url).path, method=method,
                headers=kwargs.get("headers", {}),
                query_string=kwargs.get("params"),
                json=kwargs.get("json"), data=kwargs.get("data"),
            )
            return Response(response)

        server_conn = self.server.get_db_connection()
        object_ids = [server_conn.execute("""
            INSERT INTO meta_objects (sync_id,company,name,markning,rate,transport_rate,is_active,sync_version)
            VALUES (?, 'Renatur', ?, ?, 0, 0, 1, 0)
        """, (str(uuid.uuid4()), name, marking)).lastrowid
            for name, marking in (("Split object A", "p-split-a"), ("Split object B", "p-split-b"))
        ]
        server_conn.commit()
        server_conn.close()

        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_expenses = self.desktop.QWidget()
        window.tab_object_expenses = self.desktop.QWidget()
        try:
            with patch.object(self.desktop.requests, "request", side_effect=fake_request):
                window.setup_expenses_tab()
                window.setup_object_expenses_tab()

            def fill_split_and_accept(dialog):
                dialog.findChild(self.desktop.QLineEdit, "expenseSupplier").setText("Split supplier")
                dialog.findChild(self.desktop.QLineEdit, "expenseGrossInput").setText("125.00")
                dialog.findChild(self.desktop.QLineEdit, "expenseVatInput").setText("25.00")
                dialog.findChild(self.desktop.QComboBox, "expenseObject").setCurrentIndex(
                    dialog.findChild(self.desktop.QComboBox, "expenseObject").findData(object_ids[0])
                )
                dialog.findChild(self.desktop.QCheckBox, "expenseSplitByObjects").setChecked(True)
                table = dialog.findChild(self.desktop.QTableWidget, "expenseAllocations")
                table.item(0, 1).setText("60.00")
                table.item(0, 2).setText("12.00")
                dialog.findChild(self.desktop.QPushButton, "expenseAddAllocation").click()
                second_object = table.cellWidget(1, 0)
                second_object.setCurrentIndex(second_object.findData(object_ids[1]))
                table.item(1, 1).setText("65.00")
                table.item(1, 2).setText("13.00")
                dialog.accept()
                return self.desktop.QDialog.DialogCode.Accepted

            with patch.object(self.desktop.QDialog, "exec", new=fill_split_and_accept), \
                 patch.object(window, "background_sync_with_cloud"), \
                 patch.object(self.desktop.QMessageBox, "information"), \
                 patch.object(self.desktop.requests, "request", side_effect=fake_request):
                self.assertTrue(window._expense_document_review_dialog(
                    parsed_fields={"currency_code": "SEK", "document_type": "invoice"},
                    recognition_message="Распознавание выполнено; поля проверены вручную.",
                ))

            desktop_conn = self.desktop.get_db_connection()
            payload = self.desktop.json.loads(desktop_conn.execute(
                "SELECT payload_json FROM expense_document_outbox"
            ).fetchone()[0])
            desktop_conn.close()
            self.assertEqual(payload["allocations"], [
                {"object_id": object_ids[0], "gross_amount": "60.00", "vat_amount": "12.00"},
                {"object_id": object_ids[1], "gross_amount": "65.00", "vat_amount": "13.00"},
            ])
            self.assertEqual(
                sum(Decimal(item["gross_amount"]) for item in payload["allocations"]),
                Decimal("125.00"),
            )
            self.assertEqual(
                sum(Decimal(item["vat_amount"]) for item in payload["allocations"]),
                Decimal("25.00"),
            )
        finally:
            window.close()
            window.deleteLater()

    def test_net_invoice_rounding_company_and_unknown_per_order_vat(self):
        from expense_documents import net_invoice_issues, normalize_net_allocations, migrate_invoice_net_schema, invoice_net_schema_ready

        fields = {"net_amount": "3049.95", "vat_amount": "762.49", "rounding_amount": "-0.44", "amount_due": "3812.00"}
        rows = [
            {"original_marker": "P1035", "destination": "object", "object_id": 1, "net_amount": "2000.00"},
            {"original_marker": "P1035", "destination": "object", "object_id": 1, "net_amount": "340.99"},
            {"original_marker": "ROMAN BASHCHUK", "destination": "company", "object_id": None,
             "net_amount": "708.96", "expense_category": "Инструмент"},
        ]
        self.assertEqual(net_invoice_issues(fields, rows), [])
        allocations = normalize_net_allocations(rows)
        self.assertEqual([item["net_amount"] for item in allocations], ["2340.99", "708.96"])
        self.assertTrue(all(item["vat_amount"] is None and item["gross_amount"] is None for item in allocations))
        self.assertIsNone(allocations[1]["object_id"])
        self.assertEqual(allocations[1]["expense_category"], "Инструмент")
        self.assertTrue(net_invoice_issues({**fields, "rounding_amount": "0.00"}, rows))
        self.assertTrue(net_invoice_issues({**fields, "vat_amount": None}, rows))
        self.assertTrue(net_invoice_issues(fields, [{**rows[0], "destination": "unselected"}]))
        connection = self.server.get_db_connection()
        try:
            self.assertFalse(invoice_net_schema_ready(connection))
            object_id = int(connection.execute("SELECT id FROM meta_objects LIMIT 1").fetchone()[0])
            connection.execute("""
                INSERT INTO expense_documents (client_id, supplier_name, gross_amount, net_amount, vat_amount, created_at, updated_at)
                VALUES ('preserve-before-net', 'Legacy', '125.00', '100.00', '25.00', 'test', 'test')
            """)
            document_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            connection.execute("INSERT INTO expense_allocations (document_id,object_id,allocated_gross,allocated_net,allocated_vat) VALUES (?,?, '125.00','100.00','25.00')",
                               (document_id, object_id))
            before = tuple(connection.execute("SELECT id,document_id,object_id,allocated_gross,allocated_net,allocated_vat,allocation_note FROM expense_allocations").fetchone())
            connection.commit()
            migrate_invoice_net_schema(connection)
            self.assertTrue(invoice_net_schema_ready(connection))
            after = tuple(connection.execute("SELECT id,document_id,object_id,allocated_gross,allocated_net,allocated_vat,allocation_note FROM expense_allocations").fetchone())
            self.assertEqual(before, after)
            migrate_invoice_net_schema(connection)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            connection.close()

    def test_net_invoice_without_explicit_schema_stays_in_outbox(self):
        from expense_documents import invoice_net_schema_ready

        connection = self.server.get_db_connection()
        self.assertFalse(invoice_net_schema_ready(connection))
        connection.close()
        client_id = str(uuid.uuid4())
        self.desktop.SmartReportApp.queue_expense_document_operation(SimpleNamespace(), client_id, "upsert", {
            "client_id": client_id, "allocation_basis": "net", "supplier_name": "Schema test AB",
            "net_amount": "100.00", "vat_amount": "25.00", "rounding_amount": "0.00", "amount_due": "125.00",
            "verification_status": "draft", "allocations": [{"destination": "company", "net_amount": "100.00"}],
        })
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append(method)
            response = self.server_client.open(urlsplit(url).path, method=method, headers=kwargs.get("headers", {}))
            return SimpleNamespace(status_code=response.status_code, json=response.get_json)

        with patch.object(self.desktop.requests, "request", side_effect=fake_request):
            result = self.desktop.SmartReportApp._sync_expense_document_outbox(SimpleNamespace())
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(calls, ["GET"])
        connection = self.desktop.get_db_connection()
        self.assertEqual(connection.execute("SELECT state FROM expense_document_outbox WHERE client_id=?", (client_id,)).fetchone()[0], "pending")
        connection.close()
        connection = self.server.get_db_connection()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM expense_documents").fetchone()[0], 0)
        self.assertFalse(invoice_net_schema_ready(connection))
        connection.close()

    def test_invoice_scanned_pdf_uses_local_ocr_for_all_missing_pages(self):
        from reportlab.pdfgen import canvas
        from expense_documents import extract_local_document, parse_invoice_pages
        from unittest.mock import Mock

        image = Path(self._case.name) / "scan.png"
        pixmap = self.desktop.QPixmap(100, 100)
        pixmap.fill(self.desktop.QColor("white"))
        pixmap.save(str(image))
        path = Path(self._case.name) / "scanned.pdf"
        pdf = canvas.Canvas(str(path))
        for _index in range(2):
            pdf.drawImage(str(image), 40, 400, 300, 300)
            pdf.showPage()
        pdf.save()

        def detections(lines):
            return [([[20, index * 30], [450, index * 30], [450, index * 30 + 15], [20, index * 30 + 15]], line, 0.96)
                    for index, line in enumerate(lines)]

        engine = Mock(side_effect=[
            (detections(["Supplier: SCAN MATERIAL AB", "Fakturanummer: SCAN-123", "Fakturadatum: 15.10.2026",
                         "Valuta: SEK", "Proj.nr.: P1022", "Order nr: S-1", "Summa order: 100.00"]), None),
            (detections(["Summa exkl. moms: 100.00", "Moms: 25.00", "Avrundning: 0.00", "Att betala: 125.00"]), None),
        ])
        extraction = extract_local_document(path, ocr_engine=engine)
        self.assertEqual(engine.call_count, 2)
        self.assertEqual(extraction.method, "локальный OCR")
        fields = parse_invoice_pages([{"page": index + 1, "text": text, "elements": extraction.page_elements[index]}
                                      for index, text in enumerate(extraction.pages)])
        self.assertEqual(fields["net_amount"], "100.00")
        self.assertEqual(fields["vat_amount"], "25.00")
        self.assertEqual(fields["amount_due"], "125.00")
        self.assertEqual(fields["field_sources"]["vat_amount"][0]["page"], 2)

    def test_invoice_unknown_format_and_missing_tax_remain_review_only(self):
        from expense_documents import parse_invoice_pages, net_invoice_issues

        unknown = parse_invoice_pages([{"page": 1, "text": "Customer ROMAN BASHCHUK\n280,96 25 %\nAccount summary"}])
        self.assertIsNone(unknown["supplier_name"])
        self.assertIsNone(unknown["vat_amount"])
        self.assertIsNone(unknown["net_amount"])
        self.assertTrue(unknown["review_reasons"])
        missing_tax = parse_invoice_pages([{"page": 1, "text":
            "Supplier: SUPPLIER AB\nFakturanummer: A123\nFakturadatum: 15.10.2026\nValuta: SEK\n"
            "Summa exkl. moms: 100.00\nAtt betala: 125.00"}])
        self.assertIsNone(missing_tax["vat_amount"])
        self.assertTrue(net_invoice_issues(missing_tax, [{"destination": "company", "net_amount": "100.00"}]))

    def test_net_invoice_ui_normal_window_preserves_edits_and_sources(self):
        from PyQt6.QtGui import QPainter, QFont

        photos = []
        for index in range(2):
            path = Path(self._case.name) / f"invoice-view-{index}.png"
            pixmap = self.desktop.QPixmap(240, 320)
            pixmap.fill(self.desktop.QColor("#ffffff" if index == 0 else "#f0f0f0"))
            painter = QPainter(pixmap)
            painter.setFont(QFont("Segoe UI", 10))
            painter.setPen(self.desktop.QColor("#202020"))
            painter.drawText(16, 30, f"Synthetic invoice / page {index + 1}")
            painter.drawText(16, 60, "BEIJER BYGGMATERIAL AB")
            painter.drawText(16, 90, "260406399329")
            painter.end()
            pixmap.save(str(path))
            photos.append(str(path))
        connection = self.server.get_db_connection()
        connection.execute("INSERT INTO meta_objects (company,name,markning,is_active) VALUES ('Renatur','Invoice P1035','P1035',1)")
        object_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.commit()
        connection.close()
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window._expense_objects = [{"id": object_id, "name": "Invoice P1035", "markning": "P1035"}]
        fields = {"allocation_basis": "net", "supplier_name": "BEIJER BYGGMATERIAL AB",
                  "invoice_number": "260406399329", "document_date": "2026-10-15", "currency_code": "SEK",
                  "net_amount": "3049.95", "vat_amount": "762.49", "rounding_amount": "-0.44", "amount_due": "3812.00",
                  "order_proposals": [
                      {"project_code": "P1035", "original_marker": "P1035", "net_amount": "2340.99",
                       "sources": [{"page": 1, "attachment_index": 0}]},
                      {"project_code": None, "original_marker": "ROMAN BASHCHUK", "net_amount": "708.96",
                       "sources": [{"page": 1, "attachment_index": 1}]},
                  ]}

        def review(dialog):
            dialog.resize(1000, 740)
            dialog.show()
            self.qt_app.processEvents()
            table = dialog.findChild(self.desktop.QTableWidget, "invoiceAllocations")
            confirm = dialog.findChild(self.desktop.QPushButton, "invoiceConfirm")
            self.assertFalse(confirm.isEnabled())
            self.assertEqual(table.cellWidget(1, 2).currentData(), "unselected")
            self.assertEqual(table.cellWidget(0, 3).currentData(), object_id)
            table.cellWidget(1, 2).setCurrentIndex(table.cellWidget(1, 2).findData("company"))
            table.cellWidget(1, 4).setText("Инструмент")
            supplier = dialog.findChild(self.desktop.QLineEdit, "invoice_supplier_name")
            supplier.setText("BEIJER BYGGMATERIAL AB / проверено")
            table.cellWidget(1, 5).click()
            gallery = dialog.findChild(self.desktop.DocumentGalleryDialog)
            self.assertEqual(gallery._page_index, 1)
            self.assertTrue(supplier.text().endswith("проверено"))
            self.assertTrue(confirm.isEnabled())
            self.assertLessEqual(dialog.height(), 760)
            self.assertTrue(confirm.isVisible())
            self.assertTrue(table.isVisible())
            self.assertTrue(dialog.rect().contains(confirm.mapTo(dialog, confirm.rect().bottomRight())))
            self.assertIn("осталось 0.00", dialog.findChild(self.desktop.QLabel, "invoiceDistribution").text())
            screenshot = os.environ.get("SMARTREPORT_TEST_SCREENSHOT")
            if screenshot:
                self.assertTrue(dialog.grab().save(screenshot))
            confirm.click()
            return dialog.result()

        try:
            with patch.object(self.desktop.QDialog, "exec", new=review), \
                 patch.object(window, "load_expense_documents"), \
                 patch.object(window, "background_sync_with_cloud"):
                self.assertTrue(window._review_net_invoice({}, photos, fields))
            connection = self.desktop.get_db_connection()
            payload = json.loads(connection.execute("SELECT payload_json FROM expense_document_outbox").fetchone()[0])
            connection.close()
            self.assertEqual(payload["allocations"][1]["expense_category"], "Инструмент")
            self.assertIsNone(payload["allocations"][1]["object_id"])
            self.assertEqual(payload["rounding_amount"], "-0.44")
            self.assertIsNone(payload["allocations"][1]["vat_amount"])
        finally:
            window.close()
            window.deleteLater()

    def test_net_invoice_api_sync_company_separation_and_rounding(self):
        from expense_documents import migrate_invoice_net_schema

        connection = self.server.get_db_connection()
        migrate_invoice_net_schema(connection)
        object_id = int(connection.execute("SELECT id FROM meta_objects WHERE is_active=1 LIMIT 1").fetchone()[0])
        connection.close()
        allocations = [
            {"original_marker": "P1035", "project_code": "P1035", "order_id": "A",
             "destination": "object", "object_id": object_id, "net_amount": "2340.99", "sources": []},
            {"original_marker": "ROMAN BASHCHUK", "project_code": None, "order_id": "B",
             "destination": "company", "object_id": None, "net_amount": "708.96",
             "expense_category": "Инструмент", "sources": []},
        ]
        payload = {
            "client_id": str(uuid.uuid4()), "supplier_name": "BEIJER BYGGMATERIAL AB",
            "invoice_number": "260406399329", "document_date": "2026-10-15", "currency_code": "SEK",
            "allocation_basis": "net", "net_amount": "3049.95", "vat_amount": "762.49",
            "rounding_amount": "-0.44", "amount_due": "3812.00", "gross_amount": "3812.44",
            "verification_status": "confirmed", "allocations": allocations, "order_proposals": allocations,
        }
        self.desktop.SmartReportApp.queue_expense_document_operation(SimpleNamespace(), payload["client_id"], "upsert", payload)
        self._worker()
        self._worker()
        headers = {"X-Sync-Key": "isolated-sync-test-key"}
        documents = self.server_client.get("/api/expense-documents", headers=headers).get_json()["documents"]
        self.assertEqual(len(documents), 1)
        document = documents[0]
        self.assertEqual(document["allocation_basis"], "net")
        self.assertEqual(document["net_amount"], "3049.95")
        self.assertEqual(document["amount_due"], "3812.00")
        self.assertEqual(document["rounding_amount"], "-0.44")
        self.assertEqual(document["vat_amount"], "762.49")
        self.assertTrue(document["fully_allocated"])
        self.assertTrue(all(row["vat_amount"] is None for row in document["allocations"]))
        self.assertIsNone(document["allocations"][1]["object_id"])
        self.assertEqual(document["allocations"][1]["expense_category"], "Инструмент")
        self.assertEqual(document["order_proposals"][1]["original_marker"], "ROMAN BASHCHUK")
        summary = self.server_client.get(f"/api/expense-objects/{object_id}/expenses", headers=headers).get_json()["summary"]["SEK"]
        self.assertEqual(summary["net"], "2340.99")
        self.assertIsNone(summary["vat"])
        self.assertIsNone(summary["gross"])
        connection = self.server.get_db_connection()
        self.assertIsNone(connection.execute("SELECT 1 FROM meta_objects WHERE name LIKE '%ROMAN%'").fetchone())
        connection.close()
        response = self.server_client.post("/api/expense-documents", headers=headers, json=payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["duplicate_request"])
        invalid = {**payload, "expected_version": document["sync_version"], "rounding_amount": "0.00"}
        response = self.server_client.put(f"/api/expense-documents/{document['id']}", headers=headers, json=invalid)
        self.assertEqual(response.status_code, 400, response.get_json())
        invalid["verification_status"] = "draft"
        response = self.server_client.put(f"/api/expense-documents/{document['id']}", headers=headers, json=invalid)
        self.assertEqual(response.status_code, 200, response.get_json())
        legacy_company = self.server_client.post("/api/expense-documents", headers=headers, json={
            "client_id": str(uuid.uuid4()), "supplier_name": "Legacy company", "document_date": "2026-10-15",
            "currency_code": "SEK", "gross_amount": "125.00", "vat_amount": "25.00", "verification_status": "confirmed",
            "allocations": [{"object_id": None, "gross_amount": "125.00", "vat_amount": "25.00"}],
        })
        self.assertEqual(legacy_company.status_code, 201, legacy_company.get_json())
        self.assertTrue(legacy_company.get_json()["document"]["allocations"][0]["company_expense"])
        old_client_update = {**payload, "allocation_basis": "gross"}
        old_client_response = self.server_client.put(f"/api/expense-documents/{document['id']}", headers=headers, json=old_client_update)
        self.assertEqual(old_client_response.status_code, 409, old_client_response.get_json())

    def test_desktop_invoice_review_blocks_order_and_tax_decision_confirmation(self):
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window._expense_objects = []
        base_fields = {
            "allocation_basis": "net",
            "supplier_name": "Receiver AB",
            "invoice_number": "DOC-1",
            "document_date": "2026-10-13",
            "currency_code": "SEK",
            "net_amount": "100.00",
            "vat_amount": "25.00",
            "rounding_amount": "0.00",
            "amount_due": "125.00",
            "order_proposals": [{
                "net_amount": "100.00", "vat_amount": "25.00",
                "destination": "company", "object_id": None,
                "expense_category": "Тест",
            }],
        }

        def inspect_dialog(dialog):
            confirm = dialog.findChild(self.desktop.QPushButton, "invoiceConfirm")
            self.assertFalse(confirm.isEnabled())
            dialog.reject()
            return dialog.result()

        try:
            for document_type, text in (
                ("order_confirmation", "ORDERBEKRÄFTELSE"),
                ("tax_decision", "Beslut om trängselskatt"),
            ):
                with self.subTest(document_type=document_type):
                    fields = {
                        **base_fields, "document_type": document_type,
                        "non_invoice_document_type": document_type, "raw_text": text,
                    }
                    with patch.object(self.desktop.QDialog, "exec", new=inspect_dialog):
                        self.assertFalse(window._review_net_invoice({}, [], fields))
        finally:
            window.close()
            window.deleteLater()

    def test_invoice_vertical_totals_do_not_steal_next_row_amount(self):
        from expense_documents import parse_invoice_pages, net_invoice_issues

        elements = []
        for index, (caption, value) in enumerate((
            ("Totalt exkl. moms", "1 795,00 SEK"), ("Momsbelopp", "448,75 SEK"),
            ("Totalt inkl. moms", "2 244,00 SEK"), ("Öresavrundning", "0,25 SEK"),
            ("Att betala", "2 244,00 SEK"),
        )):
            elements.extend([
                {"text": caption, "x0": 300, "x1": 420, "y0": 100 + 25 * index, "y1": 110 + 25 * index},
                {"text": value, "x0": 520, "x1": 590, "y0": 100 + 25 * index, "y1": 110 + 25 * index},
            ])
        fields = parse_invoice_pages([{"page": 2, "elements": elements}])
        self.assertEqual(fields["net_amount"], "1795.00")
        self.assertEqual(fields["vat_amount"], "448.75")
        self.assertEqual(fields["rounding_amount"], "0.25")
        self.assertEqual(fields["amount_due"], "2244.00")
        self.assertTrue(net_invoice_issues({**fields, "rounding_amount": "2244.00"},
                                          [{"destination": "company", "net_amount": "1795.00"}]))

    def test_beijer_explicit_rounding_reconciles_document_gross_and_amount_due(self):
        from expense_documents import parse_invoice_pages, net_invoice_issues

        elements = []
        for index, (caption, value) in enumerate((
            ("Netto", "6 490,50 SEK"), ("Moms 25,00%", "1 622,63 SEK"),
            ("Totalt inkl. moms", "8 113,13 SEK"), ("Öresavr.", "-0,13 SEK"),
            ("Att betala", "8 113,00 SEK"),
        )):
            elements.extend([
                {"text": caption, "x0": 300, "x1": 420, "y0": 100 + 25 * index, "y1": 110 + 25 * index},
                {"text": value, "x0": 520, "x1": 590, "y0": 100 + 25 * index, "y1": 110 + 25 * index},
            ])

        fields = parse_invoice_pages([{"page": 2, "elements": elements}])
        self.assertEqual(fields["gross_amount"], "8113.13")
        self.assertEqual(fields["rounding_amount"], "-0.13")
        self.assertEqual(fields["amount_due"], "8113.00")
        self.assertEqual(fields["field_sources"]["rounding_amount"][0]["caption"], "Öresavr.")
        self.assertEqual(net_invoice_issues(fields, [{
            "destination": "company", "net_amount": "6490.50", "vat_amount": "1622.63",
        }]), [])
        self.assertTrue(net_invoice_issues(
            {**fields, "rounding_amount": "0.00"},
            [{"destination": "company", "net_amount": "6490.50", "vat_amount": "1622.63"}],
        ))

    def test_invoice_explicit_tax_rate_and_buyer_block_never_use_item_price(self):
        from expense_documents import parse_invoice_pages

        def cell(text, left, top, right):
            return {"text": text, "x0": left, "x1": right, "y0": top, "y1": top + 8}

        elements = [cell("Fakturamottagare:", 20, 30, 120), cell("Buyer AB", 20, 45, 110),
                    cell("Street 1", 20, 60, 100), cell("123 45 City", 20, 75, 110),
                    cell("SELLER & Co KB Servicecenter", 320, 45, 560), cell("Box 12", 320, 60, 370),
                    cell("123 45 City", 320, 75, 400), cell("Fakturanummer", 320, 100, 430), cell("54", 490, 100, 510),
                    cell("Omvänd", 400, 210, 460), cell("moms", 400, 222, 450), cell("1800.00", 490, 235, 560),
                    cell("Exkl. moms", 40, 400, 130), cell("44550.00", 220, 400, 280),
                    cell("Bankkontonr", 350, 397, 430), cell("001328722710", 490, 397, 580),
                    cell("Moms (0 %)", 40, 420, 140), cell("0.00", 250, 420, 280),
                    cell("Att betala", 320, 445, 430), cell("44550.00", 490, 445, 560)]
        fields = parse_invoice_pages([{"elements": elements}])
        self.assertEqual(fields["supplier_name"], "SELLER & Co KB Servicecenter")
        self.assertEqual(fields["invoice_number"], "54")
        self.assertEqual(fields["vat_amount"], "0.00")
        self.assertEqual(fields["net_amount"], "44550.00")

    def test_api_blocks_unsupported_payment_and_unreviewed_reverse_charge(self):
        headers = {"X-Sync-Key": "isolated-sync-test-key"}
        payload = {"client_id": str(uuid.uuid4()), "supplier_name": "Isolated seller", "document_date": "2026-10-17",
                   "currency_code": "SEK", "gross_amount": "125.00", "vat_amount": "25.00",
                   "verification_status": "confirmed", "recognized_text": "Förskottsbetald; Kvar att betala 0.00"}
        response = self.server_client.post("/api/expense-documents", headers=headers, json=payload)
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIn("Предоплаченный", response.get_json()["error"])
        payload.update({"recognized_text": "Omvänd betalningsskyldighet", "gross_amount": "100.00", "vat_amount": "0.00"})
        response = self.server_client.post("/api/expense-documents", headers=headers, json=payload)
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIn("сверки", response.get_json()["error"])
        connection = self.server.get_db_connection()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM expense_documents").fetchone()[0], 0)
        connection.close()
        payload["tax_reviewed"] = True
        response = self.server_client.post("/api/expense-documents", headers=headers, json=payload)
        self.assertEqual(response.status_code, 201, response.get_json())

    def test_reverse_charge_and_prepaid_confirmation_are_explicit(self):
        from expense_documents import net_invoice_issues, parse_invoice_pages

        allocations = [{"destination": "company", "net_amount": "100.00"}]
        reverse = {"net_amount": "100.00", "vat_amount": "0.00", "amount_due": "100.00",
                   "recognized_text": "Omvänd betalningsskyldighet"}
        self.assertTrue(any("Обратное начисление" in issue for issue in net_invoice_issues(reverse, allocations)))
        self.assertEqual(net_invoice_issues({**reverse, "tax_reviewed": True}, allocations), [])
        self.assertTrue(net_invoice_issues({**reverse, "tax_reviewed": True, "vat_amount": None}, allocations))
        prepaid = {"net_amount": "100.00", "vat_amount": "25.00", "amount_due": "0.00",
                   "recognized_text": "Förskottsbetald. Kvar att betala 0.00"}
        issues = net_invoice_issues(prepaid, allocations)
        self.assertTrue(any("пока не поддерживается" in issue for issue in issues))
        self.assertFalse(any("не равна" in issue for issue in issues))
        parsed = parse_invoice_pages([{"text": "Totalt exkl moms: 100.00\nMoms 25%: 25.00\nTotalt inkl moms: 125.00\nKvar att betala: 0.00\nFörskottsbetald"}])
        self.assertEqual(parsed["amount_due"], "0.00")
        self.assertEqual(parsed["document_total"], "125.00")
        self.assertTrue(parsed["prepaid"])

    def test_invoice_split_caption_money_and_adjacent_total_headers(self):
        from expense_documents import parse_invoice_pages

        def cell(text, left, top, right, bottom):
            return {"text": text, "x0": left, "x1": right, "y0": top, "y1": bottom}

        fields = parse_invoice_pages([{"elements": [
            cell("Bankkontonr", 366, 709, 423, 715), cell("832798446960489", 472, 709, 547, 715),
            cell("Exkl.", 53.3, 711.9, 73.6, 718.1), cell("moms", 77.7, 713.5, 103.9, 718.2),
            cell("42", 198.5, 712.1, 207.9, 718.1), cell("750,00", 211.8, 712.1, 239, 719.5),
            cell("Moms (0 %)", 53.3, 722, 108, 729.4), cell("0,00", 222, 722, 239, 729.4),
            cell("Att betala", 365, 735, 420, 742), cell("42 750,00", 490, 735, 547, 744),
        ]}])
        self.assertEqual(fields["net_amount"], "42750.00")
        self.assertEqual(fields["vat_amount"], "0.00")
        self.assertEqual(fields["amount_due"], "42750.00")
        fields = parse_invoice_pages([{"elements": [
            cell("Avrundningsdifferens", 350, 100, 450, 108),
            cell("Fakturabelopp:", 458, 100, 550, 108),
            cell("0,00 SEK", 350, 120, 405, 128), cell("125,13 SEK", 490, 120, 550, 128),
        ]}])
        self.assertEqual(fields["rounding_amount"], "0.00")
        self.assertEqual(fields["amount_due"], "125.13")

    def test_net_invoice_unknown_currency_stays_empty_in_form_and_unknown_in_draft(self):
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window._expense_objects = []

        def save_draft(dialog):
            currency = dialog.findChild(self.desktop.QLineEdit, "invoice_currency_code")
            self.assertEqual(currency.text(), "")
            table = dialog.findChild(self.desktop.QTableWidget, "invoiceAllocations")
            table.cellWidget(0, 2).setCurrentIndex(table.cellWidget(0, 2).findData("company"))
            self.assertFalse(dialog.findChild(self.desktop.QPushButton, "invoiceConfirm").isEnabled())
            dialog.findChild(self.desktop.QPushButton, "invoiceSaveDraft").click()
            return dialog.result()

        try:
            with patch.object(self.desktop.QDialog, "exec", new=save_draft), \
                 patch.object(window, "load_expense_documents"), patch.object(window, "background_sync_with_cloud"):
                window._review_net_invoice({}, [], {"supplier_name": "Test", "invoice_number": "54", "document_date": "2026-10-17",
                    "allocation_basis": "net", "currency_code": "UNKNOWN", "net_amount": "100.00", "vat_amount": "0.00", "amount_due": "100.00"})
            connection = self.desktop.get_db_connection()
            payload = json.loads(connection.execute("SELECT payload_json FROM expense_document_outbox").fetchone()[0])
            connection.close()
            self.assertEqual(payload["currency_code"], "UNKNOWN")
        finally:
            window.close()
            window.deleteLater()

    def test_due_date_explicit_calculated_and_ambiguous_sources(self):
        from expense_documents import parse_invoice_pages, due_date_issues

        explicit = parse_invoice_pages([{"page": 1, "text":
            "Fakturadatum: 2026-10-15\nFörfallodag: 2026-11-14\nBetalningsvillkor 10 dagar"}])
        self.assertEqual(explicit["due_date"], "2026-11-14")
        self.assertEqual(explicit["due_date_kind"], "explicit")
        self.assertEqual(due_date_issues(explicit), [])
        calculated = parse_invoice_pages([{"text": "Fakturadatum: 2026-10-15\nBetalningsvillkor: 30 dagar netto"}])
        self.assertEqual(calculated["due_date"], "2026-11-14")
        self.assertEqual(calculated["due_date_kind"], "calculated")
        self.assertTrue(due_date_issues(calculated))
        self.assertEqual(due_date_issues({**calculated, "due_date_reviewed": True}), [])
        ambiguous = parse_invoice_pages([{"text": "Förfallodatum: 2026-11-14\nOss tillhanda senast: 2026-11-15"}])
        self.assertIsNone(ambiguous["due_date"])
        self.assertEqual(ambiguous["due_date_kind"], "ambiguous")
        self.assertTrue(due_date_issues({**ambiguous, "due_date_reviewed": True}))
        conflict_terms = parse_invoice_pages([{"text": "Fakturadatum: 2026-10-15\nBetalningsvillkor 30 dagar\nBetalningsvillkor 10 dagar"}])
        self.assertIsNone(conflict_terms["due_date"])
        self.assertEqual(conflict_terms["due_date_kind"], "ambiguous")

    def test_due_date_api_roundtrip_manual_correction_and_legacy_update_preserves_date(self):
        headers = {"X-Sync-Key": "isolated-sync-test-key"}
        payload = {"client_id": str(uuid.uuid4()), "supplier_name": "Date test", "document_date": "2026-10-15",
                   "currency_code": "SEK", "gross_amount": "125.00", "vat_amount": "25.00", "verification_status": "draft",
                   "due_date": "2026-11-14", "due_date_kind": "explicit", "due_date_evidence": [{"page": 2, "text": "2026-11-14"}]}
        response = self.server_client.post("/api/expense-documents", headers=headers, json=payload)
        self.assertEqual(response.status_code, 201, response.get_json())
        document = response.get_json()["document"]
        self.assertEqual(document["due_date"], "2026-11-14")
        self.assertEqual(document["due_date_kind"], "explicit")
        payload.update({"due_date": "2026-11-16", "due_date_kind": "manual", "due_date_reviewed": True,
                        "expected_version": document["sync_version"], "verification_status": "confirmed"})
        response = self.server_client.put(f"/api/expense-documents/{document['id']}", headers=headers, json=payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        corrected = response.get_json()["document"]
        self.assertEqual(corrected["due_date"], "2026-11-16")
        self.assertTrue(corrected["due_date_reviewed"])
        legacy = {key: value for key, value in payload.items() if not key.startswith("due_date")}
        legacy["expected_version"] = corrected["sync_version"]
        response = self.server_client.put(f"/api/expense-documents/{document['id']}", headers=headers, json=legacy)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["document"]["due_date"], "2026-11-16")
        calculated = {**payload, "client_id": str(uuid.uuid4()), "due_date_kind": "calculated", "due_date_reviewed": False}
        response = self.server_client.post("/api/expense-documents", headers=headers, json=calculated)
        self.assertEqual(response.status_code, 400)
        calculated["verification_status"] = "draft"
        response = self.server_client.post("/api/expense-documents", headers=headers, json=calculated)
        self.assertEqual(response.status_code, 201, response.get_json())

    def test_invoice_distribution_destination_counter_and_draft_roundtrip(self):
        connection = self.server.get_db_connection()
        object_id = int(connection.execute("SELECT id FROM meta_objects WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()[0])
        connection.close()

        for destination in ("object", "company"):
            with self.subTest(destination=destination):
                identifier = str(uuid.uuid4())
                window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
                self.desktop.QMainWindow.__init__(window)
                window._expense_objects = [{"id": object_id, "name": "Isolated catalog object", "markning": "TEST-1"}]
                fields = {"client_id": identifier, "allocation_basis": "net", "supplier_name": "Isolated supplier",
                          "invoice_number": "UI-" + destination, "document_date": "2026-10-17", "currency_code": "SEK",
                          "net_amount": "100.00", "vat_amount": "25.00", "rounding_amount": "0.00", "amount_due": "125.00"}

                def choose_and_save(dialog):
                    table = dialog.findChild(self.desktop.QTableWidget, "invoiceAllocations")
                    counter = dialog.findChild(self.desktop.QLabel, "invoiceDistribution")
                    confirm = dialog.findChild(self.desktop.QPushButton, "invoiceConfirm")
                    self.assertEqual(table.cellWidget(0, 2).currentData(), "unselected")
                    self.assertEqual(counter.text(), "Без НДС: распределено 0.00 / осталось 100.00")
                    self.assertFalse(confirm.isEnabled())
                    confirm.click()
                    connection = self.desktop.get_db_connection()
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM expense_document_outbox WHERE client_id=?", (identifier,)).fetchone()[0], 0)
                    connection.close()
                    choice = table.cellWidget(0, 2)
                    choice.setCurrentIndex(choice.findData(destination))
                    if destination == "object":
                        self.assertIsNone(table.cellWidget(0, 3).currentData())
                        self.assertFalse(confirm.isEnabled())
                        self.assertEqual(counter.text(), "Без НДС: распределено 0.00 / осталось 100.00")
                        objects = table.cellWidget(0, 3)
                        objects.setCurrentIndex(objects.findData(object_id))
                    else:
                        self.assertFalse(table.cellWidget(0, 3).isEnabled())
                        table.cellWidget(0, 4).setText("Isolated company category")
                    self.assertEqual(counter.text(), "Без НДС: распределено 100.00 / осталось 0.00")
                    self.assertTrue(confirm.isEnabled())
                    dialog.findChild(self.desktop.QPushButton, "invoiceSaveDraft").click()
                    return dialog.result()

                def verify_reopened(dialog):
                    table = dialog.findChild(self.desktop.QTableWidget, "invoiceAllocations")
                    self.assertEqual(table.rowCount(), 1)
                    self.assertEqual(table.cellWidget(0, 2).currentData(), destination)
                    self.assertEqual(table.item(0, 1).text(), "100.00")
                    self.assertEqual(table.cellWidget(0, 3).currentData(), object_id if destination == "object" else None)
                    if destination == "company":
                        self.assertEqual(table.cellWidget(0, 4).text(), "Isolated company category")
                    for key, expected in (("net_amount", "100.00"), ("vat_amount", "25.00"),
                                          ("rounding_amount", "0.00"), ("amount_due", "125.00")):
                        self.assertEqual(dialog.findChild(self.desktop.QLineEdit, "invoice_" + key).text(), expected)
                    self.assertEqual(dialog.findChild(self.desktop.QLabel, "invoiceDistribution").text(),
                                     "Без НДС: распределено 100.00 / осталось 0.00")
                    self.assertTrue(dialog.findChild(self.desktop.QPushButton, "invoiceConfirm").isEnabled())
                    dialog.reject()
                    return dialog.result()

                try:
                    with patch.object(window, "load_expense_documents"), patch.object(window, "background_sync_with_cloud"), \
                         patch.object(self.desktop.QDialog, "exec", new=choose_and_save):
                        self.assertTrue(window._review_net_invoice({}, [], fields))
                    connection = self.desktop.get_db_connection()
                    queued = connection.execute("SELECT payload_json,revision FROM expense_document_outbox WHERE client_id=?", (identifier,)).fetchone()
                    connection.close()
                    payload = json.loads(queued["payload_json"])
                    self.assertEqual(payload["verification_status"], "draft")
                    self.assertEqual(payload["allocations"][0]["destination"], destination)
                    self.assertEqual(payload["allocations"][0]["object_id"], object_id if destination == "object" else None)
                    self.assertEqual(payload["allocations"][0]["net_amount"], "100.00")
                    with patch.object(window, "load_expense_documents"), patch.object(window, "background_sync_with_cloud"), \
                         patch.object(self.desktop.QDialog, "exec", new=verify_reopened):
                        self.assertFalse(window._review_net_invoice(payload, [], {}))
                    connection = self.desktop.get_db_connection()
                    saved = connection.execute("SELECT payload_json,revision FROM expense_document_outbox WHERE client_id=?", (identifier,)).fetchall()
                    connection.close()
                    self.assertEqual(len(saved), 1)
                    self.assertEqual(saved[0]["payload_json"], queued["payload_json"])
                    self.assertEqual(saved[0]["revision"], queued["revision"])
                finally:
                    window.close()
                    window.deleteLater()

    def test_due_date_qt_draft_manual_edit_reopen_and_confirm(self):
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window._expense_objects = []
        fields = {"allocation_basis": "net", "supplier_name": "Date test", "invoice_number": "D54", "document_date": "2026-10-15",
                  "currency_code": "SEK", "net_amount": "100.00", "vat_amount": "25.00", "amount_due": "125.00",
                  "due_date": "2026-11-14", "due_date_kind": "calculated", "due_date_review_required": True,
                  "due_date_evidence": [{"kind": "payment_terms", "days": 30}]}

        def first(dialog):
            date_input = dialog.findChild(self.desktop.QLineEdit, "invoice_due_date")
            self.assertEqual(date_input.text(), "2026-11-14")
            self.assertIn("Рассчитан", dialog.findChild(self.desktop.QLabel, "invoiceDueDateOrigin").text())
            self.assertIn("распределено 0.00 / осталось 100.00", dialog.findChild(self.desktop.QLabel, "invoiceDistribution").text())
            table = dialog.findChild(self.desktop.QTableWidget, "invoiceAllocations")
            table.cellWidget(0, 2).setCurrentIndex(table.cellWidget(0, 2).findData("company"))
            self.assertIn("распределено 100.00 / осталось 0.00", dialog.findChild(self.desktop.QLabel, "invoiceDistribution").text())
            confirm = dialog.findChild(self.desktop.QPushButton, "invoiceConfirm")
            self.assertFalse(confirm.isEnabled())
            dialog.findChild(self.desktop.QCheckBox, "invoiceDueDateReviewed").setChecked(True)
            self.assertTrue(confirm.isEnabled())
            date_input.setText("2026-11-16")
            self.assertIn("вручную", dialog.findChild(self.desktop.QLabel, "invoiceDueDateOrigin").text())
            dialog.findChild(self.desktop.QPushButton, "invoiceSaveDraft").click()
            return dialog.result()

        def reopened(dialog):
            self.assertEqual(dialog.findChild(self.desktop.QLineEdit, "invoice_due_date").text(), "2026-11-16")
            self.assertIn("вручную", dialog.findChild(self.desktop.QLabel, "invoiceDueDateOrigin").text())
            button = dialog.findChild(self.desktop.QPushButton, "invoiceConfirm")
            self.assertTrue(button.isEnabled())
            button.click()
            return dialog.result()

        try:
            with patch.object(window, "load_expense_documents"), patch.object(window, "background_sync_with_cloud"), \
                 patch.object(self.desktop.QDialog, "exec", new=first):
                self.assertTrue(window._review_net_invoice({}, [], fields))
            connection = self.desktop.get_db_connection()
            payload = json.loads(connection.execute("SELECT payload_json FROM expense_document_outbox").fetchone()[0])
            connection.close()
            self.assertEqual(payload["verification_status"], "draft")
            self.assertEqual(payload["due_date_kind"], "manual")
            with patch.object(window, "load_expense_documents"), patch.object(window, "background_sync_with_cloud"), \
                 patch.object(self.desktop.QDialog, "exec", new=reopened):
                self.assertTrue(window._review_net_invoice(payload, [], {}))
            connection = self.desktop.get_db_connection()
            rows = connection.execute("SELECT payload_json FROM expense_document_outbox").fetchall()
            connection.close()
            self.assertEqual(len(rows), 1)
            confirmed = json.loads(rows[0][0])
            self.assertEqual(confirmed["due_date"], "2026-11-16")
            self.assertEqual(confirmed["verification_status"], "confirmed")
        finally:
            window.close()
            window.deleteLater()

    def test_due_date_schema_is_added_additively_to_existing_database(self):
        import sqlite3
        legacy = Path(self._case.name) / "legacy-no-due.db"
        connection = sqlite3.connect(legacy)
        try:
            connection.execute("""CREATE TABLE expense_documents (
                id INTEGER PRIMARY KEY, client_id TEXT UNIQUE, supplier_name TEXT DEFAULT '', invoice_number TEXT DEFAULT '',
                document_date TEXT, currency_code TEXT DEFAULT 'SEK', net_amount TEXT, vat_amount TEXT, gross_amount TEXT,
                expense_category TEXT DEFAULT '', document_comment TEXT DEFAULT '', order_proposals_json TEXT DEFAULT '[]',
                verification_status TEXT DEFAULT 'draft', recognized_text TEXT DEFAULT '', original_name TEXT, stored_name TEXT,
                file_sha256 TEXT, is_deleted INTEGER DEFAULT 0, deleted_at TEXT, sync_version INTEGER DEFAULT 1,
                created_at TEXT, updated_at TEXT)""")
            connection.execute("INSERT INTO expense_documents (id, client_id, supplier_name, gross_amount, created_at, updated_at) VALUES (7, 'old', 'Old AB', '125.00', 'x', 'x')")
            connection.commit()
        finally:
            connection.close()
        with patch.object(self.server, "DB_FILE", str(legacy)):
            self.server.init_cloud_db()
            connection = self.server.get_db_connection()
            try:
                self.assertTrue(self.server.expense_due_date_schema_ready(connection))
                row = connection.execute("SELECT supplier_name, gross_amount, due_date, due_date_kind FROM expense_documents WHERE id=7").fetchone()
                self.assertEqual(tuple(row), ("Old AB", "125.00", None, "absent"))
            finally:
                connection.close()
    def test_due_date_ack_must_echo_saved_date_and_origin(self):
        identifier = str(uuid.uuid4())
        payload = {"client_id": identifier, "supplier_name": "Date ACK", "currency_code": "SEK",
                   "gross_amount": "125.00", "verification_status": "draft", "allocations": [],
                   "due_date": "2026-11-14", "due_date_kind": "manual"}
        harness = SimpleNamespace()
        self.desktop.SmartReportApp.queue_expense_document_operation(harness, identifier, "upsert", payload)
        correct = {"id": 9, "client_id": identifier, "sync_version": 1, "due_date": "2026-11-14", "due_date_kind": "manual"}
        capabilities = {"ok": True, "api_version": 2, "features": ["client_id", "soft_delete", "attachments",
                         "nullable_company_allocations", "versioned_updates", "due_date_v1"]}
        for document in ({**correct, "due_date": None}, {**correct, "due_date_kind": "explicit"}, correct):
            def request(method, _url, **_kwargs):
                return SimpleNamespace(status_code=200, json=lambda: capabilities if method == "GET" else {"ok": True, "document": document})
            with patch.object(self.desktop.requests, "request", side_effect=request):
                result = self.desktop.SmartReportApp._sync_expense_document_outbox(harness)
            self.assertEqual(result["synced"], int(document == correct))
        connection = self.desktop.get_db_connection()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM expense_document_outbox").fetchone()[0], 0)
        connection.close()

    def test_invoice_unstated_rounding_is_not_invented(self):
        from expense_documents import parse_invoice_pages, net_invoice_issues

        fields = parse_invoice_pages([{"page": 1, "text":
            "Supplier: SELLER AB\nSumma exkl. moms: 100.00\nMomsbelopp: 25.00\nAtt betala: 125.00"}])
        self.assertIsNone(fields["rounding_amount"])
        allocations = [{"destination": "company", "net_amount": "100.00"}]
        self.assertEqual(net_invoice_issues(fields, allocations), [])
        self.assertTrue(net_invoice_issues({**fields, "amount_due": "125.25"}, allocations))
        self.assertIsNone(fields["rounding_amount"])

    def test_invoice_supplier_roles_and_rotated_coordinates(self):
        from expense_documents import parse_invoice_pages, pdf_display_box

        def cell(text, left, top, right=220):
            return {"text": text, "x0": left, "x1": right, "y0": top, "y1": top + 9}

        buyer = cell("BUYER RENOVATION AB", 330, 130, 550)
        payment_service = cell("PAYMENT SERVICE AB", 330, 740, 550)
        fields = parse_invoice_pages([{"elements": [buyer, payment_service,
            cell("Bank: BANK Swift: ABC Adress: PROCESSOR AB, Box 1, 123 45 City", 230, 700, 550),
            cell("Customer street 2", 330, 145, 550), cell("123 45 Buyer City", 330, 160, 550),
            cell("Org.nr: 123456-7890", 40, 80),
            cell("ISSUER AB, Box 12, 123 45 City", 40, 50), cell("Vår referens", 40, 150)]}])
        self.assertEqual(fields["supplier_name"], "ISSUER AB")
        fields = parse_invoice_pages([{"elements": [buyer, payment_service,
            cell("Adress", 40, 700), cell("Independent Seller", 40, 715),
            cell("Street 10", 40, 730), cell("123 45 City", 40, 745)]}])
        self.assertEqual(fields["supplier_name"], "Independent Seller")
        fields = parse_invoice_pages([{"elements": [buyer, payment_service]}])
        self.assertIsNone(fields["supplier_name"])
        bounds = (10, 20, 30, 40)
        crop = (0, 0, 100, 200)
        self.assertEqual(list(pdf_display_box(bounds, crop, 90).values()), [20, 10, 40, 30])
        self.assertEqual(list(pdf_display_box(bounds, crop, 180).values()), [70, 20, 90, 40])
        self.assertEqual(list(pdf_display_box(bounds, crop, 270).values()), [160, 70, 180, 90])

    def test_invoice_tax_column_uses_total_and_single_order_marker(self):
        from expense_documents import parse_invoice_pages

        def cell(text, left, top, right):
            return {"text": text, "x0": left, "x1": right, "y0": top, "y1": top + 8}

        fields = parse_invoice_pages([{"page": 1, "elements": [
            cell("Moms belopp SEK", 300, 100, 370), cell("Moms %", 400, 100, 440),
            cell("10,00", 310, 120, 350), cell("Totalt", 40, 140, 80), cell("25,00", 310, 140, 350),
            cell("Totalt fakturerat belopp exkl moms", 40, 200, 250), cell("SEK 100,00", 400, 200, 490),
            cell("Totalt fakturerat belopp inkl moms", 40, 225, 250), cell("SEK 125,00", 400, 225, 490),
            cell("Order O9876", 40, 260, 140), cell("Märkning P422", 40, 280, 140),
        ]}])
        self.assertEqual(fields["vat_amount"], "25.00")
        self.assertEqual(fields["currency_code"], "SEK")
        self.assertEqual(fields["amount_due"], "125.00")
        self.assertEqual(len(fields["order_proposals"]), 1)
        self.assertEqual(fields["order_proposals"][0]["project_code"], "P422")
        self.assertEqual(fields["order_proposals"][0]["order_id"], "O9876")
        self.assertEqual(fields["order_proposals"][0]["net_amount"], "100.00")

    def test_invoice_original_format_separated_cells_and_repeated_order(self):
        from expense_documents import parse_invoice_pages

        def cell(text, left, top, right, bottom):
            return {"text": text, "x0": left, "y0": top, "x1": right, "y1": bottom}

        header = [
            cell("Fakturanummer", 369.17, 40.4, 436.63, 46.94),
            cell("Fakturans valuta", 454.21, 40.4, 524.76, 46.94),
            cell("260406399329", 368.76, 51.17, 428.16, 57.75),
            cell("SEK", 453.95, 51.09, 471.53, 57.74),
            cell("Returadress: BEIJER BYGGMATERIAL AB", 48.82, 66.31, 202.04, 72.13),
            cell("Fakturadatum", 369.17, 67.67, 427.43, 74.21),
            cell("2026-10-15", 368.76, 78.44, 414.17, 85.01),
        ]
        first_order = [
            cell("Sälj.ordnr.:", 65.56, 401.12, 103.17, 408.61),
            cell("8776341-01", 122.22, 401.19, 163.12, 407.04),
            cell("Er ref.:", 337.95, 412.21, 360.62, 418.03),
            cell("ROMAN BASHCHUK", 394.64, 412.11, 470.44, 418.03),
            cell("Proj.nr.:", 337.94, 434.21, 364.62, 441.61),
            cell("P1035", 394.63, 434.19, 416.82, 440.04),
            cell("Summa order", 43.51, 634.62, 94.89, 640.54),
            cell("2 340,99", 538.12, 634.69, 568.69, 641.71),
        ]
        repeated_order = [{**item, "y0": item["y0"] - 300, "y1": item["y1"] - 300} for item in first_order]
        second_order = [
            cell("Sälj.ordnr.:", 65.56, 350, 103.17, 357),
            cell("8776342-01", 122.22, 350, 163.12, 357),
            cell("Proj.nr.:", 337.94, 380, 364.62, 387),
            cell("ROMAN BASHCHUK", 394.64, 380, 470.44, 387),
            cell("Artikel Verktyg", 100, 410, 200, 417),
            cell("280,96", 381, 410, 411, 417),
            cell("25,00", 432, 410, 462, 417),
            cell("Summa order", 43.51, 450, 94.89, 457),
            cell("708,96", 544.93, 450, 568.78, 457),
        ]
        totals = [
            cell("Summa rader", 42.88, 730.86, 106.56, 738.26),
            cell("Exkl. moms", 156.63, 730.98, 211, 738.25),
            cell("Moms", 270, 730.98, 297.7, 738.25),
            cell("Öresavr.", 354.76, 729.39, 394.22, 738.25),
            cell("Fakturatotal", 513.27, 730.98, 569.07, 738.25),
            cell("3 049,95", 42.94, 742.95, 81.04, 751.55),
            cell("3 049,95", 156.32, 742.95, 194.42, 751.55),
            cell("762,49", 269.76, 742.95, 299.43, 751.55),
            cell("-0,44", 354.65, 742.95, 376.64, 751.55),
            cell("SEK", 439.73, 742.86, 459.91, 750.26),
            cell("3 812,00", 531.26, 742.95, 569.28, 751.55),
        ]
        pages = [{"page": 1, "source": "layout.pdf", "elements": header + first_order},
                 {"page": 2, "source": "layout.pdf", "elements": header + repeated_order + second_order + totals}]
        fields = parse_invoice_pages(pages)
        expected = {"supplier_name": "BEIJER BYGGMATERIAL AB", "invoice_number": "260406399329",
                    "document_date": "2026-10-15", "currency_code": "SEK", "net_amount": "3049.95",
                    "vat_amount": "762.49", "rounding_amount": "-0.44", "amount_due": "3812.00"}
        for key, value in expected.items():
            self.assertEqual(fields[key], value, key)
        orders = fields["order_proposals"]
        self.assertEqual(len(orders), 2)
        self.assertEqual([row["net_amount"] for row in orders], ["2340.99", "708.96"])
        self.assertEqual([source["page"] for source in orders[0]["sources"]], [1, 2])
        self.assertEqual(orders[0]["project_code"], "P1035")
        self.assertIsNone(orders[1]["project_code"])
        self.assertEqual(orders[1]["original_marker"], "ROMAN BASHCHUK")
        self.assertTrue(all(source.get("box") for row in orders for source in row["sources"]))

        replacements = {"260406399329": "INV-987654", "2026-10-15": "2026-11-01",
                        "Returadress: BEIJER BYGGMATERIAL AB": "Returadress: OTHER MATERIAL AB",
                        "2 340,99": "20,00", "708,96": "30,00", "3 049,95": "50,00",
                        "762,49": "12,50", "-0,44": "-0,50", "3 812,00": "62,00"}
        changed = [{**page, "elements": [{**item, "text": replacements.get(item["text"], item["text"])}
                                         for item in page["elements"]]} for page in pages]
        fields = parse_invoice_pages(changed)
        self.assertEqual(fields["supplier_name"], "OTHER MATERIAL AB")
        self.assertEqual(fields["invoice_number"], "INV-987654")
        self.assertEqual(fields["net_amount"], "50.00")
        self.assertEqual(fields["vat_amount"], "12.50")
        self.assertEqual(fields["amount_due"], "62.00")
        self.assertEqual([row["net_amount"] for row in fields["order_proposals"]], ["20.00", "30.00"])

    def test_invoice_synthetic_beijer_layout_last_page_totals(self):
        from reportlab.pdfgen import canvas
        from expense_documents import extract_local_document, parse_invoice_pages

        path = Path(self._case.name) / "synthetic-beijer-layout.pdf"
        pdf = canvas.Canvas(str(path))
        pdf.setFont("Helvetica", 10)
        pdf.drawString(40, 790, "Returadress: BEIJER BYGGMATERIAL AB")
        for x, label, value in ((40, "Fakturanummer", "260406399329"),
                                (210, "Fakturadatum", "15.10.2026"),
                                (370, "Fakturans valuta", "SEK")):
            pdf.drawString(x, 740, label)
            pdf.drawString(x, 722, value)
        for y, text in ((650, "Proj.nr.: P1035"), (630, "Order nr: A-1"),
                        (610, "Artikel 1 25 % 280,96"), (590, "Summa order: 2 340,99")):
            pdf.drawString(40, y, text)
        pdf.showPage()
        pdf.setFont("Helvetica", 10)
        for y, text in ((740, "Proj.nr.: ROMAN BASHCHUK"), (720, "Order nr: B-2"),
                        (700, "Artikel Verktyg 25 % 280,96"), (680, "Summa order: 708,96")):
            pdf.drawString(40, y, text)
        for x, label, value in ((40, "Summa exkl. moms", "3 049,95"),
                                (210, "Moms", "762,49"), (340, "Avrundning", "-0,44"),
                                (470, "Att betala", "3 812,00")):
            pdf.drawString(x, 100, label)
            pdf.drawString(x, 80, value)
        pdf.save()
        extraction = extract_local_document(path)
        self.assertEqual(len(extraction.page_elements), 2)
        fields = parse_invoice_pages([
            {"source": path.name, "page": index + 1, "text": text,
             "elements": extraction.page_elements[index]}
            for index, text in enumerate(extraction.pages)
        ])
        self.assertEqual(fields["supplier_name"], "BEIJER BYGGMATERIAL AB")
        self.assertEqual(fields["invoice_number"], "260406399329")
        self.assertEqual(fields["document_date"], "2026-10-15")
        self.assertEqual(fields["currency_code"], "SEK")
        self.assertEqual(fields["net_amount"], "3049.95")
        self.assertEqual(fields["vat_amount"], "762.49")
        self.assertEqual(fields["rounding_amount"], "-0.44")
        self.assertEqual(fields["amount_due"], "3812.00")
        self.assertEqual(fields["gross_amount"], "3812.44")
        self.assertEqual(fields["field_sources"]["net_amount"][0]["page"], 2)
        self.assertEqual([item["net_amount"] for item in fields["order_proposals"]], ["2340.99", "708.96"])
        self.assertEqual(fields["order_proposals"][1]["original_marker"], "ROMAN BASHCHUK")
        self.assertIsNone(fields["order_proposals"][1]["project_code"])

    def test_expense_pdf_extraction_reads_every_page(self):
        from reportlab.pdfgen import canvas
        from expense_documents import extract_local_document

        for page_count in (1, 3, 4):
            pdf_path = Path(self._case.name) / f"ocr-pages-{page_count}.pdf"
            document = canvas.Canvas(str(pdf_path))
            expected_pages = []
            for page in range(page_count):
                text = f"Unique invoice page marker {page + 1} of {page_count}"
                expected_pages.append(text)
                document.drawString(40, 760, text)
                document.showPage()
            document.save()
            extracted = extract_local_document(pdf_path)
            for text in expected_pages:
                self.assertIn(text, extracted.text)

    def test_catalog_pull_links_exact_legacy_rows_and_preserves_history_and_allocations(self):
        server_objects = (
            ("***", "Tomteboda arbetsledning"),
            ("p1032", "Tomteboda doors"),
            ("p1031", "Tomteboda glass"),
            ("p1034", "Tomteboda ÄTA"),
            ("****", "Tomteboda ÄTA Hotel"),
            ("p1038", "Tomteboda ÄTA Rivning"),
        )
        server_conn = self.server.get_db_connection()
        for markning, name in server_objects:
            server_rate = 0 if markning in {"p1034", "p1038"} else 550
            server_conn.execute("""
                INSERT INTO meta_objects (
                    name, markning, company, rate, transport_rate, is_active, sync_version
                ) VALUES (?, ?, 'Renatur', ?, 0, 1, 0)
            """, (name, markning, server_rate))
        server_conn.commit()
        server_conn.close()

        desktop_conn = self.desktop.get_db_connection()
        desktop_conn.execute("DELETE FROM objects WHERE company = 'Renatur'")
        desktop_conn.execute("""
            INSERT INTO objects (
                name, markning, company, rate, transport_rate, is_active, sync_version
            ) VALUES ('Tomteboda ÄTA', 'p1034', 'Renatur', 520, 450, 1, 0),
                     ('Tomteboda ÄTA Rivning', 'p1038', 'Renatur', 425, 450, 1, 0)
        """)
        local_object_ids = {
            row["markning"]: int(row["id"])
            for row in desktop_conn.execute(
                "SELECT id, markning FROM objects WHERE company = 'Renatur'"
            )
        }
        document_id = desktop_conn.execute("""
            INSERT INTO supplier_documents (
                document_type, document_date, currency_code, gross_amount,
                expense_category, created_at, updated_at
            ) VALUES ('manual', '2026-10-12', 'SEK', '37500.00', 'Тестовая allocation', 'test', 'test')
        """).lastrowid
        desktop_conn.execute("""
            INSERT INTO supplier_document_allocations (document_id, object_id, allocated_gross)
            VALUES (?, ?, '37500.00')
        """, (document_id, local_object_ids["p1034"]))
        desktop_conn.executemany("""
            INSERT INTO shifts (date, employee, company, object_name, hours, rate, transport, comment, invoiced)
            VALUES (?, 'Test Employee', 'Renatur', ?, 8, 0, 0, ?, 0)
        """, (
            ("2026-10-01", "Tomteboda doors", "history-p1032"),
            ("2026-10-02", "Tomteboda glass", "history-p1031"),
        ))
        desktop_conn.commit()
        desktop_conn.close()

        result = self._worker()
        desktop_conn = self.desktop.get_db_connection()
        rows = desktop_conn.execute("""
            SELECT id, name, markning, sync_id, server_id, is_active
            FROM objects WHERE company = 'Renatur' ORDER BY markning
        """).fetchall()
        history = {
            tuple(row) for row in desktop_conn.execute(
                "SELECT object_name, comment FROM shifts WHERE comment LIKE 'history-%'"
            ).fetchall()
        }
        desktop_conn.close()
        self.assertEqual(len(rows), 6)
        self.assertEqual(result, {
            "conflicts": 0, "linked_legacy": 7, "unresolved_legacy": 0,
            "imported_employees": 0, "unresolved_employees": 0,
            "conflict_details": "", "unresolved_legacy_details": "",
        })
        self.assertTrue(all(row[3] and row[4] and row[5] == 1 for row in rows))
        self.assertEqual(
            {row[2]: row[0] for row in rows if row[2] in local_object_ids},
            local_object_ids,
        )
        self.assertEqual(history, {
            ("Tomteboda doors", "history-p1032"),
            ("Tomteboda glass", "history-p1031"),
        })
        server_conn = self.server.get_db_connection()
        server_rows = {
            row["markning"]: row
            for row in server_conn.execute(
                "SELECT id, markning, rate, transport_rate, sync_id, sync_version "
                "FROM meta_objects WHERE company = 'Renatur'"
            )
        }
        self.assertEqual((server_rows["p1034"]["rate"], server_rows["p1034"]["transport_rate"]), (520, 450))
        self.assertEqual((server_rows["p1038"]["rate"], server_rows["p1038"]["transport_rate"]), (425, 450))
        self.assertTrue(all(row["sync_id"] and row["sync_version"] == 1 for row in server_rows.values()))
        self.assertEqual(server_conn.execute("SELECT COUNT(*) FROM object_sync_events").fetchone()[0], 7)
        server_conn.close()
        desktop_conn = self.desktop.get_db_connection()
        allocation = desktop_conn.execute(
            "SELECT object_id FROM supplier_document_allocations WHERE document_id = ?", (document_id,)
        ).fetchone()
        self.assertEqual(allocation[0], local_object_ids["p1034"])
        self.assertEqual(desktop_conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(desktop_conn.execute("SELECT COUNT(*) FROM object_sync_outbox").fetchone()[0], 0)
        desktop_conn.close()
        combo = self.desktop.QComboBox()
        self.desktop.SmartReportApp.load_objects_for_company(
            SimpleNamespace(), "Renatur", combo, self.desktop.RECORD_TYPE_WORK
        )
        visible_codes = {
            combo.itemText(index).split("|", 1)[0].strip()
            for index in range(combo.count())
        }
        self.assertEqual(visible_codes, {"***", "****", "p1031", "p1032", "p1034", "p1038"})
        repeated = self._worker()
        self.assertEqual(repeated["linked_legacy"], 0)
        self.assertEqual(repeated["conflicts"], 0)
        desktop_conn = self.desktop.get_db_connection()
        self.assertEqual(
            desktop_conn.execute("SELECT COUNT(*) FROM objects WHERE company = 'Renatur'").fetchone()[0],
            6,
        )
        desktop_conn.close()

    def test_legacy_rate_conflict_is_not_linked_or_overwritten(self):
        server_conn = self.server.get_db_connection()
        server_id = server_conn.execute("""
            INSERT INTO meta_objects (name, markning, company, rate, transport_rate, is_active, sync_version)
            VALUES ('Conflicting Rate', 'p-rate-conflict', 'Renatur', 120, 10, 1, 0)
        """).lastrowid
        server_conn.commit()
        server_conn.close()
        desktop_conn = self.desktop.get_db_connection()
        local_id = desktop_conn.execute("""
            INSERT INTO objects (name, markning, company, rate, transport_rate, is_active, sync_version)
            VALUES ('Conflicting Rate', 'p-rate-conflict', 'Renatur', 100, 10, 1, 0)
        """).lastrowid
        desktop_conn.commit()
        desktop_conn.close()

        result = self._worker()
        self.assertEqual(result["conflicts"], 0)
        self.assertEqual(result["linked_legacy"], 1)
        self.assertEqual(result["unresolved_legacy"], 1)
        self.assertEqual(result["imported_employees"], 0)
        self.assertEqual(result["unresolved_employees"], 0)
        self.assertEqual(result["conflict_details"], "")
        self.assertTrue(result["unresolved_legacy_details"])
        desktop_conn = self.desktop.get_db_connection()
        local = desktop_conn.execute(
            "SELECT sync_id, server_id, rate, transport_rate FROM objects WHERE id = ?", (local_id,)
        ).fetchone()
        self.assertEqual(tuple(local), (None, None, 100, 10))
        self.assertEqual(desktop_conn.execute("SELECT COUNT(*) FROM object_sync_outbox").fetchone()[0], 0)
        desktop_conn.close()
        server_conn = self.server.get_db_connection()
        remote = server_conn.execute(
            "SELECT sync_id, rate, transport_rate, sync_version FROM meta_objects WHERE id = ?", (server_id,)
        ).fetchone()
        self.assertEqual(tuple(remote), (None, 120, 10, 0))
        server_conn.close()

    def test_server_employee_catalog_imports_missing_profile_but_respects_local_delete(self):
        server_conn = self.server.get_db_connection()
        server_conn.executemany(
            "INSERT INTO meta_employees (name, salary_rate, is_active) VALUES (?, ?, 1)",
            (("Server Only Employee", 450), ("Locally Deleted Employee", 320)),
        )
        server_conn.commit()
        server_conn.close()
        desktop_conn = self.desktop.get_db_connection()
        desktop_conn.execute("""
            INSERT INTO employee_sync_state (employee_key, employee_name, state, sync_version, updated_at)
            VALUES (?, ?, 'deleted', 1, 'test')
        """, (self.desktop.employee_sync_key("Locally Deleted Employee"), "Locally Deleted Employee"))
        desktop_conn.commit()
        desktop_conn.close()

        response = self.server_client.get(
            "/api/sync/employee-catalog", headers={"X-Sync-Key": "wrong-test-key"}
        )
        self.assertEqual(response.status_code, 403)
        result = self._worker()
        self.assertEqual(result["imported_employees"], 1)
        self.assertEqual(result["unresolved_employees"], 0)
        desktop_conn = self.desktop.get_db_connection()
        imported = desktop_conn.execute(
            "SELECT salary_rate, is_active FROM employees WHERE name = 'Server Only Employee'"
        ).fetchone()
        deleted = desktop_conn.execute(
            "SELECT id FROM employees WHERE name = 'Locally Deleted Employee'"
        ).fetchone()
        self.assertEqual(tuple(imported), (450, 1))
        self.assertIsNone(deleted)
        second_result = self._worker()
        self.assertEqual(second_result["imported_employees"], 0)
        self.assertEqual(desktop_conn.execute(
            "SELECT COUNT(*) FROM employees WHERE name = 'Server Only Employee'"
        ).fetchone()[0], 1)
        desktop_conn.close()

    def test_desktop_employee_deactivation_is_soft_and_survives_repeat_sync(self):
        employee_name = f"Deactivate Test {uuid.uuid4().hex[:8]}"
        employee_key = self.desktop.employee_sync_key(employee_name)
        pin_hash = generate_password_hash("isolated-test-pin")
        server_conn = self.server.get_db_connection()
        profile_id = server_conn.execute(
            "INSERT INTO meta_employees (name, salary_rate, is_active) VALUES (?, 450, 1)",
            (employee_name,),
        ).lastrowid
        server_conn.execute("""
            INSERT INTO users (username, password_hash, role, employee_id, is_active, created_at)
            VALUES (?, ?, 'user', ?, 1, 'test')
        """, (employee_name, pin_hash, profile_id))
        server_conn.commit()
        server_conn.close()

        desktop_conn = self.desktop.get_db_connection()
        desktop_conn.execute(
            "INSERT INTO employees (name, salary_rate, is_active) VALUES (?, 450, 1)",
            (employee_name,),
        )
        employee_id = desktop_conn.execute(
            "SELECT id FROM employees WHERE name = ?", (employee_name,)
        ).fetchone()[0]
        desktop_conn.execute("""
            INSERT INTO shifts (
                date, employee, company, object_name, hours, rate, sync_id, sync_version
            ) VALUES ('2026-10-16', ?, 'Renatur', 'Tomteboda doors', 8, 0, ?, 1)
        """, (employee_name, str(uuid.uuid4())))
        desktop_conn.commit()
        desktop_conn.close()

        class EmployeeTable:
            def currentRow(self):
                return 0

            def item(self, _row, _column):
                return SimpleNamespace(
                    text=lambda: employee_name,
                    data=lambda _role: employee_id,
                )

        harness = SimpleNamespace(
            table_employees=EmployeeTable(),
            load_dropdowns=lambda: None,
            sync_with_cloud=lambda: self._worker(),
        )
        self.desktop.SmartReportApp.toggle_employee_active(harness)
        desktop_conn = self.desktop.get_db_connection()
        local_profile = desktop_conn.execute(
            "SELECT salary_rate, is_active FROM employees WHERE name = ?", (employee_name,)
        ).fetchone()
        local_history = desktop_conn.execute(
            "SELECT COUNT(*) FROM shifts WHERE employee = ?", (employee_name,)
        ).fetchone()[0]
        desktop_conn.close()
        self.assertEqual(tuple(local_profile), (450, 0))
        self.assertEqual(local_history, 1)

        class EmployeeCombo:
            def __init__(self):
                self.items = []

            def clear(self):
                self.items.clear()

            def addItem(self, item):
                self.items.append(item)

        combo = EmployeeCombo()
        self.desktop.SmartReportApp.load_dropdowns(SimpleNamespace(emp_cb=combo))
        self.assertNotIn(employee_name, combo.items)
        server_conn = self.server.get_db_connection()
        server_profile = server_conn.execute(
            "SELECT salary_rate, is_active FROM meta_employees WHERE id = ?", (profile_id,)
        ).fetchone()
        user_active = server_conn.execute(
            "SELECT is_active FROM users WHERE employee_id = ?", (profile_id,)
        ).fetchone()[0]
        event_count = server_conn.execute(
            "SELECT COUNT(*) FROM employee_sync_events WHERE employee_key = ? AND operation = 'deactivate'",
            (employee_key,),
        ).fetchone()[0]
        server_conn.close()
        self.assertEqual(tuple(server_profile), (450, 0))
        self.assertEqual(user_active, 0)
        self.assertEqual(event_count, 1)

        second_result = self._worker()
        self.assertEqual(second_result["conflicts"], 0)
        desktop_conn = self.desktop.get_db_connection()
        self.assertEqual(tuple(desktop_conn.execute(
            "SELECT salary_rate, is_active FROM employees WHERE name = ?", (employee_name,)
        ).fetchone()), (450, 0))
        self.assertEqual(desktop_conn.execute(
            "SELECT COUNT(*) FROM shifts WHERE employee = ?", (employee_name,)
        ).fetchone()[0], 1)
        self.assertEqual(desktop_conn.execute("SELECT COUNT(*) FROM employee_sync_outbox").fetchone()[0], 0)
        desktop_conn.close()
        combo = EmployeeCombo()
        self.desktop.SmartReportApp.load_dropdowns(SimpleNamespace(emp_cb=combo))
        self.assertNotIn(employee_name, combo.items)
        server_conn = self.server.get_db_connection()
        self.assertEqual(tuple(server_conn.execute(
            "SELECT salary_rate, is_active FROM meta_employees WHERE id = ?", (profile_id,)
        ).fetchone()), (450, 0))
        self.assertEqual(server_conn.execute(
            "SELECT COUNT(*) FROM employee_sync_events WHERE employee_key = ? AND operation = 'deactivate'",
            (employee_key,),
        ).fetchone()[0], 1)
        server_conn.close()

        self.desktop.SmartReportApp.toggle_employee_active(harness)
        self.assertEqual(self._worker()["conflicts"], 0)
        server_conn = self.server.get_db_connection()
        self.assertEqual(tuple(server_conn.execute(
            "SELECT salary_rate, is_active FROM meta_employees WHERE id = ?", (profile_id,)
        ).fetchone()), (450, 1))
        self.assertEqual(tuple(server_conn.execute(
            "SELECT password_hash, is_active FROM users WHERE employee_id = ?", (profile_id,)
        ).fetchone()), (pin_hash, 1))
        self.assertEqual(server_conn.execute(
            "SELECT COUNT(*) FROM employee_sync_events WHERE employee_key = ? AND operation = 'activate'",
            (employee_key,),
        ).fetchone()[0], 1)
        server_conn.close()
        desktop_conn = self.desktop.get_db_connection()
        self.assertEqual(tuple(desktop_conn.execute(
            "SELECT id, salary_rate, is_active FROM employees WHERE name = ?", (employee_name,)
        ).fetchone()), (employee_id, 450, 1))
        self.assertEqual(desktop_conn.execute(
            "SELECT COUNT(*) FROM shifts WHERE employee = ?", (employee_name,)
        ).fetchone()[0], 1)
        self.assertEqual(desktop_conn.execute("SELECT COUNT(*) FROM employee_sync_outbox").fetchone()[0], 0)
        desktop_conn.close()

    def test_archive_filters_counts_stable_selection_and_action_state(self):
        desktop_conn = self.desktop.get_db_connection()
        desktop_conn.execute("DELETE FROM objects")
        desktop_conn.execute("DELETE FROM employees")
        for is_active, name in ((1, "Active"), (0, "Archived")):
            desktop_conn.execute("""
                INSERT INTO objects (company, name, markning, rate, transport_rate,
                                     is_active, sync_id, server_id, sync_version)
                VALUES ('Renatur', ?, ?, 520, 450, ?, ?, ?, 2)
            """, (name, name, is_active, str(uuid.uuid4()), 100 + is_active))
            desktop_conn.execute(
                "INSERT INTO employees (name, salary_rate, is_active) VALUES (?, 450, ?)",
                (name, is_active),
            )
        desktop_conn.commit()
        object_id = desktop_conn.execute(
            "SELECT id FROM objects WHERE name = 'Archived'"
        ).fetchone()[0]
        employee_id = desktop_conn.execute(
            "SELECT id FROM employees WHERE name = 'Archived'"
        ).fetchone()[0]
        desktop_conn.close()

        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_sprav = self.desktop.QWidget()
        window.setup_sprav_tab()
        response = SimpleNamespace(status_code=200, json=lambda: {
            "users": [{"username": "Archived", "has_pin": True}]
        })
        try:
            with patch.object(self.desktop.requests, "get", return_value=response):
                window.load_objects_table()
                window.load_employees_table()
                self.assertFalse(window.show_archived_objects_check.isChecked())
                self.assertFalse(window.show_inactive_employees_check.isChecked())
                self.assertEqual(window.table_objects.rowCount(), 1)
                self.assertEqual(window.table_employees.rowCount(), 1)
                self.assertEqual(window.object_counts_label.text(), "Активных: 1 | В архиве: 1")
                self.assertEqual(window.employee_counts_label.text(), "Активных: 1 | Неактивных: 1")
                self.assertFalse(window.object_archive_button.isEnabled())
                self.assertFalse(window.employee_status_button.isEnabled())
                window.show_archived_objects_check.setChecked(True)
                window.show_inactive_employees_check.setChecked(True)
                self.assertEqual(window.table_objects.rowCount(), 2)
                self.assertEqual(window.table_employees.rowCount(), 2)
                role = self.desktop.Qt.ItemDataRole.UserRole
                object_row = next(row for row in range(2) if window.table_objects.item(row, 0).data(role) == object_id)
                employee_row = next(row for row in range(2) if window.table_employees.item(row, 0).data(role) == employee_id)
                window.table_objects.selectRow(object_row)
                window.table_employees.selectRow(employee_row)
                self.assertEqual(window.object_archive_button.text(), "Восстановить из архива")
                self.assertEqual(window.employee_status_button.text(), "Активировать сотрудника")
                self.assertEqual(window.table_employees.item(employee_row, 2).text(), "Неактивен")
                self.assertEqual(window.table_employees.item(employee_row, 3).text(), "Задан")
                window.load_objects_table()
                window.load_employees_table()
                self.assertEqual(window.table_objects.item(window.table_objects.currentRow(), 0).data(role), object_id)
                self.assertEqual(window.table_employees.item(window.table_employees.currentRow(), 0).data(role), employee_id)
                window.show_archived_objects_check.setChecked(False)
                window.show_inactive_employees_check.setChecked(False)
                self.assertEqual(window.table_objects.currentRow(), -1)
                self.assertEqual(window.table_employees.currentRow(), -1)
                self.assertFalse(window.object_archive_button.isEnabled())
                self.assertFalse(window.employee_status_button.isEnabled())
        finally:
            window.close()
            window.deleteLater()

    def test_history_selector_keeps_only_original_archived_object(self):
        desktop_conn = self.desktop.get_db_connection()
        desktop_conn.execute("DELETE FROM objects")
        for name, is_active in (("Active", 1), ("Original", 0), ("Other archive", 0)):
            desktop_conn.execute("""
                INSERT INTO objects (company, name, rate, transport_rate, is_active)
                VALUES ('Renatur', ?, 520, 450, ?)
            """, (name, is_active))
        desktop_conn.commit()
        desktop_conn.close()
        combo = self.desktop.QComboBox()
        load = self.desktop.SmartReportApp.load_objects_for_company
        load(SimpleNamespace(), "Renatur", combo, self.desktop.RECORD_TYPE_WORK)
        self.assertEqual([combo.itemData(index) for index in range(combo.count())], ["Active"])
        combo.clear()
        load(SimpleNamespace(), "Renatur", combo, self.desktop.RECORD_TYPE_WORK,
             archived_object_name="Original")
        self.assertEqual(combo.currentData(), "Original")
        self.assertIn("В архиве", combo.currentText())
        self.assertEqual([combo.itemData(index) for index in range(combo.count())], ["Active", "Original"])
        load(SimpleNamespace(), "Renatur", combo, self.desktop.RECORD_TYPE_WORK)
        self.assertEqual([combo.itemData(index) for index in range(combo.count())], ["Active"])

    def test_edit_history_preserves_archived_object_inactive_employee_and_rates(self):
        desktop_conn = self.desktop.get_db_connection()
        desktop_conn.execute(
            "INSERT INTO employees (name, salary_rate, is_active) VALUES ('Inactive employee', 450, 0)"
        )
        desktop_conn.execute("""
            INSERT INTO objects (company, name, rate, transport_rate, is_active)
            VALUES ('Renatur', 'Archived history object', 520, 450, 0)
        """)
        shift_id = desktop_conn.execute("""
            INSERT INTO shifts (date, employee, company, object_name, hours, rate,
                                transport, comment, sync_id, sync_version, entry_type)
            VALUES ('2026-10-16', 'Inactive employee', 'Renatur', 'Archived history object',
                    8, 510, 440, 'history', ?, 1, 'work')
        """, (str(uuid.uuid4()),)).lastrowid
        desktop_conn.commit()
        desktop_conn.close()
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.table_shifts = self.desktop.QTableWidget(1, 1)
        window.table_shifts.setItem(0, 0, self.desktop.QTableWidgetItem(str(shift_id)))
        window.table_shifts.selectRow(0)

        def accept_edit(dialog):
            combos = dialog.findChildren(self.desktop.QComboBox)
            employee = next(combo for combo in combos if combo.currentData() == "Inactive employee")
            self.assertIn("Неактивен", employee.currentText())
            archived = next(combo for combo in combos if combo.currentData() == "Archived history object")
            self.assertIn("В архиве", archived.currentText())
            return self.desktop.QDialog.DialogCode.Accepted

        try:
            with patch.object(self.desktop.QDialog, "exec", accept_edit), \
                 patch.object(self.desktop.QMessageBox, "information"), \
                 patch.object(window, "load_shifts_history"), \
                 patch.object(window, "background_sync_with_cloud"):
                window.edit_shift()
            desktop_conn = self.desktop.get_db_connection()
            self.assertEqual(tuple(desktop_conn.execute(
                "SELECT employee, object_name, rate, transport, comment FROM shifts WHERE id = ?",
                (shift_id,),
            ).fetchone()), ("Inactive employee", "Archived history object", 510, 440, "history"))
            self.assertEqual(desktop_conn.execute("SELECT COUNT(*) FROM shift_sync_outbox").fetchone()[0], 1)
            desktop_conn.close()

            def accept_correction(dialog):
                employee = next(combo for combo in dialog.findChildren(self.desktop.QComboBox)
                                if combo.currentData() == "Inactive employee")
                employee.setEditText("Corrected employee")
                return self.desktop.QDialog.DialogCode.Accepted

            with patch.object(self.desktop.QDialog, "exec", accept_correction), \
                 patch.object(self.desktop.QMessageBox, "information"), \
                 patch.object(window, "load_shifts_history"), \
                 patch.object(window, "background_sync_with_cloud"):
                window.edit_shift()
            desktop_conn = self.desktop.get_db_connection()
            self.assertEqual(desktop_conn.execute(
                "SELECT employee FROM shifts WHERE id = ?", (shift_id,)
            ).fetchone()[0], "Corrected employee")
            desktop_conn.close()
        finally:
            window.close()
            window.deleteLater()

    def test_service_objects_restore_through_outbox_without_duplicates(self):
        company = "Bygger och Renoverar i Sthm AB"
        self._add_company(company)
        desktop_conn = self.desktop.get_db_connection()
        identities = []
        for name, markning, record_type in (
            ("Arbetstidsförkortning", "p1000", self.desktop.RECORD_TYPE_ATF),
            ("Sjuk", "p1001", self.desktop.RECORD_TYPE_SICK),
        ):
            sync_id = str(uuid.uuid4())
            object_id = desktop_conn.execute("""
                INSERT INTO objects (company, name, markning, rate, transport_rate, is_active, sync_id)
                VALUES (?, ?, ?, 520, 450, 1, ?)
            """, (company, name, markning, sync_id)).lastrowid
            self.desktop.enqueue_object_sync_event(desktop_conn.cursor(), object_id, "upsert")
            identities.append((object_id, name, markning, sync_id, record_type))
        desktop_conn.commit()
        desktop_conn.close()
        self.assertEqual(self._worker()["conflicts"], 0)

        for object_id, name, _markning, _sync_id, record_type in identities:
            item = self.desktop.QTableWidgetItem(name)
            item.setData(self.desktop.Qt.ItemDataRole.UserRole, object_id)
            table = SimpleNamespace(currentRow=lambda: 0, item=lambda *_: item)
            harness = SimpleNamespace(
                table_objects=table, load_dropdowns=lambda: None,
                background_sync_with_cloud=lambda: None,
            )
            self.desktop.SmartReportApp.toggle_object_archive(harness)
            self.assertEqual(self._worker()["conflicts"], 0)
            combo = self.desktop.QComboBox()
            self.assertFalse(self.desktop.SmartReportApp.load_objects_for_company(
                SimpleNamespace(), company, combo, record_type
            ))
            self.desktop.SmartReportApp.toggle_object_archive(harness)
            with self.assertRaises(self.desktop.requests.ConnectionError):
                self._worker("offline")
            with self.assertRaises(self.desktop.requests.ConnectionError):
                self._worker("lost_ack")
            self.assertEqual(self._worker()["conflicts"], 0)
            self.assertEqual(self._worker()["conflicts"], 0)
            self.assertTrue(self.desktop.SmartReportApp.load_objects_for_company(
                SimpleNamespace(), company, combo, record_type
            ))
            self.assertEqual(combo.count(), 1)
            self.assertEqual(combo.currentData(), name)

        desktop_conn = self.desktop.get_db_connection()
        server_conn = self.server.get_db_connection()
        for object_id, name, markning, sync_id, _record_type in identities:
            self.assertEqual(tuple(desktop_conn.execute(
                "SELECT id, name, markning, sync_id, rate, transport_rate, is_active, sync_version "
                "FROM objects WHERE sync_id = ?", (sync_id,),
            ).fetchone()), (object_id, name, markning, sync_id, 520, 450, 1, 3))
            self.assertEqual(tuple(server_conn.execute(
                "SELECT name, markning, rate, transport_rate, is_active, sync_version "
                "FROM meta_objects WHERE sync_id = ?", (sync_id,),
            ).fetchone()), (name, markning, 520, 450, 1, 3))
            self.assertEqual(server_conn.execute(
                "SELECT COUNT(*) FROM object_sync_events WHERE sync_id = ?", (sync_id,)
            ).fetchone()[0], 3)
        self.assertEqual(desktop_conn.execute("SELECT COUNT(*) FROM object_sync_outbox").fetchone()[0], 0)
        self.assertEqual(server_conn.execute(
            "SELECT COUNT(*) FROM meta_objects WHERE company = ?", (company,)
        ).fetchone()[0], 2)
        desktop_conn.close()
        server_conn.close()

    def test_archived_object_allows_existing_history_edit_but_not_new_shift(self):
        server_conn = self.server.get_db_connection()
        server_conn.execute("""
            INSERT INTO meta_objects (company, name, markning, rate, transport_rate, is_active)
            VALUES ('Renatur', 'History object', '', 520, 450, 1)
        """)
        server_conn.commit()
        server_conn.close()
        sync_id = str(uuid.uuid4())
        payload = {
            "date": "2026-10-16", "employee": "Historical employee", "company": "Renatur",
            "object_name": "History object", "hours": 8, "rate": 520,
            "transport": 450, "comment": "before", "record_type": "work", "invoiced": 0,
        }

        def push(target_id, revision):
            return self.server_client.post("/sync/push", headers={
                "X-Sync-Key": "isolated-sync-test-key",
            }, json={"changes": [{
                "sync_id": target_id, "operation_id": str(uuid.uuid4()),
                "operation": "upsert", "base_version": revision, "shift": payload,
            }]})

        self.assertEqual(push(sync_id, 0).status_code, 200)
        server_conn = self.server.get_db_connection()
        server_conn.execute("UPDATE meta_objects SET is_active = 0 WHERE name = 'History object'")
        server_conn.commit()
        server_conn.close()
        payload["comment"] = "after"
        response = push(sync_id, 1)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(push(str(uuid.uuid4()), 0).status_code, 400)
        server_conn = self.server.get_db_connection()
        self.assertEqual(tuple(server_conn.execute(
            "SELECT object_name, rate, transport, comment FROM incoming_submissions WHERE sync_id = ?",
            (sync_id,),
        ).fetchone()), ("History object", 520, 450, "after"))
        self.assertEqual(server_conn.execute("SELECT COUNT(*) FROM incoming_submissions WHERE sync_id = ?", (sync_id,)).fetchone()[0], 1)
        server_conn.close()

    def test_delete_sync_is_allowed_after_object_is_removed_and_replay_is_idempotent(self):
        server_conn = self.server.get_db_connection()
        server_conn.execute("""
            INSERT INTO meta_objects (company, name, markning, rate, transport_rate, is_active)
            VALUES ('Renatur', 'Removed object', '', 520, 450, 1)
        """)
        server_conn.commit()
        server_conn.close()
        sync_id = str(uuid.uuid4())
        payload = {
            "date": "2026-10-16", "employee": "Historical employee", "company": "Renatur",
            "object_name": "Removed object", "hours": 8, "rate": 520, "transport": 450,
            "comment": "", "record_type": "work", "invoiced": 0,
        }
        operation_id = str(uuid.uuid4())
        upsert = self.server_client.post("/sync/push", headers={
            "X-Sync-Key": "isolated-sync-test-key",
        }, json={"changes": [{
            "sync_id": sync_id, "operation_id": str(uuid.uuid4()),
            "operation": "upsert", "base_version": 0, "shift": payload,
        }]})
        self.assertEqual(upsert.status_code, 200, upsert.get_json())

        server_conn = self.server.get_db_connection()
        server_conn.execute("DELETE FROM meta_objects WHERE company = 'Renatur' AND name = 'Removed object'")
        server_conn.commit()
        server_conn.close()
        tombstone = {
            "sync_id": sync_id, "operation_id": operation_id,
            "operation": "delete", "base_version": 1, "shift": payload,
        }
        response = self.server_client.post("/sync/push", headers={
            "X-Sync-Key": "isolated-sync-test-key",
        }, json={"changes": [tombstone]})
        self.assertEqual(response.status_code, 200, response.get_json())
        result = response.get_json()["results"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["version"], 2)

        replay = self.server_client.post("/sync/push", headers={
            "X-Sync-Key": "isolated-sync-test-key",
        }, json={"changes": [tombstone]})
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertTrue(replay.get_json()["results"][0]["idempotent"])
        server_conn = self.server.get_db_connection()
        rows = server_conn.execute(
            "SELECT is_deleted, last_operation_id FROM incoming_submissions WHERE sync_id = ?",
            (sync_id,),
        ).fetchall()
        server_conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0]), (1, operation_id))

    def test_sync_rejection_displays_server_error_body(self):
        response = self.desktop.requests.Response()
        response.status_code = 400
        response._content = '{"ok":false,"error":"Объект не принадлежит выбранной фирме или не существует."}'.encode("utf-8")
        response.headers["Content-Type"] = "application/json"
        with self.assertRaisesRegex(
            RuntimeError,
            "Сервер отклонил синхронизацию \\(HTTP 400\\): Объект не принадлежит",
        ):
            self.desktop.raise_for_sync_response(response)

    def test_special_time_submissions_sync_history_reports_and_payroll_use_existing_objects(self):
        company = "Bygger och Renoverar i Sthlm AB"
        employee = "Vacation Test Employee"
        object_name = "Semestertimmar"
        holiday_employee = "Holiday Test Employee"
        holiday_object_name = "Internal Red Days"
        self._add_company(company)

        server_conn = self.server.get_db_connection()
        server_conn.execute(
            "INSERT INTO meta_employees (name, salary_rate, is_active) VALUES (?, 520, 1)",
            (employee,),
        )
        server_conn.execute(
            "INSERT INTO meta_employees (name, salary_rate, is_active) VALUES (?, 520, 1)",
            (holiday_employee,),
        )
        server_conn.execute("""
            INSERT INTO meta_objects (company, name, markning, rate, transport_rate, is_active)
            VALUES (?, ?, 'p1002', 0, 0, 1)
        """, (company, object_name))
        server_conn.execute("""
            INSERT INTO meta_objects (company, name, markning, rate, transport_rate, is_active)
            VALUES (?, ?, 'p1004', 0, 0, 1)
        """, (company, holiday_object_name))
        server_conn.commit()
        server_conn.close()

        desktop_conn = self.desktop.get_db_connection()
        desktop_conn.execute(
            "INSERT INTO employees (name, salary_rate) VALUES (?, 520)",
            (employee,),
        )
        desktop_conn.execute(
            "INSERT INTO employees (name, salary_rate) VALUES (?, 520)",
            (holiday_employee,),
        )
        desktop_conn.execute("""
            INSERT INTO objects (company, name, markning, rate, transport_rate, is_active)
            VALUES (?, ?, 'p1002', 0, 0, 1)
        """, (company, object_name))
        desktop_conn.execute("""
            INSERT INTO objects (company, name, markning, rate, transport_rate, is_active)
            VALUES (?, ?, 'p1004', 0, 0, 1)
        """, (company, holiday_object_name))
        desktop_conn.commit()
        desktop_conn.close()

        with self.server_client.session_transaction() as web_session:
            web_session["user_id"] = 1
            web_session["role"] = "admin"

        form_response = self.server_client.get("/")
        self.assertEqual(form_response.status_code, 200)
        self.assertIn(b"holiday", form_response.data)
        self.assertIn(b"p1004", form_response.data)
        self.assertIn(holiday_object_name.encode(), form_response.data)

        work_objects = self.desktop.QComboBox()
        holiday_objects = self.desktop.QComboBox()
        self.desktop.SmartReportApp.load_objects_for_company(
            SimpleNamespace(), company, work_objects, self.desktop.RECORD_TYPE_WORK
        )
        self.desktop.SmartReportApp.load_objects_for_company(
            SimpleNamespace(), company, holiday_objects, self.desktop.RECORD_TYPE_HOLIDAY
        )
        self.assertNotIn(holiday_object_name, [
            work_objects.itemData(index) for index in range(work_objects.count())
        ])
        self.assertEqual(
            [holiday_objects.itemData(index) for index in range(holiday_objects.count())],
            [holiday_object_name],
        )

        shift_date = date.today().isoformat()
        invalid_work_submission = self.server_client.post("/submit-shift", json={
            "request_id": str(uuid.uuid4()),
            "date": shift_date,
            "employee": holiday_employee,
            "company": company,
            "object_name": holiday_object_name,
            "hours": 8,
            "rate": 520,
            "transport": 0,
            "submission_type": "work",
        })
        self.assertEqual(invalid_work_submission.status_code, 400)

        submission = self.server_client.post("/submit-shift", json={
            "request_id": str(uuid.uuid4()),
            "date": shift_date,
            "employee": employee,
            "company": company,
            "object_name": object_name,
            "hours": 4.5,
            "rate": 520,
            "transport": 1,
            "submission_type": "vacation",
        })
        self.assertEqual(submission.status_code, 201, submission.get_json())

        history_response = self.server_client.post("/check-employee-shifts", json={
            "employee": employee,
            "month": shift_date[:7],
        })
        self.assertEqual(history_response.status_code, 200)
        history_shift = history_response.get_json()["recent_shifts"][0]
        self.assertEqual(
            (history_shift["submission_type"], history_shift["object_name"],
             history_shift["marking"], history_shift["hours"]),
            ("vacation", object_name, "p1002", 4.5),
        )

        holiday_submission = self.server_client.post("/submit-shift", json={
            "request_id": str(uuid.uuid4()),
            "date": shift_date,
            "employee": holiday_employee,
            "company": company,
            "object_name": holiday_object_name,
            "hours": 2,
            "rate": 520,
            "transport": 10,
            "submission_type": "holiday",
        })
        self.assertEqual(holiday_submission.status_code, 201, holiday_submission.get_json())
        holiday_history = self.server_client.post("/check-employee-shifts", json={
            "employee": holiday_employee,
            "month": shift_date[:7],
        })
        self.assertEqual(holiday_history.status_code, 200)
        holiday_shift = holiday_history.get_json()["recent_shifts"][0]
        self.assertEqual(
            (holiday_shift["submission_type"], holiday_shift["object_name"],
             holiday_shift["marking"], holiday_shift["hours"]),
            ("holiday", holiday_object_name, "p1004", 2),
        )

        sync_result = self._worker()
        self.assertEqual(sync_result["conflicts"], 0)
        desktop_conn = self.desktop.get_db_connection()
        synced_shift = desktop_conn.execute("""
            SELECT date, employee, company, object_name, hours, rate, transport, entry_type
            FROM shifts WHERE employee = ?
        """, (employee,)).fetchone()
        self.assertEqual(tuple(synced_shift), (
            shift_date, employee, company, object_name, 4.5, 0.0, 0.0, "vacation",
        ))
        self.assertEqual(self.desktop.record_type_short_label(synced_shift["entry_type"]), "Отпуск")
        self.assertEqual(desktop_conn.execute(
            "SELECT SUM(CASE WHEN entry_type = 'vacation' THEN hours ELSE 0 END) "
            "FROM shifts WHERE date = ? AND employee = ?",
            (shift_date, employee),
        ).fetchone()[0], 4.5)
        synced_holiday = desktop_conn.execute("""
            SELECT date, employee, company, object_name, hours, rate, transport, entry_type
            FROM shifts WHERE employee = ?
        """, (holiday_employee,)).fetchone()
        self.assertEqual(tuple(synced_holiday), (
            shift_date, holiday_employee, company, holiday_object_name, 2.0, 0.0, 0.0, "holiday",
        ))
        self.assertEqual(
            self.desktop.record_type_short_label(synced_holiday["entry_type"]),
            "Красные дни",
        )
        idle_employee = "No Salary Activity"
        inactive_employee = "Inactive Salary Activity"
        desktop_conn.execute(
            "INSERT INTO employees (name, salary_rate, is_active) VALUES (?, 520, 1)",
            (idle_employee,),
        )
        desktop_conn.execute(
            "INSERT INTO employees (name, salary_rate, is_active) VALUES (?, 520, 0)",
            (inactive_employee,),
        )
        for entry_type, hours in (
            ("work", 10.0), ("atf", 1.0), ("sick", 1.0),
            ("vacation", 1.5), ("ob", 1.0),
        ):
            desktop_conn.execute("""
                INSERT INTO shifts (
                    date, employee, company, object_name, hours, rate, transport, comment,
                    entry_type
                ) VALUES (?, ?, ?, ?, ?, 0, 0, '', ?)
            """, (
                shift_date, inactive_employee, company, object_name, hours, entry_type,
            ))
        desktop_conn.commit()
        desktop_conn.close()

        from PyQt6.QtWidgets import QDoubleSpinBox, QLabel, QTableWidget

        report_ui = SimpleNamespace(
            dag_date_from=SimpleNamespace(text=lambda: shift_date),
            dag_date_to=SimpleNamespace(text=lambda: shift_date),
            dag_comp_cb=SimpleNamespace(currentText=lambda: company),
            dag_obj_cb=SimpleNamespace(
                currentData=lambda: object_name,
                currentText=lambda: object_name,
            ),
            table_dagbok=QTableWidget(0, 6),
            dag_total_hours_lbl=QLabel(),
            dag_total_atf_lbl=QLabel(),
            dag_total_sick_lbl=QLabel(),
            dag_total_vacation_lbl=QLabel(),
            dag_total_holiday_lbl=QLabel(),
            dag_total_ob_lbl=QLabel(),
        )
        conn = self.desktop.get_db_connection()
        journal_rows = [dict(row) for row in conn.execute(
            "SELECT * FROM shifts WHERE company=? AND object_name=? AND date=? ORDER BY date,employee,id",
            (company, object_name, shift_date),
        )]
        conn.close()
        self.desktop.SmartReportApp._render_dagbok_report(
            report_ui, {"journal_rows": journal_rows, "journal_segments": {}},
        )
        vacation_history_row = next(
            row for row in range(report_ui.table_dagbok.rowCount())
            if report_ui.table_dagbok.item(row, 1).text() == employee
        )
        self.assertEqual(report_ui.table_dagbok.item(vacation_history_row, 2).text(), "Отпуск")
        self.assertEqual(
            report_ui.table_dagbok.item(vacation_history_row, 4).text(),
            "Отпуск / Semestertimmar (p1002)",
        )
        self.assertIn("6.0", report_ui.dag_total_vacation_lbl.text())

        holiday_report_ui = SimpleNamespace(
            dag_date_from=SimpleNamespace(text=lambda: shift_date),
            dag_date_to=SimpleNamespace(text=lambda: shift_date),
            dag_comp_cb=SimpleNamespace(currentText=lambda: company),
            dag_obj_cb=SimpleNamespace(
                currentData=lambda: holiday_object_name,
                currentText=lambda: holiday_object_name,
            ),
            table_dagbok=QTableWidget(0, 6),
            dag_total_hours_lbl=QLabel(),
            dag_total_atf_lbl=QLabel(),
            dag_total_sick_lbl=QLabel(),
            dag_total_vacation_lbl=QLabel(),
            dag_total_holiday_lbl=QLabel(),
            dag_total_ob_lbl=QLabel(),
        )
        conn = self.desktop.get_db_connection()
        journal_rows = [dict(row) for row in conn.execute(
            "SELECT * FROM shifts WHERE company=? AND object_name=? AND date=? ORDER BY date,employee,id",
            (company, holiday_object_name, shift_date),
        )]
        conn.close()
        self.desktop.SmartReportApp._render_dagbok_report(
            holiday_report_ui, {"journal_rows": journal_rows, "journal_segments": {}},
        )
        self.assertEqual(holiday_report_ui.table_dagbok.item(0, 2).text(), "Красные дни")
        self.assertEqual(
            holiday_report_ui.table_dagbok.item(0, 4).text(),
            "Красные дни / Röda dagar (p1004)",
        )
        self.assertIn("2.0", holiday_report_ui.dag_total_holiday_lbl.text())

        salary_ui = SimpleNamespace(
            sal_date_from=SimpleNamespace(text=lambda: shift_date),
            sal_date_to=SimpleNamespace(text=lambda: shift_date),
            table_salary=QTableWidget(0, 17),
            sal_vacation_percent=QDoubleSpinBox(),
            sal_tax_percent=QDoubleSpinBox(),
            sal_employer_fee_percent=QDoubleSpinBox(),
            sal_atf_percent=QDoubleSpinBox(),
            sal_sick_percent=QDoubleSpinBox(),
            sal_summary_hours_lbl=QLabel(),
            sal_summary_vacation_lbl=QLabel(),
            sal_summary_ob_lbl=QLabel(),
            sal_summary_net_lbl=QLabel(),
            sal_summary_employer_lbl=QLabel(),
            payroll_history_note=QLabel(),
            on_salary_cell_changed=lambda *_: None,
        )
        salary_ui.sal_vacation_percent.setValue(13)
        salary_ui.load_salary_table_template = lambda: self.desktop.SmartReportApp.load_salary_table_template(salary_ui)
        verify_conn = self.desktop.get_db_connection()
        import payroll_history
        for fixture_employee in (employee, holiday_employee, inactive_employee):
            fixture_rate = verify_conn.execute(
                "SELECT salary_rate FROM employees WHERE name=?", (fixture_employee,)
            ).fetchone()[0]
            payroll_history.apply_action(
                verify_conn.cursor(), "shifts", {
                    "kind": "rate", "employee": fixture_employee, "effective_from": shift_date,
                    "rate": fixture_rate, "operation_id": str(uuid.uuid4()),
                    "expected_seq": len(payroll_history.events(verify_conn.cursor())),
                }, "explicit synthetic fixture confirmation", writes_enabled=True,
            )
        verify_conn.commit()
        self.assertEqual(verify_conn.execute(
            "SELECT COUNT(*) FROM shifts WHERE employee = ?", (inactive_employee,)
        ).fetchone()[0], 5)
        verify_conn.close()
        self.desktop.SmartReportApp.load_salary_table_template(salary_ui)
        displayed_employees = {
            salary_ui.table_salary.item(row, 0).text()
            for row in range(salary_ui.table_salary.rowCount())
        }
        self.assertEqual(displayed_employees, {employee, holiday_employee, inactive_employee})
        salary_row = next(
            row for row in range(salary_ui.table_salary.rowCount())
            if salary_ui.table_salary.item(row, 0).text() == employee
        )
        self.assertEqual(float(salary_ui.table_salary.item(salary_row, 1).text()), 0)
        self.assertEqual(
            float(salary_ui.table_salary.item(salary_row, 15).text()), 4.5,
            salary_ui.sal_summary_vacation_lbl.text(),
        )
        self.assertEqual(salary_ui.table_salary.item(salary_row, 7).text(), "0.00")
        self.assertEqual(salary_ui.table_salary.item(salary_row, 14).text(), "0.00")
        inactive_row = next(
            row for row in range(salary_ui.table_salary.rowCount())
            if salary_ui.table_salary.item(row, 0).text() == inactive_employee
        )
        self.assertEqual(float(salary_ui.table_salary.item(inactive_row, 1).text()), 10)
        self.assertEqual(float(salary_ui.table_salary.item(inactive_row, 6).text()), 3)
        self.assertEqual(float(salary_ui.table_salary.item(inactive_row, 15).text()), 1.5)
        holiday_salary_row = next(
            row for row in range(salary_ui.table_salary.rowCount())
            if salary_ui.table_salary.item(row, 0).text() == holiday_employee
        )
        self.assertEqual(float(salary_ui.table_salary.item(holiday_salary_row, 1).text()), 0)
        self.assertEqual(float(salary_ui.table_salary.item(holiday_salary_row, 6).text()), 0)
        self.assertEqual(float(salary_ui.table_salary.item(holiday_salary_row, 7).text()), 0)
        self.assertEqual(float(salary_ui.table_salary.item(holiday_salary_row, 16).text()), 2)
        self.assertIn("10.0", salary_ui.sal_summary_hours_lbl.text())
        self.assertIn("6.0", salary_ui.sal_summary_vacation_lbl.text())

        with tempfile.TemporaryDirectory(prefix="smartreport-salary-pdf-") as report_dir, \
             patch.object(self.desktop, "REPORTS_DIR", report_dir), \
             patch("payroll_ui.QFileDialog.getSaveFileName",
                   return_value=(str(Path(report_dir)/"Loneunderlag_test.pdf"), "PDF (*.pdf)")), \
             patch.object(self.desktop.QMessageBox, "information"), \
             patch.object(self.desktop.os, "startfile", create=True):
            self.desktop.SmartReportApp.export_salary_to_pdf(salary_ui)
            from pypdf import PdfReader
            pdf_path = next(Path(report_dir).glob("Loneunderlag_*.pdf"))
            pdf_text = "\n".join(page.extract_text() for page in PdfReader(pdf_path).pages)
        self.assertIn(inactive_employee, pdf_text)
        self.assertIn(holiday_employee, pdf_text)
        self.assertIn("Сверх нормы, ч", pdf_text)
        self.assertIn("Röda dagar", pdf_text)
        self.assertNotIn(idle_employee, pdf_text)
        report_response = self.server_client.get(
            "/report-preview",
            query_string={"employee": employee, "month": shift_date[:7]},
        )
        self.assertEqual(report_response.status_code, 200)
        self.assertIn(b"Semestertimmar", report_response.data)
        self.assertIn(b"4.50 tim", report_response.data)
        holiday_report_response = self.server_client.get(
            "/report-preview",
            query_string={"employee": holiday_employee, "month": shift_date[:7]},
        )
        self.assertEqual(holiday_report_response.status_code, 200)
        self.assertIn(b"R\xc3\xb6da dagar", holiday_report_response.data)
        self.assertIn(b"2.00 tim", holiday_report_response.data)
        server_conn = self.server.get_db_connection()
        self.assertEqual(
            server_conn.execute(
                "SELECT COUNT(*) FROM meta_objects WHERE company = ? AND name = ? AND markning = 'p1002'",
                (company, object_name),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            server_conn.execute(
                "SELECT COUNT(*) FROM meta_objects WHERE company = ? AND markning = 'p1004'",
                (company,),
            ).fetchone()[0],
            1,
        )
        server_conn.close()

    def test_swedish_salary_norm_excludes_weekday_public_holidays(self):
        self.assertEqual(
            self.desktop.swedish_workday_norm("2026-05-01", "2026-05-31"),
            152.0,
        )
        self.assertEqual(
            self.desktop.swedish_workday_norm("2026-04-01", "2026-04-30"),
            160.0,
        )

    def test_stable_id_revision_merge_and_deactivation_preserve_history(self):
        sync_id = str(uuid.uuid4())
        desktop_conn = self.desktop.get_db_connection()
        local_id = desktop_conn.execute("""
            INSERT INTO objects (name, markning, company, rate, transport_rate, sync_id, is_active, sync_version)
            VALUES ('Old Name', 'p-stable', 'Renatur', 10, 1, ?, 1, 1)
        """, (sync_id,)).lastrowid
        desktop_conn.execute("""
            INSERT INTO shifts (date, employee, company, object_name, hours, rate, comment)
            VALUES ('2026-10-03', 'Test Employee', 'Renatur', 'Old Name', 8, 10, 'stable-history')
        """)
        desktop_conn.commit()
        stats = self.desktop.merge_server_object_catalog(desktop_conn.cursor(), [{
            "id": 9001, "sync_id": sync_id, "name": "New Name", "markning": "p-stable",
            "company": "Renatur", "rate": 20, "transport_rate": 2,
            "is_active": 0, "sync_version": 2,
        }])
        desktop_conn.commit()
        row = desktop_conn.execute(
            "SELECT id, name, rate, is_active, sync_version, server_id FROM objects WHERE sync_id = ?",
            (sync_id,),
        ).fetchone()
        history_name = desktop_conn.execute(
            "SELECT object_name FROM shifts WHERE comment = 'stable-history'"
        ).fetchone()[0]
        desktop_conn.close()
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(tuple(row), (local_id, "New Name", 20, 0, 2, 9001))
        self.assertEqual(history_name, "Old Name")

    def test_legacy_link_preserves_local_rates_and_checks_server_revision(self):
        server_conn = self.server.get_db_connection()
        server_id = server_conn.execute("""
            INSERT INTO meta_objects (name, markning, company, rate, transport_rate, is_active, sync_version)
            VALUES ('Local Rate Wins', 'p-link-rate', 'Renatur', 0, 0, 1, 0)
        """).lastrowid
        server_conn.commit()
        server_conn.close()
        sync_id = str(uuid.uuid4())
        event = {
            "operation_id": str(uuid.uuid4()), "sync_id": sync_id, "revision": 1,
            "operation": "link_legacy",
            "object": {
                "name": "Local Rate Wins", "markning": "p-link-rate", "company": "Renatur",
                "rate": 520, "transport_rate": 450, "server_object_id": server_id,
                "expected_server_name": "Local Rate Wins", "expected_server_company": "Renatur",
                "expected_server_markning": "p-link-rate", "expected_server_rate": 0,
                "expected_server_transport_rate": 0, "expected_server_is_active": 1,
                "expected_server_revision": 0,
            },
        }

        response = self._server_post({"object_events": [event]})
        self.assertEqual(response.status_code, 200)
        server_conn = self.server.get_db_connection()
        row = server_conn.execute(
            "SELECT sync_id, rate, transport_rate, sync_version FROM meta_objects WHERE id = ?",
            (server_id,),
        ).fetchone()
        server_conn.close()
        self.assertEqual(tuple(row), (sync_id, 520, 450, 1))

        server_conn = self.server.get_db_connection()
        stale_id = server_conn.execute("""
            INSERT INTO meta_objects (name, markning, company, rate, transport_rate, is_active, sync_version)
            VALUES ('Changed During Link', 'p-link-stale', 'Renatur', 100, 10, 1, 1)
        """).lastrowid
        server_conn.commit()
        server_conn.close()
        stale_event = {
            **event,
            "operation_id": str(uuid.uuid4()),
            "sync_id": str(uuid.uuid4()),
            "object": {
                **event["object"], "name": "Changed During Link", "markning": "p-link-stale",
                "server_object_id": stale_id, "expected_server_name": "Changed During Link",
                "expected_server_markning": "p-link-stale", "expected_server_rate": 0,
                "expected_server_transport_rate": 0, "expected_server_revision": 0,
            },
        }
        stale_response = self._server_post({"object_events": [stale_event]})
        self.assertEqual(stale_response.status_code, 400)
        server_conn = self.server.get_db_connection()
        unchanged = server_conn.execute(
            "SELECT sync_id, rate, transport_rate, sync_version FROM meta_objects WHERE id = ?",
            (stale_id,),
        ).fetchone()
        server_conn.close()
        self.assertEqual(tuple(unchanged), (None, 100, 10, 1))

    def test_offline_lost_ack_and_employee_restore_rate(self):
        employee_name = f"Sync Test {uuid.uuid4().hex[:8]}"
        object_name = f"Sync Object {uuid.uuid4().hex[:8]}"
        employee_key = self.desktop.employee_sync_key(employee_name)
        desktop_conn = self.desktop.get_db_connection()
        object_id = desktop_conn.execute("""
            INSERT INTO objects (name, markning, company, rate, transport_rate, sync_id, is_active)
            VALUES (?, 'p-test', 'Renatur', 50, 1, ?, 1)
        """, (object_name, str(uuid.uuid4()))).lastrowid
        object_event = self.desktop.enqueue_object_sync_event(
            desktop_conn.cursor(), object_id, "upsert"
        )
        desktop_conn.execute("INSERT INTO employees (name, salary_rate) VALUES (?, 70)", (employee_name,))
        desktop_conn.commit()
        with self.assertRaises(self.desktop.requests.ConnectionError):
            self._worker("offline")
        self.assertEqual(desktop_conn.execute(
            "SELECT COUNT(*) FROM object_sync_outbox WHERE operation_id = ?",
            (object_event["operation_id"],),
        ).fetchone()[0], 1)
        with self.assertRaises(self.desktop.requests.ConnectionError):
            self._worker("lost_ack")
        self.assertEqual(desktop_conn.execute(
            "SELECT COUNT(*) FROM object_sync_outbox WHERE operation_id = ?",
            (object_event["operation_id"],),
        ).fetchone()[0], 1)
        desktop_conn.close()
        retry_result = self._worker()
        self.assertEqual(retry_result["conflicts"], 0)
        self.assertEqual(retry_result["linked_legacy"], 0)
        desktop_conn = self.desktop.get_db_connection()
        self.assertEqual(desktop_conn.execute("SELECT COUNT(*) FROM object_sync_outbox").fetchone()[0], 0)
        desktop_conn.close()

        server_conn = self.server.get_db_connection()
        remote_object = server_conn.execute(
            "SELECT id, rate, is_active, sync_version FROM meta_objects WHERE sync_id = ?",
            (object_event["sync_id"],),
        ).fetchone()
        profile = server_conn.execute(
            "SELECT id, salary_rate, is_active FROM meta_employees WHERE name = ?",
            (employee_name,),
        ).fetchone()
        server_conn.execute("""
            INSERT INTO users (username, password_hash, role, employee_id, is_active, created_at)
            VALUES (?, ?, 'user', ?, 1, 'test')
        """, (employee_name, generate_password_hash("isolated-test-pin"), profile["id"]))
        server_conn.commit()
        server_conn.close()
        self.assertIsNotNone(remote_object)
        self.assertEqual(tuple(profile)[1:], (70, 1))

        desktop_conn = self.desktop.get_db_connection()
        desktop_conn.execute("UPDATE objects SET name = ?, rate = 55 WHERE id = ?", (object_name + " renamed", object_id))
        rename_event = self.desktop.enqueue_object_sync_event(desktop_conn.cursor(), object_id, "upsert")
        desktop_conn.execute("UPDATE employees SET salary_rate = 75 WHERE name = ?", (employee_name,))
        desktop_conn.execute("""
            INSERT INTO shifts (date, employee, company, object_name, hours, rate, comment)
            VALUES ('2026-10-04', ?, 'Renatur', ?, 8, 55, 'lifecycle-history')
        """, (employee_name, object_name))
        desktop_conn.commit()
        self._worker()
        desktop_conn.execute("UPDATE objects SET is_active = 0 WHERE id = ?", (object_id,))
        object_delete = self.desktop.enqueue_object_sync_event(desktop_conn.cursor(), object_id, "deactivate")
        desktop_conn.execute("DELETE FROM employees WHERE name = ?", (employee_name,))
        employee_delete = self.desktop.enqueue_employee_sync_event(desktop_conn.cursor(), employee_name, "deactivate")
        desktop_conn.commit()
        with self.assertRaises(self.desktop.requests.ConnectionError):
            self._worker("lost_ack")
        self.assertEqual(desktop_conn.execute(
            "SELECT COUNT(*) FROM object_sync_outbox WHERE operation_id = ?",
            (object_delete["operation_id"],),
        ).fetchone()[0], 1)
        self.assertEqual(desktop_conn.execute(
            "SELECT COUNT(*) FROM employee_sync_outbox WHERE operation_id = ?",
            (employee_delete,),
        ).fetchone()[0], 1)
        inactive_combo = self.desktop.QComboBox()
        self.desktop.SmartReportApp.load_objects_for_company(
            SimpleNamespace(), "Renatur", inactive_combo, self.desktop.RECORD_TYPE_WORK
        )
        self.assertNotIn(object_name + " renamed", [
            inactive_combo.itemData(index) for index in range(inactive_combo.count())
        ])
        desktop_conn.close()
        self._worker()

        desktop_conn = self.desktop.get_db_connection()
        desktop_conn.execute("UPDATE objects SET name = ?, rate = 60, is_active = 1 WHERE id = ?", (object_name + " renamed", object_id))
        self.desktop.enqueue_object_sync_event(desktop_conn.cursor(), object_id, "activate")
        desktop_conn.execute("INSERT INTO employees (name, salary_rate) VALUES (?, 80)", (employee_name,))
        restore_id = self.desktop.enqueue_employee_sync_event(desktop_conn.cursor(), employee_name, "activate")
        self.assertIsNotNone(restore_id)
        restore_payload = desktop_conn.execute(
            "SELECT salary_rate, revision FROM employee_restore_outbox WHERE operation_id = ?",
            (restore_id,),
        ).fetchone()
        self.assertEqual(tuple(restore_payload), (80, 2))
        desktop_conn.commit()
        desktop_conn.close()
        self._worker()

        server_conn = self.server.get_db_connection()
        final_object = server_conn.execute(
            "SELECT name, rate, is_active, sync_version FROM meta_objects WHERE sync_id = ?",
            (object_event["sync_id"],),
        ).fetchone()
        final_employee = server_conn.execute(
            "SELECT salary_rate, is_active FROM meta_employees WHERE id = ?", (profile["id"],)
        ).fetchone()
        final_account = server_conn.execute(
            "SELECT is_active FROM users WHERE employee_id = ?", (profile["id"],)
        ).fetchone()[0]
        server_conn.close()
        self.assertEqual(tuple(final_object), (object_name + " renamed", 60, 1, 4))
        self.assertEqual(tuple(final_employee), (80, 1))
        self.assertEqual(final_account, 1)
        divergent_restore = self._server_post({
            "restored_employees": [{
                "operation_id": "different-rate-same-revision",
                "name": employee_name,
                "employee_key": employee_key,
                "revision": 2,
                "salary_rate": 90,
            }],
        })
        self.assertEqual(divergent_restore.status_code, 400)
        stale_delete = self._server_post({
            "deleted_employees": [{
                "operation_id": "late-delete-old-revision",
                "name": employee_name,
                "employee_key": employee_key,
                "revision": 1,
            }],
        })
        self.assertEqual(stale_delete.status_code, 200)
        server_conn = self.server.get_db_connection()
        unchanged_employee = server_conn.execute(
            "SELECT salary_rate, is_active FROM meta_employees WHERE id = ?", (profile["id"],)
        ).fetchone()
        server_conn.close()
        self.assertEqual(tuple(unchanged_employee), (80, 1))
        desktop_conn = self.desktop.get_db_connection()
        self.assertEqual(desktop_conn.execute(
            "SELECT object_name FROM shifts WHERE comment = 'lifecycle-history'"
        ).fetchone()[0], object_name)
        for table in ("object_sync_outbox", "employee_sync_outbox", "employee_restore_outbox"):
            self.assertEqual(desktop_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        desktop_conn.close()

    def test_equal_revision_with_different_object_payload_is_rejected(self):
        sync_id = str(uuid.uuid4())
        object_data = {
            "name": "Revision Object", "company": "Renatur", "markning": "p-rev",
            "rate": 20, "transport_rate": 1,
        }
        first = {
            "operation_id": "revision-first", "sync_id": sync_id, "revision": 1,
            "operation": "upsert", "object": object_data,
        }
        self.assertEqual(self._server_post({"object_events": [first]}).status_code, 200)
        divergent = {
            **first, "operation_id": "revision-divergent",
            "object": {**object_data, "rate": 99},
        }
        self.assertEqual(self._server_post({"object_events": [divergent]}).status_code, 400)
        conn = self.server.get_db_connection()
        row = conn.execute(
            "SELECT rate, is_active, sync_version FROM meta_objects WHERE sync_id = ?", (sync_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(tuple(row), (20, 1, 1))

    def test_catalog_revision_conflict_returns_specific_reason(self):
        sync_id = str(uuid.uuid4())
        server_conn = self.server.get_db_connection()
        server_conn.execute("""
            INSERT INTO meta_objects (
                sync_id, name, markning, company, rate, transport_rate, is_active, sync_version
            ) VALUES (?, 'Stable Object', 'p-revision', 'Renatur', 100, 10, 1, 2)
        """, (sync_id,))
        server_conn.commit()
        server_conn.close()
        desktop_conn = self.desktop.get_db_connection()
        desktop_conn.execute("""
            INSERT INTO objects (
                name, markning, company, rate, transport_rate, sync_id, server_id, is_active, sync_version
            ) VALUES ('Stable Object', 'p-revision', 'Renatur', 100, 10, ?, 9001, 1, 3)
        """, (sync_id,))
        desktop_conn.commit()
        desktop_conn.close()

        result = self._worker()
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(result["conflict_details"], "серверная revision ниже локальной: 1")


if __name__ == "__main__":
    unittest.main()