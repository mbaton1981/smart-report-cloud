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


if __name__ == "__main__":
    unittest.main()
