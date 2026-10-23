import unittest
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from flask import Flask, jsonify, request, session

import test_auth_login as auth


class WebSessionSecurityTests(unittest.TestCase):
    setUpClass = classmethod(auth.AuthLoginTests.setUpClass.__func__)
    tearDownClass = classmethod(auth.AuthLoginTests.tearDownClass.__func__)
    _query = auth.AuthLoginTests._query

    def setUp(self):
        auth.AuthLoginTests.setUp(self)
        self.server.web_login_throttle = self.server.SQLiteLoginThrottle(self.server.get_db_connection)

    def security_client(self, environment):
        app = Flask(__name__)
        app.secret_key = "isolated-cookie-test-only"
        self.server.configure_session_security(app, environment)

        @app.route("/")
        def index():
            session["test"] = True
            return jsonify(secure=request.is_secure, remote=request.remote_addr)

        return app.test_client()

    def test_secure_defaults_and_hsts_only_on_https(self):
        client = self.security_client({})
        response = client.get("/", base_url="https://localhost")
        cookie = response.headers["Set-Cookie"]
        for flag in ("Secure", "HttpOnly", "SameSite=Lax"):
            self.assertIn(flag, cookie)
        self.assertEqual(response.headers["Strict-Transport-Security"], "max-age=31536000")
        response = client.get("/", base_url="http://localhost")
        self.assertNotIn("Strict-Transport-Security", response.headers)

    def test_explicit_local_http_development(self):
        response = self.security_client({"USE_SECURE_COOKIES": "false"}).get("/")
        self.assertNotIn("; Secure", response.headers["Set-Cookie"])
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertNotIn("Strict-Transport-Security", response.headers)

    def test_forwarded_headers_are_untrusted_by_default(self):
        response = self.security_client({}).get("/", headers={
            "X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.25"})
        self.assertFalse(response.json["secure"])
        self.assertEqual(response.json["remote"], "127.0.0.1")
        self.assertNotIn("Strict-Transport-Security", response.headers)

    def test_explicit_proxy_hops_recognize_https_and_client_address(self):
        client = self.security_client({"TRUSTED_PROXY_HOPS": "1"})
        response = client.get("/", headers={
            "X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.25"})
        self.assertTrue(response.json["secure"])
        self.assertEqual(response.json["remote"], "198.51.100.25")
        self.assertIn("Strict-Transport-Security", response.headers)
        response = client.get("/", headers={"X-Forwarded-Proto": "http"})
        self.assertNotIn("Strict-Transport-Security", response.headers)

    def test_render_flag_does_not_silently_trust_forwarded_headers(self):
        response = self.security_client({"RENDER": "true"}).get(
            "/", headers={"X-Forwarded-Proto": "https"})
        self.assertFalse(response.json["secure"])
        response = self.security_client({
            "RENDER": "true", "USE_SECURE_COOKIES": "true", "TRUSTED_PROXY_HOPS": "1",
        }).get("/", headers={"X-Forwarded-Proto": "https"})
        self.assertTrue(response.json["secure"])
        self.assertEqual(response.headers["Strict-Transport-Security"], "max-age=31536000")

    def test_invalid_security_configuration_is_rejected(self):
        for environment in ({"USE_SECURE_COOKIES": "typo"}, {"TRUSTED_PROXY_HOPS": "-1"},
                            {"TRUSTED_PROXY_HOPS": "typo"}):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                self.security_client(environment)

    def login(self, username="admin", password="wrong", address="127.0.0.1", headers=None):
        return self.client.post("/login", json={"username": username, "password": password},
                                environ_overrides={"REMOTE_ADDR": address}, headers=headers or {})

    def test_web_failures_throttle_correct_pin_until_window_expires(self):
        with patch("web_login_throttle.time.time", return_value=1000):
            for _ in range(8):
                self.assertEqual(self.login().status_code, 401)
            with patch.object(self.server, "check_password_hash") as check:
                response = self.login(password="isolated-test-only")
                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.headers["Retry-After"], "300")
                check.assert_not_called()
        with patch("web_login_throttle.time.time", return_value=1301):
            self.assertEqual(self.login(password="isolated-test-only").status_code, 200)

    def test_rotating_accounts_or_addresses_cannot_bypass_throttle(self):
        for number in range(8):
            self.assertEqual(self.login(username=f"unknown-{number}").status_code, 401)
        self.assertEqual(self.login(username="different").status_code, 429)
        connection = self.server.get_db_connection()
        connection.execute("DELETE FROM web_login_attempts")
        connection.commit()
        connection.close()
        for number in range(8):
            self.assertEqual(self.login(address=f"192.0.2.{number + 1}").status_code, 401)
        self.assertEqual(self.login(password="isolated-test-only", address="192.0.2.99").status_code, 429)

    def test_untrusted_forwarded_for_does_not_bypass_address_limit(self):
        for number in range(8):
            response = self.login(username=f"missing-{number}",
                                  headers={"X-Forwarded-For": f"192.0.2.{number + 1}"})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.login(username="missing-last").status_code, 429)

    def test_success_preserves_prior_failures_and_rotates_session(self):
        for _ in range(2):
            self.assertEqual(self.login().status_code, 401)
        with self.client.session_transaction() as cookie:
            cookie["old_session_value"] = "discard"
        self.assertEqual(self.login(password="isolated-test-only").status_code, 200)
        self.assertEqual(self._query("SELECT COUNT(*) FROM web_login_attempts")[0][0], 6)
        with self.client.session_transaction() as cookie:
            self.assertNotIn("old_session_value", cookie)
            self.assertEqual(cookie["username"], "admin")

    def test_workers_share_account_lock_even_after_address_changes_and_restart(self):
        for number in range(8):
            self.server.web_login_throttle = self.server.SQLiteLoginThrottle(self.server.get_db_connection)
            self.assertEqual(self.login(address=f"192.0.2.{number + 1}").status_code, 401)
        self.server.web_login_throttle = self.server.SQLiteLoginThrottle(self.server.get_db_connection)
        self.assertEqual(self.login(password="isolated-test-only", address="198.51.100.99").status_code, 429)

    def test_atomic_reservations_bound_parallel_workers_and_expire(self):
        barrier = Barrier(16)

        def attempt(number):
            throttle = self.server.SQLiteLoginThrottle(self.server.get_db_connection)
            barrier.wait(timeout=10)
            return throttle.reserve("admin", f"192.0.2.{number + 1}")

        with patch("web_login_throttle.time.time", return_value=1000):
            with ThreadPoolExecutor(max_workers=16) as workers:
                attempts = list(workers.map(attempt, range(16)))
            self.assertEqual(sum(value is not None for value in attempts), 8)
            self.assertEqual(self._query("SELECT COUNT(*) FROM web_login_attempts")[0][0], 24)
            self.assertIsNone(self.server.web_login_throttle.reserve("ADMIN", "198.51.100.1"))
        with patch("web_login_throttle.time.time", return_value=1300):
            self.assertIsNotNone(self.server.web_login_throttle.reserve("admin", "198.51.100.1"))
            self.assertEqual(self._query("SELECT COUNT(*) FROM web_login_attempts")[0][0], 3)

    def test_success_cannot_clear_other_workers_reserved_failures(self):
        throttle = self.server.web_login_throttle
        success = throttle.reserve("admin", "192.0.2.1")
        failure = throttle.reserve("admin", "192.0.2.2")
        throttle.complete_success(success)
        self.assertEqual(self._query("SELECT DISTINCT attempt_id FROM web_login_attempts"), [(failure,)])

    def test_ip_aggregate_is_atomic_across_parallel_accounts(self):
        barrier = Barrier(16)

        def attempt(number):
            throttle = self.server.SQLiteLoginThrottle(self.server.get_db_connection)
            barrier.wait(timeout=10)
            return throttle.reserve(f"account-{number}", "198.51.100.5")

        with ThreadPoolExecutor(max_workers=16) as workers:
            attempts = list(workers.map(attempt, range(16)))
        self.assertEqual(sum(value is not None for value in attempts), 8)
        self.assertIsNone(self.server.web_login_throttle.reserve("another-account", "198.51.100.5"))

    def test_account_ip_pair_budget_is_recorded_and_expired_together(self):
        throttle = self.server.web_login_throttle
        with patch("web_login_throttle.time.time", return_value=1000):
            for _ in range(8):
                self.assertIsNotNone(throttle.reserve("admin", "192.0.2.1"))
            buckets = self._query("SELECT bucket,COUNT(*) FROM web_login_attempts GROUP BY bucket")
            self.assertEqual(sorted(bucket.split(":")[0] for bucket, _ in buckets),
                             ["account", "account-ip", "ip"])
            self.assertTrue(all(count == 8 for _, count in buckets))
        with patch("web_login_throttle.time.time", return_value=1299):
            self.assertIsNone(throttle.reserve("admin", "192.0.2.1"))
        with patch("web_login_throttle.time.time", return_value=1300):
            self.assertIsNotNone(throttle.reserve("admin", "192.0.2.1"))

    def test_throttle_persists_no_pin_or_plain_account_address(self):
        self.login(username="sensitive-account", password="not-a-real-pin", address="198.51.100.65")
        rows = repr(self._query("SELECT * FROM web_login_attempts"))
        for value in ("sensitive-account", "not-a-real-pin", "198.51.100.65"):
            self.assertNotIn(value, rows)

    def test_database_failure_fails_closed_without_password_check(self):
        with patch.object(self.server.web_login_throttle, "reserve", side_effect=sqlite3.OperationalError), \
                patch.object(self.server, "check_password_hash") as check:
            self.assertEqual(self.login(password="isolated-test-only").status_code, 503)
            check.assert_not_called()

    def test_api_memory_throttle_is_unchanged_by_web_login_failures(self):
        for _ in range(8):
            self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.client.post("/api/auth/token", json={
            "username": "admin", "password": "isolated-test-only",
        }).status_code, 200)

    def test_first_pin_is_not_self_provisioned_and_failures_do_not_change_users(self):
        connection = self.server.get_db_connection()
        self.addCleanup(connection.close)
        connection.execute("INSERT INTO users(username,password_hash,role,is_active,created_at) "
                           "VALUES ('new-user','','employee',1,'2026-10-20 00:00:00')")
        connection.commit()
        connection.close()
        before = self._query("SELECT * FROM users ORDER BY id")
        for _ in range(8):
            response = self.login(username="new-user", password="4321")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json["code"], "pin_not_provisioned")
        self.assertEqual(self.login(username="new-user", password="4321").status_code, 429)
        self.assertEqual(self._query("SELECT * FROM users ORDER BY id"), before)

    def test_invalid_json_fields_do_not_raise_or_authenticate(self):
        for payload in ([], "text", {"username": [], "password": {}},
                        {"username": "admin", "password": "x" * 201}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/login", json=payload).status_code, 400)

    def test_web_login_does_not_log_pin(self):
        pin = "isolated-not-a-real-pin-67291"
        with self.assertNoLogs(self.server.logger, level="DEBUG"):
            self.assertEqual(self.login(password=pin).status_code, 401)


if __name__ == "__main__":
    unittest.main()
