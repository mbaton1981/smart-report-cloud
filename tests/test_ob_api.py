import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

_IMPORT_ROOT = tempfile.TemporaryDirectory()
os.environ["SECRET_KEY"] = "unit-test-secret"
os.environ["SYNC_API_KEY"] = "unit-test-sync-key"
os.environ["ADMIN_PASSWORD"] = "unit-test-admin-password"
os.environ["DATABASE_PATH"] = str(Path(_IMPORT_ROOT.name) / "server_import.db")

import server
from migrate_ob import migrate_database


class ObApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_db_file = server.DB_FILE
        server.DB_FILE = str(Path(self.temp_dir.name) / "ob_server.db")
        server.app.config["TESTING"] = True
        server.init_cloud_db()
        conn = server.get_db_connection()
        conn.execute("UPDATE meta_employees SET salary_rate = 100 WHERE name = ?", ("Aliaksei Patonich",))
        conn.commit()
        conn.close()
        migrate_database(
            server.DB_FILE,
            Path(self.temp_dir.name) / "ob_server.pre-migration.db",
        )
        self.client = server.app.test_client()
        with self.client.session_transaction() as session:
            session.update({
                "user_id": 1,
                "username": "Aliaksei Patonich",
                "role": "user",
                "employee_id": 1,
            })
        self.payload = {
            "request_id": "ob-request-1",
            "date": "2026-10-12",
            "employee": "Aliaksei Patonich",
            "company": "Privat",
            "object_name": "Bygg och renovering",
            "hours": 23,
            "rate": 99999,
            "transport": 9999,
            "comment": "evening interval",
            "submission_type": "ob",
            "start_time": "17:00",
            "end_time": "21:00",
            "ob_base_rate": 999999,
            "total_amount": 1,
        }

    def tearDown(self):
        server.DB_FILE = self.previous_db_file
        self.temp_dir.cleanup()

    def database(self):
        conn = server.get_db_connection()
        conn.row_factory = sqlite3.Row
        return conn

    def test_server_recalculates_submit_edit_history_and_delete_segments(self):
        response = self.client.post("/submit-shift", json=self.payload)
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(response.json["hours"], 4.0)
        self.assertEqual(response.json["total_amount"], 570.0)
        self.assertNotIn("ob_base_rate", response.json)
        self.assertEqual([segment["amount"] for segment in response.json["segments"]], [100.0, 300.0, 170.0])

        conn = self.database()
        original = conn.execute(
            "SELECT id, sync_id, hours, rate, transport, submission_type FROM incoming_submissions"
        ).fetchone()
        self.assertEqual(tuple(original), (1, "ob-request-1", 4.0, 0.0, 0.0, "ob"))
        stored_segments = conn.execute("SELECT COUNT(*) FROM ob_segments WHERE shift_id = ?", (original["sync_id"],)).fetchone()[0]
        conn.close()

        self.assertEqual(stored_segments, 3)
        history = self.client.post(
            "/check-employee-shifts",
            json={"employee": "Aliaksei Patonich", "month": "2026-10"},
        ).json["recent_shifts"][0]
        self.assertEqual(history["submission_type"], "ob")
        self.assertEqual(history["ob_total_amount"], 570.0)
        self.assertEqual(len(history["ob_segments"]), 3)
        self.assertNotIn("ob_base_rate", history)
        self.assertNotIn("base_rate", history["ob_segments"][0])
        exported = self.client.post("/export-shifts-csv", json={
            "employee": "Aliaksei Patonich", "month": "2026-10"
        })
        self.assertEqual(exported.status_code, 200, exported.get_data(as_text=True))
        self.assertIn("OB сегменты", exported.get_data(as_text=True))
        self.assertIn("570.0", exported.get_data(as_text=True))
        self.assertNotIn("Базовая ставка OB", exported.get_data(as_text=True))

        edit_payload = {
            "sync_id": original["sync_id"],
            "operation_id": "ob-edit-1",
            "version": 1,
            "employee": "Aliaksei Patonich",
            "date": "2026-10-12",
            "company": "Privat",
            "object_name": "Bygg och renovering",
            "submission_type": "ob",
            "start_time": "18:00",
            "end_time": "20:00",
            "ob_base_rate": 200,
            "hours": 1,
            "total_amount": 999999,
        }
        edited = self.client.post("/edit-shift", json=edit_payload)
        self.assertEqual(edited.status_code, 200, edited.json)
        conn = self.database()
        rows = conn.execute("SELECT id, sync_id, hours, start_time, end_time, ob_base_rate FROM incoming_submissions").fetchall()
        segments = conn.execute("SELECT hours, multiplier, amount FROM ob_segments WHERE shift_id = ?", (original["sync_id"],)).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0]), (original["id"], original["sync_id"], 2.0, "18:00", "20:00", 100.0))
        self.assertEqual([tuple(row) for row in segments], [(2.0, 1.5, 300.0)])

        deleted = self.client.post("/delete-shift", json={
            "sync_id": original["sync_id"],
            "operation_id": "ob-delete-1",
            "version": 2,
            "employee": "Aliaksei Patonich",
            "submission_type": "ob",
        })
        self.assertEqual(deleted.status_code, 200, deleted.json)
        conn = self.database()
        self.assertEqual(conn.execute("SELECT is_deleted FROM incoming_submissions WHERE sync_id = ?", (original["sync_id"],)).fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ob_segments WHERE shift_id = ?", (original["sync_id"],)).fetchone()[0], 0)
        conn.close()

    def test_calculate_ob_preview_uses_internal_rules_endpoint(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        page_text = page.get_data(as_text=True)
        self.assertIn('<option value="ob">OB/сверхурочные</option>', page_text)
        self.assertIn('id="ob-start-hour"', page_text)
        self.assertIn('id="ob-start-minute"', page_text)
        self.assertIn('id="edit-ob-start-hour"', page_text)
        self.assertIn('id="edit-ob-start-minute"', page_text)
        self.assertNotIn('type="time"', page_text)
        self.assertNotIn("ob-base-rate", page_text)

        response = self.client.post("/calculate-ob", json={
            "date": "2026-10-12",
            "start_time": "17:00",
            "end_time": "21:00",
            "employee": "Aliaksei Patonich",
        })
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["total_amount"], 570.0)
        self.assertEqual([segment["amount"] for segment in response.json["segments"]], [100.0, 300.0, 170.0])
        self.assertNotIn("base_rate", response.json["segments"][0])
        self.assertNotIn("ob_base_rate", response.json)

    def test_desktop_sync_matches_employee_name_case_insensitively(self):
        conn = self.database()
        conn.execute("UPDATE meta_employees SET salary_rate = 0 WHERE id = 1")
        conn.commit()
        conn.close()

        response = self.client.post("/sync-desktop-data", headers={
            "X-Sync-Key": "unit-test-sync-key",
        }, json={
            "employees": [{
                "name": "  ALIAKSEI PATONICH  ",
                "salary_rate": 245.80,
                "is_active": 1,
            }],
            "companies": [],
            "objects": [],
            "shifts": [],
        })
        self.assertEqual(response.status_code, 200, response.json)
        synced_profile = response.json["employee_sync"][0]["profiles"][0]
        self.assertEqual(synced_profile["id"], 1)
        self.assertEqual(synced_profile["salary_rate"], 245.80)

        conn = self.database()
        profiles = conn.execute(
            "SELECT id, salary_rate FROM meta_employees WHERE lower(trim(name)) = ?",
            ("aliaksei patonich",),
        ).fetchall()
        conn.close()
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["id"], 1)
        self.assertEqual(profiles[0]["salary_rate"], 245.80)

        preview = self.client.post("/calculate-ob", json={
            "date": "2026-10-12",
            "start_time": "17:00",
            "end_time": "21:00",
            "employee": "  aLiAkSeI   PaToNiCh  ",
        })
        self.assertEqual(preview.status_code, 200, preview.json)
        self.assertTrue(preview.json["base_rate_configured"])

    def test_ob_preview_repairs_stale_session_employee_id_by_username(self):
        conn = self.database()
        conn.execute("UPDATE meta_employees SET salary_rate = 0 WHERE id = 1")
        conn.execute(
            "INSERT INTO meta_employees (id, name, salary_rate, is_active) VALUES (?, ?, ?, 1)",
            (2, "ALIAKSEI   PATONICH", 245.80),
        )
        conn.commit()
        conn.close()

        with self.client.session_transaction() as session:
            session["username"] = "Aliaksei Patonich"
            session["employee_id"] = 1

        current_user = self.client.get("/me")
        self.assertEqual(current_user.status_code, 200, current_user.json)
        self.assertEqual(current_user.json["user"]["employee_id"], 2)

        with self.assertLogs("server", level="INFO") as preview_logs:
            preview = self.client.post("/calculate-ob", json={
                "date": "2026-10-12",
                "start_time": "17:00",
                "end_time": "21:00",
                "employee": "Aliaksei Patonich",
            })
        self.assertEqual(preview.status_code, 200, preview.json)
        self.assertTrue(preview.json["base_rate_configured"])
        self.assertAlmostEqual(preview.json["total_amount"], 1401.06, places=2)
        log_output = "\n".join(preview_logs.output)
        self.assertIn("employee_name='Aliaksei Patonich'", log_output)
        self.assertIn("normalize_employee_name(name) = ?", log_output)
        self.assertIn("salary_rate=245.8", log_output)

        with self.client.session_transaction() as session:
            self.assertEqual(session["employee_id"], 2)

    def test_ob_preview_uses_known_fallback_when_profile_rate_is_zero(self):
        conn = self.database()
        conn.execute("UPDATE meta_employees SET salary_rate = 0 WHERE id = 1")
        conn.commit()
        conn.close()

        preview = self.client.post("/calculate-ob", json={
            "date": "2026-10-12",
            "start_time": "17:00",
            "end_time": "21:00",
            "employee": "Aliaksei Patonich",
        })
        self.assertEqual(preview.status_code, 200, preview.json)
        self.assertTrue(preview.json["base_rate_configured"])
        self.assertAlmostEqual(preview.json["total_amount"], 1401.06, places=2)

    def test_startup_seeds_known_base_rate_only_when_database_rate_is_zero(self):
        conn = self.database()
        conn.execute("UPDATE meta_employees SET salary_rate = 0 WHERE id = 1")
        conn.commit()
        conn.close()

        server.init_cloud_db()

        conn = self.database()
        profile = conn.execute("SELECT salary_rate FROM meta_employees WHERE id = 1").fetchone()
        conn.close()
        self.assertEqual(profile["salary_rate"], 245.80)

    def test_server_rejects_invalid_ob_and_does_not_trust_client_amount(self):
        invalid = dict(self.payload, request_id="invalid-ob", start_time="10:00", end_time="10:00")
        response = self.client.post("/submit-shift", json=invalid)
        self.assertEqual(response.status_code, 400)
        self.assertIn("не может совпадать", response.json["error"])

        self.assertEqual(self.client.post("/submit-shift", json=self.payload).status_code, 201)
        conn = self.database()
        stored = conn.execute("SELECT hours, rate, transport FROM incoming_submissions WHERE request_id = ?", ("ob-request-1",)).fetchone()
        self.assertEqual(tuple(stored), (4.0, 0.0, 0.0))
        conn.close()

    def test_server_rounds_arbitrary_ob_times_to_nearest_half_hour(self):
        payload = dict(
            self.payload,
            request_id="rounded-ob",
            start_time="17:14",
            end_time="21:16",
        )
        response = self.client.post("/submit-shift", json=payload)
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual((response.json["start_time"], response.json["end_time"]), ("17:00", "21:30"))
        self.assertEqual(response.json["hours"], 4.5)
        self.assertEqual(response.json["total_amount"], 655.0)
        conn = self.database()
        saved = conn.execute(
            "SELECT start_time, end_time, hours FROM incoming_submissions WHERE request_id = ?",
            ("rounded-ob",),
        ).fetchone()
        conn.close()
        self.assertEqual(tuple(saved), ("17:00", "21:30", 4.5))

    def test_old_submission_payload_remains_work(self):
        legacy = dict(self.payload)
        legacy.pop("submission_type")
        legacy.pop("start_time")
        legacy.pop("end_time")
        legacy.pop("ob_base_rate")
        legacy.pop("total_amount")
        legacy.update({"request_id": "legacy-work", "hours": 8, "rate": 100, "transport": 0})
        response = self.client.post("/submit-shift", json=legacy)
        self.assertEqual(response.status_code, 201, response.json)
        conn = self.database()
        row = conn.execute("SELECT submission_type, hours, rate FROM incoming_submissions WHERE request_id = ?", ("legacy-work",)).fetchone()
        self.assertEqual(tuple(row), ("work", 8.0, 100.0))
        conn.close()

    def test_atf_and_sick_submission_types_remain_unchanged(self):
        conn = self.database()
        conn.execute(
            "INSERT OR IGNORE INTO meta_objects (name, markning, company) VALUES (?, ?, ?)",
            ("Arbetstidsförkortning", "p1000", "Privat"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta_objects (name, markning, company) VALUES (?, ?, ?)",
            ("Sjuk", "p1001", "Privat"),
        )
        conn.commit()
        conn.close()

        for record_type, object_name in (("atf", "Arbetstidsförkortning"), ("sick", "Sjuk")):
            payload = {
                "request_id": f"legacy-{record_type}", "date": "2026-10-12",
                "employee": "Aliaksei Patonich", "company": "Privat", "object_name": object_name,
                "hours": 4, "rate": 50, "transport": 1, "comment": "old type",
                "submission_type": record_type,
            }
            response = self.client.post("/submit-shift", json=payload)
            self.assertEqual(response.status_code, 201, response.json)

        conn = self.database()
        rows = conn.execute(
            "SELECT submission_type, rate, transport FROM incoming_submissions ORDER BY request_id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("atf", 0.0, 0.0), ("sick", 0.0, 0.0)])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ob_segments").fetchone()[0], 0)
        conn.close()

    def test_sync_push_recalculates_ob_segments_instead_of_trusting_client(self):
        response = self.client.post("/sync/push", headers={"X-Sync-Key": "unit-test-sync-key"}, json={
            "changes": [{
                "operation_id": "ob-sync-1",
                "sync_id": "stable-ob-sync-id",
                "operation": "upsert",
                "base_version": 0,
                "shift": {
                    "record_type": "ob",
                    "date": "2026-10-12",
                    "employee": "Aliaksei Patonich",
                    "company": "Privat",
                    "object_name": "Bygg och renovering",
                    "hours": 1,
                    "rate": 99999,
                    "transport": 9999,
                    "start_time": "17:00",
                    "end_time": "21:00",
                    "ob_base_rate": 80,
                    "total_amount": 0.01,
                    "comment": "synced OB",
                },
            }]
        })
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json["results"][0]["ok"])
        conn = self.database()
        row = conn.execute("SELECT hours, rate, transport, submission_type, ob_base_rate FROM incoming_submissions WHERE sync_id = ?", ("stable-ob-sync-id",)).fetchone()
        segments = conn.execute("SELECT amount FROM ob_segments WHERE shift_id = ? ORDER BY start_time", ("stable-ob-sync-id",)).fetchall()
        conn.close()
        self.assertEqual(tuple(row), (4.0, 0.0, 0.0, "ob", 100.0))
        self.assertEqual([segment[0] for segment in segments], [100.0, 300.0, 170.0])

    def test_unmigrated_server_refuses_ob_but_accepts_legacy_work(self):
        previous_db = server.DB_FILE
        server.DB_FILE = str(Path(self.temp_dir.name) / "unmigrated.db")
        try:
            server.init_cloud_db()
            rejected = self.client.post("/submit-shift", json=self.payload)
            self.assertEqual(rejected.status_code, 503)
            self.assertIn("migrate_ob.py", rejected.json["error"])
            legacy = dict(self.payload, request_id="old-work-payload", hours=8, rate=100, transport=0)
            for key in ("submission_type", "start_time", "end_time", "ob_base_rate", "total_amount"):
                legacy.pop(key, None)
            accepted = self.client.post("/submit-shift", json=legacy)
            self.assertEqual(accepted.status_code, 201, accepted.json)
            conn = self.database()
            self.assertEqual(conn.execute("SELECT submission_type FROM incoming_submissions").fetchone()[0], "work")
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name = 'ob_segments'").fetchone())
            conn.close()
        finally:
            server.DB_FILE = previous_db


if __name__ == "__main__":
    unittest.main()