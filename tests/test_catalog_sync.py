import os
import tempfile
import unittest
import uuid
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

        with patch.object(self.desktop.requests, "get", side_effect=fake_get), \
             patch.object(self.desktop.requests, "post", side_effect=fake_post):
            return self.desktop.SmartReportApp.perform_sync_logic(SimpleNamespace())

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
        self.assertEqual(result, {
            "conflicts": 0, "linked_legacy": 1, "unresolved_legacy": 1,
            "imported_employees": 0, "unresolved_employees": 0,
        })
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


if __name__ == "__main__":
    unittest.main()