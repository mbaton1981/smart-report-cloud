import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

SYNC_KEY = "isolated-sync-test-key"


class AuthLoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        keys = ("DATABASE_PATH", "ADMIN_PASSWORD", "SECRET_KEY", "SYNC_API_KEY", "SMARTREPORT_DB_FILE",
                "SMARTREPORT_DOCUMENTS_DIR", "SMARTREPORT_MAIL_SETTINGS_FILE", "SMARTREPORT_CLOUD_URL",
                "SMARTREPORT_TRIAL_MODE", "SMARTREPORT_DATA_DIR")
        cls._previous = {key: os.environ.get(key) for key in keys}
        cls._bootstrap = tempfile.TemporaryDirectory(prefix="smartreport-auth-import-")
        root = Path(cls._bootstrap.name)
        os.environ.update({
            "DATABASE_PATH": str(root / "server.db"), "ADMIN_PASSWORD": "isolated-test-only",
            "SECRET_KEY": "isolated-test-only", "SYNC_API_KEY": SYNC_KEY,
            "SMARTREPORT_DB_FILE": str(root / "desktop.db"), "SMARTREPORT_DOCUMENTS_DIR": str(root / "documents"),
            "SMARTREPORT_MAIL_SETTINGS_FILE": str(root / "mail.json"),
            "SMARTREPORT_CLOUD_URL": "http://isolated-testserver", "SMARTREPORT_TRIAL_MODE": "1",
        })
        import server
        import desktop_auth
        cls.server = server
        cls.desktop_auth = desktop_auth
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.qt_app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls._bootstrap.cleanup()
        for key, value in cls._previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def setUp(self):
        self._case = tempfile.TemporaryDirectory(prefix="smartreport-auth-test-")
        self.server.DB_FILE = str(Path(self._case.name) / "server.db")
        self.server.init_cloud_db()
        self.server.auth_throttle = self.server.LoginThrottle()
        self.client = self.server.app.test_client()
        self.addCleanup(self._case.cleanup)
        self.addCleanup(self.desktop_auth.set_token, "", False)

    def _login(self, username="admin", password="isolated-test-only"):
        return self.client.post("/api/auth/token", json={"username": username, "password": password, "device_name": "pc"})

    def _token(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        return response.get_json()["token"]

    def _bearer(self, token):
        return {"Authorization": f"Bearer {token}"}

    def _query(self, sql, args=()):
        connection = sqlite3.connect(self.server.DB_FILE)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    def test_admin_login_issues_token_stored_only_as_hash(self):
        token = self._token()
        rows = self._query("SELECT token_hash, revoked FROM api_tokens")
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0][0], token)
        self.assertNotIn(token, rows[0][0])
        me = self.client.get("/api/auth/me", headers=self._bearer(token))
        self.assertEqual(me.get_json()["user"], {"username": "admin", "role": "admin"})

    def test_allocation_vat_saved_and_object_report_uses_mixed_item_rates(self):
        from io import BytesIO
        import uuid
        from reportlab.pdfgen import canvas
        from expense_documents import migrate_invoice_net_schema

        connection = self.server.get_db_connection()
        try:
            migrate_invoice_net_schema(connection)
        finally:
            connection.close()
        source = BytesIO()
        pdf = canvas.Canvas(source)
        for text, left, top in [
            ("Order nr: A-1", 40, 740), ("Rad", 40, 710), ("Moms %", 250, 710), ("Summa", 400, 710),
            ("10", 40, 690), ("25,00", 250, 690), ("100,00", 400, 690),
            ("20", 40, 670), ("12,00", 250, 670), ("50,00", 400, 670),
            ("Summa order", 40, 650), ("150,00", 400, 650), ("Order nr: B-1", 40, 620),
            ("10", 40, 600), ("0,00", 250, 600), ("100,00", 400, 600),
            ("20", 40, 580), ("6,00", 250, 580), ("33,33", 400, 580),
            ("Summa order", 40, 560), ("133,33", 400, 560),
        ]:
            pdf.drawString(left, top, text)
        pdf.save()
        headers = self._bearer(self._token())
        parts = [{"order_id": "A-1", "net_amount": "150.00", "destination": "object", "object_id": 1},
                 {"order_id": "B-1", "net_amount": "133.33", "destination": "company", "object_id": None}]
        payload = {"client_id": str(uuid.uuid4()), "supplier_name": "Mixed tax fixture AB", "invoice_number": "MIX-1",
                   "document_date": "2026-10-19", "currency_code": "SEK", "allocation_basis": "net",
                   "net_amount": "283.33", "vat_amount": "33.00", "amount_due": "316.33",
                   "verification_status": "confirmed", "allocations": parts, "order_proposals": parts}
        created = self.client.post("/api/expense-documents", headers=headers,
                                   data={"payload": json.dumps(payload), "original": (BytesIO(source.getvalue()), "mixed.pdf")})
        self.assertEqual(created.status_code, 201, created.get_json())
        identifier = created.get_json()["document"]["id"]
        response = self.client.post(f"/api/expense-documents/{identifier}/allocation-vat", headers=headers,
                                    json={"expected_version": 1})
        self.assertEqual(response.status_code, 200, response.get_json())
        document = response.get_json()["document"]
        self.assertEqual(document["allocated_vat"], "33.00")
        self.assertEqual(document["allocated_gross"], "316.33")
        self.assertEqual(document["net_amount"], payload["net_amount"])
        report = self.client.get("/api/expense-objects/1/expenses", headers=headers).get_json()
        self.assertEqual(report["documents"][0]["object_vat"], "31.00")
        self.assertEqual(report["documents"][0]["object_gross"], "181.00")
        self.assertEqual(self.client.post(f"/api/expense-documents/{identifier}/allocation-vat", headers=headers,
                                          json={"expected_version": 1}).status_code, 409)
        self.assertEqual(self.client.post(f"/api/expense-documents/{identifier}/allocation-vat", headers=headers,
                                          json={"expected_version": 2}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/expense-documents/{identifier}/allocation-vat",
                                          json={"expected_version": 2}).status_code, 403)

    def test_deleted_original_restores_same_record_and_retries_without_duplicate(self):
        from io import BytesIO
        import uuid
        from expense_documents import migrate_invoice_net_schema
        connection = self.server.get_db_connection()
        try:
            migrate_invoice_net_schema(connection)
        finally:
            connection.close()
        headers = self._bearer(self._token())
        original = self._invoice_pdf(6001)
        initial = {"client_id": str(uuid.uuid4()), "verification_status": "draft"}
        created = self.client.post("/api/expense-documents", headers=headers, data={
            "payload": json.dumps(initial), "original": (BytesIO(original), "receipt.pdf")})
        identifier = created.get_json()["document"]["id"]
        self.assertEqual(self.client.delete(f"/api/expense-documents/{identifier}", headers=headers,
                                           json={"expected_version": 1}).status_code, 200)
        self.assertEqual(self.client.get(f"/api/expense-documents/{identifier}", headers=headers).status_code, 404)
        files_before = self._query("SELECT id,document_id,stored_name,file_sha256 FROM expense_document_attachments")
        pending_id = str(uuid.uuid4())
        duplicate = self.client.post("/api/expense-documents", headers=headers, data={
            "payload": json.dumps({"client_id": pending_id, "verification_status": "draft"}),
            "original": (BytesIO(original), "receipt-again.pdf")})
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.get_json()["code"], "deleted_original")
        self.assertEqual(duplicate.get_json()["existing_document_id"], identifier)
        self.assertEqual(self._query("SELECT COUNT(*) FROM expense_documents")[0][0], 1)
        payload = {**initial, "restore_deleted": True, "restore_operation_id": pending_id, "expected_version": 2,
                   "supplier_name": "ELWO Tools AB", "document_date": "2026-10-16", "currency_code": "SEK",
                   "allocation_basis": "net", "net_amount": "10209.00", "vat_amount": "2552.25", "gross_amount": "12761.25",
                   "amount_due": None, "due_date": None, "due_date_kind": "absent", "allocations": []}

        def restore(content=original, values=None):
            return self.client.put(f"/api/expense-documents/{identifier}", headers=headers, data={
                "payload": json.dumps(payload if values is None else values), "originals": (BytesIO(content), "receipt-again.pdf")})

        self.assertEqual(restore(b"different original").status_code, 400)
        self.assertEqual(restore(values={**payload, "expected_version": 1}).status_code, 409)
        self.assertEqual(self.client.get(f"/api/expense-documents/{identifier}", headers=headers).status_code, 404)
        response = restore()
        self.assertEqual(response.status_code, 200, response.get_json())
        saved = response.get_json()["document"]
        self.assertEqual((saved["id"], saved["client_id"], saved["sync_version"]), (identifier, initial["client_id"], 3))
        self.assertEqual((saved["document_date"], saved["net_amount"], saved["vat_amount"], saved["gross_amount"]),
                         ("2026-10-16", "10209.00", "2552.25", "12761.25"))
        self.assertTrue(saved["has_original"])
        retry = restore()
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.get_json()["duplicate_request"])
        self.assertEqual(retry.get_json()["document"]["sync_version"], 3)
        self.assertEqual(files_before, self._query("SELECT id,document_id,stored_name,file_sha256 FROM expense_document_attachments"))
        self.assertEqual(self._query("SELECT COUNT(*) FROM expense_documents")[0][0], 1)
        saved_again = self.client.get(f"/api/expense-documents/{identifier}", headers=headers).get_json()["document"]
        self.assertEqual(saved_again["gross_amount"], "12761.25")

    def test_invoice_date_filter_keeps_undated_documents_visible(self):
        connection = sqlite3.connect(self.server.DB_FILE)
        try:
            for identifier, date in (("undated", None), ("empty-date", ""),
                                     ("in-period", "2026-10-19"), ("outside", "2026-09-01")):
                connection.execute(
                    "INSERT INTO expense_documents (client_id, document_date, created_at, updated_at) VALUES (?, ?, 'x', 'x')",
                    (identifier, date),
                )
            connection.commit()
        finally:
            connection.close()
        response = self.client.get(
            "/api/expense-documents?date_from=2026-10-01&date_to=2026-10-31",
            headers=self._bearer(self._token()),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row["client_id"] for row in response.get_json()["documents"]},
                         {"undated", "empty-date", "in-period"})

    def _invoice_pdf(self, number):
        from io import BytesIO
        from reportlab.pdfgen import canvas
        output = BytesIO()
        pdf = canvas.Canvas(output)
        for index, text in enumerate([
            "Test Supplier AB", "Org.nr. 556012-5220", f"Fakturanummer: {number}",
            "Fakturadatum: 2026-10-19", "Valuta: SEK", "Exkl. moms: 100,00",
            "Moms: 25,00", "Att betala: 125,00",
        ]):
            pdf.drawString(40, 780 - index * 24, text)
        pdf.save()
        return output.getvalue()

    def test_eleven_invoice_uploads_recognize_persist_and_duplicate_points_to_existing(self):
        from io import BytesIO
        import uuid
        headers = self._bearer(self._token())
        for index in range(11):
            content = self._invoice_pdf(3000 + index)
            saved = self.client.post("/api/expense-documents", headers=headers, data={
                "payload": json.dumps({"client_id": str(uuid.uuid4()), "verification_status": "draft"}),
                "original": (BytesIO(content), f"invoice-{index}.pdf"),
            })
            self.assertEqual(saved.status_code, 201)
            identifier = saved.get_json()["document"]["id"]
            recognized = self.client.post(f"/api/expense-documents/{identifier}/recognize", headers=headers,
                                          json={"expected_version": 1})
            self.assertEqual(recognized.status_code, 200, recognized.get_json())
            document = recognized.get_json()["document"]
            self.assertEqual(document["invoice_number"], str(3000 + index))
            self.assertEqual(document["net_amount"], "100.00")
            self.assertTrue(document["recognized_text"])
            self.assertEqual(document["processing"]["state"], "requires_review")
            self.assertTrue(document["processing"]["field_sources"]["vat_amount"])
            self.assertEqual(self.client.post(f"/api/expense-documents/{identifier}/recognize", headers=headers,
                                             json={"expected_version": 1}).status_code, 409)
            duplicate = self.client.post("/api/expense-documents", headers=headers, data={
                "payload": json.dumps({"client_id": str(uuid.uuid4()), "verification_status": "draft"}),
                "original": (BytesIO(content), "renamed.pdf"),
            })
            self.assertEqual(duplicate.status_code, 409)
            self.assertEqual(duplicate.get_json()["existing_document_id"], identifier)
        listing = self.client.get("/api/expense-documents?date_from=2026-10-01&date_to=2026-10-31", headers=headers).get_json()
        self.assertEqual(len(listing["documents"]), 11)
        self.assertTrue(all(row["invoice_number"] and row["processing"] for row in listing["documents"]))

    def test_split_legacy_eleven_file_package_is_idempotent_and_keeps_originals(self):
        from io import BytesIO
        import uuid
        from expense_documents import migrate_invoice_net_schema
        connection = sqlite3.connect(self.server.DB_FILE)
        try:
            migrate_invoice_net_schema(connection)
        finally:
            connection.close()
        headers = self._bearer(self._token())
        saved = self.client.post("/api/expense-documents", headers=headers, data={
            "payload": json.dumps({"client_id": str(uuid.uuid4()), "verification_status": "draft"}),
            "originals": [(BytesIO(self._invoice_pdf(4000 + index)), f"old-{index}.pdf") for index in range(11)],
        })
        self.assertEqual(saved.status_code, 201)
        identifier = saved.get_json()["document"]["id"]
        connection = sqlite3.connect(self.server.DB_FILE)
        try:
            connection.execute("INSERT INTO expense_allocations (document_id,object_id,allocated_gross) VALUES (?,NULL,NULL)", (identifier,))
            connection.commit()
        finally:
            connection.close()
        before = self._query("SELECT id,stored_name,file_sha256 FROM expense_document_attachments ORDER BY id")
        connection = sqlite3.connect(self.server.DB_FILE)
        try:
            connection.execute("UPDATE expense_allocations SET allocated_net='1.00' WHERE document_id=?", (identifier,))
            connection.commit()
            blocked = self.client.post(f"/api/expense-documents/{identifier}/split", headers=headers, json={"expected_version": 1})
            self.assertEqual(blocked.status_code, 409)
            connection.execute("UPDATE expense_allocations SET allocated_net=NULL WHERE document_id=?", (identifier,))
            connection.commit()
        finally:
            connection.close()
        response = self.client.post(f"/api/expense-documents/{identifier}/split", headers=headers, json={"expected_version": 1})
        self.assertEqual(response.status_code, 200)
        children = response.get_json()["children"]
        self.assertEqual(len(children), 11)
        retry = self.client.post(f"/api/expense-documents/{identifier}/split", headers=headers, json={"expected_version": 1})
        self.assertEqual(retry.get_json()["children"], children)
        self.assertEqual(before, self._query("SELECT id,stored_name,file_sha256 FROM expense_document_attachments ORDER BY id"))
        self.assertEqual(self._query("SELECT COUNT(*) FROM expense_allocations WHERE document_id=?", (identifier,))[0][0], 1)
        for child in children:
            result = self.client.post(f"/api/expense-documents/{child}/recognize", headers=headers, json={"expected_version": 1})
            self.assertEqual(result.status_code, 200)
            self.assertTrue(result.get_json()["document"]["invoice_number"])
        self.assertEqual(self._query("SELECT COUNT(*) FROM expense_documents")[0][0], 12)
        listing = self.client.get("/api/expense-documents", headers=headers).get_json()["documents"]
        self.assertEqual({row["id"] for row in listing}, set(children))
        retained = self.client.get(f"/api/expense-documents/{identifier}", headers=headers).get_json()["document"]
        self.assertFalse(retained["is_deleted"])
        self.assertEqual(retained["processing"]["children"], children)

    def test_failed_recognition_is_visible_and_retry_preserves_one_record(self):
        from io import BytesIO
        import uuid
        headers = self._bearer(self._token())
        saved = self.client.post("/api/expense-documents", headers=headers, data={
            "payload": json.dumps({"client_id": str(uuid.uuid4()), "verification_status": "draft"}),
            "original": (BytesIO(self._invoice_pdf(5000)), "retry.pdf"),
        })
        identifier = saved.get_json()["document"]["id"]
        failed = {"state": "failed", "fields": {}, "methods": [], "errors": ["test extraction failure"], "missing_fields": ["net_amount"]}
        with patch("invoice_processing.recognize_invoice", return_value=failed):
            result = self.client.post(f"/api/expense-documents/{identifier}/recognize", headers=headers, json={"expected_version": 1})
        self.assertEqual(result.get_json()["document"]["processing"]["errors"], failed["errors"])
        result = self.client.post(f"/api/expense-documents/{identifier}/recognize", headers=headers, json={"expected_version": 2})
        self.assertEqual(result.get_json()["document"]["invoice_number"], "5000")
        self.assertEqual(self._query("SELECT COUNT(*) FROM expense_documents")[0][0], 1)

    def test_wrong_password_and_unknown_user_share_one_error(self):
        wrong = self._login(password="wrong-password")
        unknown = self._login(username="nobody")
        self.assertEqual((wrong.status_code, unknown.status_code), (401, 401))
        self.assertEqual(wrong.get_json()["error"], unknown.get_json()["error"])
        self.assertEqual(self._query("SELECT COUNT(*) FROM api_tokens")[0][0], 0)

    def test_non_admin_and_passwordless_users_cannot_get_a_token(self):
        connection = sqlite3.connect(self.server.DB_FILE)
        connection.execute("INSERT INTO users (username, password_hash, role, is_active, created_at) VALUES ('worker', ?, 'user', 1, 'x')",
                           (self.server.generate_password_hash("worker-pass"),))
        connection.execute("INSERT INTO users (username, role, is_active, created_at) VALUES ('fresh', 'user', 1, 'x')")
        connection.commit()
        connection.close()
        self.assertEqual(self._login("worker", "worker-pass").status_code, 401)
        self.assertEqual(self._login("fresh", "anything").status_code, 401)
        self.assertIsNone(self._query("SELECT password_hash FROM users WHERE username='fresh'")[0][0])

    def test_token_authorizes_api_but_missing_or_bad_token_does_not(self):
        token = self._token()
        self.assertEqual(self.client.get("/api/expense-objects").status_code, 403)
        self.assertEqual(self.client.get("/api/expense-objects", headers=self._bearer("not-a-token")).status_code, 403)
        self.assertEqual(self.client.get("/api/expense-objects", headers=self._bearer(token)).status_code, 200)
        self.assertEqual(self.client.get("/api/expense-objects", headers={"X-Sync-Key": SYNC_KEY}).status_code, 200)
        self.assertEqual(self.client.get("/admin/get-users-status", headers=self._bearer(token)).status_code, 200)
        self.assertEqual(self.client.get("/admin/get-users-status").status_code, 403)

    def test_logout_expiry_and_deactivation_stop_access(self):
        token = self._token()
        self.assertTrue(self.client.post("/api/auth/logout", headers=self._bearer(token)).get_json()["revoked"])
        self.assertEqual(self.client.get("/api/expense-objects", headers=self._bearer(token)).status_code, 403)
        token = self._token()
        connection = sqlite3.connect(self.server.DB_FILE)
        connection.execute("UPDATE api_tokens SET expires_at='2000-01-01 00:00:00' WHERE revoked=0")
        connection.commit()
        connection.close()
        self.assertEqual(self.client.get("/api/auth/me", headers=self._bearer(token)).status_code, 401)
        token = self._token()
        connection = sqlite3.connect(self.server.DB_FILE)
        connection.execute("UPDATE users SET is_active=0 WHERE username='admin'")
        connection.commit()
        connection.close()
        self.assertEqual(self.client.get("/api/expense-objects", headers=self._bearer(token)).status_code, 403)

    def test_repeated_failures_are_throttled_even_for_correct_password(self):
        for _ in range(8):
            self.assertEqual(self._login(password="bad").status_code, 401)
        self.assertEqual(self._login().status_code, 429)
        self.assertEqual(self._query("SELECT COUNT(*) FROM api_tokens")[0][0], 0)

    def test_desktop_login_maps_server_and_network_outcomes(self):
        auth = self.desktop_auth

        class Reply:
            def __init__(self, status, payload):
                self.status_code, self._payload = status, payload

            def json(self):
                return self._payload

        with patch.object(auth.requests, "post", return_value=Reply(200, {"ok": True, "token": "abc"})):
            self.assertEqual(auth.login("https://x", "admin", "pw"), "abc")
        with patch.object(auth.requests, "post", return_value=Reply(401, {"ok": False, "error": "Неверный логин или пароль."})):
            with self.assertRaisesRegex(auth.AuthError, "Неверный"):
                auth.login("https://x", "admin", "bad")
        with patch.object(auth.requests, "post", side_effect=requests.ConnectionError("down")):
            with self.assertRaisesRegex(auth.AuthError, "связаться"):
                auth.login("https://x", "admin", "pw")
        with patch.object(auth.requests, "get", return_value=Reply(401, {})):
            self.assertIs(auth.check_token("https://x", "t"), False)
        with patch.object(auth.requests, "get", return_value=Reply(200, {})):
            self.assertIs(auth.check_token("https://x", "t"), True)
        with patch.object(auth.requests, "get", side_effect=requests.ConnectionError("down")):
            self.assertIsNone(auth.check_token("https://x", "t"))

    @unittest.skipUnless(os.name == "nt", "DPAPI доступен только в Windows")
    def test_saved_token_is_encrypted_and_survives_restart(self):
        auth = self.desktop_auth
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {"SMARTREPORT_DATA_DIR": data}):
            auth.set_token("secret-token-value")
            raw = (Path(data) / "session.dat").read_bytes()
            self.assertNotIn(b"secret-token-value", raw)
            auth.set_token("", persist=False)
            self.assertEqual(auth.load_saved_token(), "secret-token-value")
            auth.set_token("")
            self.assertEqual(auth.load_saved_token(), "")
            self.assertFalse((Path(data) / "session.dat").exists())

    def test_storage_status_reports_missing_and_corrupted_attachments_and_backup_is_consistent(self):
        import hashlib
        root = Path(self._case.name) / "documents"
        root.mkdir()
        good, bad = b"%PDF good", b"%PDF changed"
        (root / "good.pdf").write_bytes(good)
        (root / "bad.pdf").write_bytes(bad)
        connection = sqlite3.connect(self.server.DB_FILE)
        connection.execute("INSERT INTO expense_documents (id, client_id, created_at, updated_at) VALUES (1, 'c1', 'x', 'x')")
        for name, digest in (("good.pdf", hashlib.sha256(good).hexdigest()), ("bad.pdf", hashlib.sha256(b"other").hexdigest()),
                             ("gone.pdf", "0" * 64)):
            connection.execute("INSERT INTO expense_document_attachments (document_id, original_name, stored_name, file_sha256, created_at) VALUES (1, ?, ?, ?, 'x')",
                               (name, name, digest))
        connection.commit()
        connection.close()
        token = self._token()
        self.assertEqual(self.client.get("/api/admin/storage-status").status_code, 403)
        with patch.dict(os.environ, {"SMARTREPORT_DOCUMENTS_DIR": str(root)}):
            status = self.client.get("/api/admin/storage-status?verify_hashes=1", headers=self._bearer(token)).get_json()
            self.assertEqual(self.client.get("/api/admin/backup/database").status_code, 403)
            backup = self.client.get("/api/admin/backup/database", headers=self._bearer(token))
            self.assertEqual(backup.status_code, 200)
            snapshot = Path(self._case.name) / "pulled.db"
            snapshot.write_bytes(backup.data)
            backup.close()
        documents = status["documents"]
        self.assertEqual((documents["attachments"], documents["missing_files"], documents["hash_mismatches"]), (3, 1, 1))
        self.assertTrue(documents["same_filesystem_as_database"])
        self.assertGreaterEqual(status["backups"]["count"], 0)
        copy = sqlite3.connect(snapshot)
        try:
            self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM expense_document_attachments").fetchone()[0], 3)
        finally:
            copy.close()

    def test_backup_client_downloads_original_over_http_with_matching_dataset(self):
        from contextlib import closing
        import hashlib
        from threading import Thread
        from werkzeug.serving import make_server
        import server_backup

        root = Path(self._case.name) / "originals"
        root.mkdir()
        original = b"%PDF isolated backup integration fixture"
        (root / "stored.pdf").write_bytes(original)
        with closing(sqlite3.connect(self.server.DB_FILE)) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS sync_dataset(id INTEGER PRIMARY KEY, dataset_id TEXT);
                INSERT OR REPLACE INTO sync_dataset VALUES(1,'backup-integration');
                INSERT INTO expense_documents(id,client_id,created_at,updated_at)
                    VALUES(1,'backup-integration-document','x','x');
            """)
            connection.execute("""
                INSERT INTO expense_document_attachments
                    (document_id,original_name,stored_name,file_sha256,created_at)
                VALUES(1,'invoice.pdf','stored.pdf',?,'x')
            """, (hashlib.sha256(original).hexdigest(),))
            connection.commit()
        token = self._token()
        http_server = make_server("127.0.0.1", 0, self.server.app)
        thread = Thread(target=http_server.serve_forever)
        with patch.dict(os.environ, {"SMARTREPORT_DOCUMENTS_DIR": str(root)}):
            thread.start()
            try:
                target, manifest = server_backup.pull_backup(
                    f"http://127.0.0.1:{http_server.server_port}", token,
                    Path(self._case.name) / "backup", expected_dataset="backup-integration",
                )
                self.assertEqual(manifest["status"], "complete")
                self.assertEqual(manifest["problems"], [])
                self.assertEqual(len(manifest["files"]), 1)
                self.assertEqual((target / "documents" / manifest["files"][0]["file"]).read_bytes(), original)
                with closing(sqlite3.connect(target / "server_snapshot.db")) as snapshot:
                    self.assertEqual(snapshot.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(snapshot.execute("SELECT COUNT(*) FROM expense_document_attachments").fetchone()[0], 1)
            finally:
                http_server.shutdown()
                thread.join(timeout=5)
                http_server.server_close()
                self.assertFalse(thread.is_alive())

    def test_login_dialog_rejects_bad_input_shows_server_error_and_returns_token(self):
        import desktop_login
        auth = self.desktop_auth
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {"SMARTREPORT_DATA_DIR": data}):
            dialog = desktop_login.LoginDialog("https://server.example", "")
            dialog.submit()
            self.assertIn("логин", dialog.status.text())
            dialog.server_input.setText("http://insecure.example")
            dialog.user_input.setText("admin")
            dialog.password_input.setText("pw")
            dialog.submit()
            self.assertIn("https://", dialog.status.text())
            dialog.server_input.setText("https://server.example")
            with patch.object(auth, "login", side_effect=auth.AuthError("Неверный логин или пароль.")):
                dialog.submit()
            self.assertEqual(dialog.status.text(), "Неверный логин или пароль.")
            self.assertEqual(dialog.password_input.text(), "")
            self.assertTrue(dialog.login_button.isEnabled())
            dialog.password_input.setText("pw")
            with patch.object(auth, "login", return_value="tok"):
                dialog.submit()
            self.assertEqual(dialog.token, "tok")
            self.assertEqual(auth.load_settings()["username"], "admin")
            self.assertNotIn("pw", json.dumps(auth.load_settings()))

    def test_ensure_login_keeps_offline_saved_token_and_drops_rejected_one(self):
        import desktop_login
        auth = self.desktop_auth
        env = {"SYNC_API_KEY": "", "SMARTREPORT_TRIAL_MODE": "0", "SMARTREPORT_CLOUD_URL": "https://server.example"}
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, dict(env, SMARTREPORT_DATA_DIR=data)):
            auth.set_token("saved")
            with patch.object(auth, "check_token", return_value=None):
                self.assertTrue(desktop_login.ensure_login())
                self.assertEqual(auth.current_token(), "saved")
            auth.set_token("", persist=False)
            with patch.object(auth, "check_token", return_value=False), patch.object(desktop_login.LoginDialog, "exec", return_value=0):
                self.assertFalse(desktop_login.ensure_login())
            self.assertEqual(auth.load_saved_token(), "")

    def test_server_url_prefers_environment_then_saved_setting_then_default(self):
        auth = self.desktop_auth
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {"SMARTREPORT_DATA_DIR": data}):
            with patch.dict(os.environ, {"SMARTREPORT_CLOUD_URL": ""}):
                self.assertEqual(auth.server_url(), auth.DEFAULT_SERVER_URL)
                auth.save_settings(server_url="https://saved.example/")
                self.assertEqual(auth.server_url(), "https://saved.example")
            with patch.dict(os.environ, {"SMARTREPORT_CLOUD_URL": "http://127.0.0.1:5999"}):
                self.assertEqual(auth.server_url(), "http://127.0.0.1:5999")

    def test_unset_employee_pin_cannot_be_claimed_at_login_and_admin_can_provision(self):
        from werkzeug.security import check_password_hash
        connection = sqlite3.connect(self.server.DB_FILE)
        cursor = connection.execute(
            "INSERT INTO meta_employees(name,salary_rate,is_active) VALUES ('Unprovisioned audit employee',200,1)"
        )
        employee_id = cursor.lastrowid
        cursor = connection.execute("""
            INSERT INTO users(username,password_hash,role,employee_id,is_active,created_at)
            VALUES('Unprovisioned audit employee',NULL,'user',?,1,'isolated')
        """, (employee_id,))
        user_id = cursor.lastrowid
        connection.commit()
        before = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        connection.close()
        for _ in range(2):
            response = self.client.post("/login", json={
                "username": "Unprovisioned audit employee", "password": "self-chosen-test-pin",
            })
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.get_json()["code"], "pin_not_provisioned")
            self.assertIn("администратор", response.get_json()["error"].lower())
        self.assertEqual(self._query("SELECT * FROM users WHERE id=?", (user_id,))[0], before)
        connection = sqlite3.connect(self.server.DB_FILE)
        connection.execute("UPDATE users SET password_hash='' WHERE id=?", (user_id,))
        connection.commit()
        connection.close()
        response = self.client.post("/login", json={
            "username": "Unprovisioned audit employee", "password": "self-chosen-test-pin",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "pin_not_provisioned")
        self.assertEqual(self._query("SELECT password_hash FROM users WHERE id=?", (user_id,))[0][0], "")
        self.assertEqual(self.client.get("/me").status_code, 401)
        self.assertEqual(self.client.post("/admin/set-user-pin", json={
            "username": "Unprovisioned audit employee", "pin": "admin-issued-test-pin",
        }).status_code, 403)
        provisioned = self.client.post("/admin/set-user-pin", headers=self._bearer(self._token()), json={
            "username": "Unprovisioned audit employee", "pin": "admin-issued-test-pin",
        })
        self.assertEqual(provisioned.status_code, 200)
        stored = self._query("SELECT password_hash FROM users WHERE id=?", (user_id,))[0][0]
        self.assertTrue(check_password_hash(stored, "admin-issued-test-pin"))
        response = self.client.post("/login", json={
            "username": "Unprovisioned audit employee", "password": "admin-issued-test-pin",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/me").get_json()["user"]["employee_id"], employee_id)
        self.assertEqual(self._query("SELECT password_hash FROM users WHERE id=?", (user_id,))[0][0], stored)


if __name__ == "__main__":
    unittest.main()
