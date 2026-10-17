import os
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
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
            "gross_amount": "125.00",
            "vat_amount": "25.00",
            "verification_status": "requires_review",
            "allocations": [{
                "object_id": None, "gross_amount": "50.00", "vat_amount": "10.00",
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
                window.load_expense_document_filters()
            self.assertEqual(window.exp_filter_object.currentData(), object_id)
            self.assertEqual(window.object_expenses_object.currentData(), object_id)
            self.assertIn("Stable expense object renamed", window.object_expenses_object.currentText())
        finally:
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
            self.assertEqual([row[1] for row in report['rows']], [5, 1])
            self.assertEqual([row[3] for row in report['rows']], [2, 1])
            self.assertEqual(report['rows'][0][4], 1)
            self.assertAlmostEqual(report['rows'][0][5], result['total_amount'])
            summary = {
                window.invoice_summary_table.item(index, 0).text():
                window.invoice_summary_table.item(index, 1).text()
                for index in range(window.invoice_summary_table.rowCount())
            }
            self.assertEqual(summary['Totalt att fakturera exkl. moms'], '3,450.00 kr')
            self.assertEqual(summary['OB-tillägg'], '800.00 kr (segmentbelopp)')
            self.assertEqual(report['totals']['invoice_total'], 3450)
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
                self.assertEqual(sum(pdf_table._colWidths), 535)
                for row_index in range(window.table_invoice.rowCount()):
                    pdf_row = pdf_table._cellvalues[row_index + 1]
                    self.assertEqual(pdf_row[0].getPlainText(), window.table_invoice.item(row_index, 0).text())
                    self.assertEqual(pdf_row[1:6], [window.table_invoice.item(row_index, column).text() for column in range(1, 6)])
                    self.assertEqual(pdf_row[6:8], [window.table_invoice.item(row_index, column).text() for column in (6, 7)])
                    self.assertEqual(pdf_row[8].getPlainText(), window.table_invoice.item(row_index, 8).text())
                    self.assertEqual(pdf_row[9].getPlainText(), window.table_invoice.item(row_index, 9).text())
                summary_group = story[-1]
                pdf_summary = next(
                    flow for flow in summary_group._content
                    if isinstance(flow, self.desktop.Table)
                )
                screen_summary = [
                    [window.invoice_summary_table.item(row, column).text() for column in range(2)]
                    for row in range(window.invoice_summary_table.rowCount())
                ]
                pdf_summary_text = [
                    [cell.getPlainText() for cell in row]
                    for row in pdf_summary._cellvalues
                ]
                self.assertEqual(pdf_summary_text, screen_summary)
                self.assertEqual(
                    summary_group._content[-1].getPlainText(),
                    window.invoice_ob_rules_lbl.text(),
                )

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
                    window._invoice_report['totals']['invoice_total'], 2530
                )
                window.inv_obj_cb.setCurrentIndex(window.inv_obj_cb.findData(identities['Invoice work']))
                window.inv_date_from.setDate(self.desktop.QDate(2026, 10, 31))
                window.export_invoice_to_pdf()
                check_pdf(build, 'Invoice work')
                self.assertEqual([row[1] for row in window._invoice_report['rows']], [3, 1])
                self.assertEqual([row[3] for row in window._invoice_report['rows']], [1, 1])
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
                self.assertIn('Varav OB1 (18:00–20:00)', pdf_text)
                self.assertIn('Varav OB2 (20:00–24:00)', pdf_text)
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
            self.assertEqual(report['totals']['hours'], 17)
            self.assertEqual(report['totals']['ob_categories']['weekday_early_evening'], 4)
            self.assertEqual(report['totals']['ob_categories']['weekday_late_evening_night'], 1)
            self.assertEqual(report['totals']['work_amount'], 3400)
            self.assertEqual(report['totals']['transport_amount'], 150)
            self.assertEqual(report['totals']['invoice_total'], 3550)
            self.assertEqual(report['totals']['ob_amount'], 920)
            self.assertNotEqual(report['totals']['invoice_total'], 3550 + 770)
            self.assertEqual(window.table_invoice.columnCount(), 10)
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
            self.assertEqual(summary['Arbetade timmar totalt'], '17.0 tim')
            self.assertEqual(summary['Varav utan OB'], 'Ej fastställbart')
            self.assertEqual(summary['Varav OB1 (18:00–20:00)'], '4.0 tim')
            self.assertEqual(summary['Varav OB2 (20:00–24:00)'], '1.0 tim')
            self.assertIn('möjlig överlappning kan inte fastställas', window.invoice_summary_note.text())
            self.assertIn('inte en verifierad premie', window.invoice_summary_note.text())
            self.assertIn('ingår inte i totalsumman', window.invoice_summary_note.text())
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
                dialog.findChild(self.desktop.QLineEdit, "expenseSupplier").setText("Corrected supplier")
                dialog.findChild(self.desktop.QLineEdit, "expenseGrossInput").setText("125.00")
                dialog.findChild(self.desktop.QLineEdit, "expenseVatInput").setText("25.00")
                dialog.accept()
                return self.desktop.QDialog.DialogCode.Accepted

            with patch.object(self.desktop.QFileDialog, "getOpenFileNames", return_value=(list(map(str, sources)), "")), \
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
        gallery = self.desktop.DocumentGalleryDialog(photo_paths, "Photo bundle")
        try:
            self.assertEqual(len(gallery._pages), 2)
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