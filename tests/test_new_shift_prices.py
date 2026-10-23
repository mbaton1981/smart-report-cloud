import json
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

import test_catalog_sync as catalog
from customer_pricing import NEW_SHIFT_PRICE_SOURCE, new_shift_pricing, row_prices, store_pricing


class NewShiftPricesTests(unittest.TestCase):
    setUpClass = classmethod(catalog.CatalogSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog.CatalogSyncTests.tearDownClass.__func__)
    _add_company = catalog.CatalogSyncTests._add_company

    def setUp(self):
        catalog.CatalogSyncTests.setUp(self)
        self.addCleanup(self._case.cleanup)
        self._add_company("Other")
        for connect, objects, employees in (
            (self.server.get_db_connection, "meta_objects", "meta_employees"),
            (self.desktop.get_db_connection, "objects", "employees"),
        ):
            conn = connect()
            conn.execute(f"INSERT INTO {employees}(name,salary_rate,is_active) VALUES('Price Test',200,1)")
            for company, rate in (("Renatur", 520), ("Other", 999)):
                conn.execute(
                    f"INSERT INTO {objects}(company,name,markning,rate,transport_rate,is_active) "
                    "VALUES(?,'Hotel','p1039',?,450,1)", (company, rate),
                )
            conn.commit()
            conn.close()
        conn = self.server.get_db_connection()
        self.employee_id = conn.execute("SELECT id FROM meta_employees WHERE name='Price Test'").fetchone()[0]
        conn.close()
        with self.server_client.session_transaction() as session:
            session.update(user_id=1, role="admin")

    def payload(self, **overrides):
        return dict(
            request_id=str(uuid.uuid4()), date="2026-10-23", employee="Price Test",
            company="Renatur", object_name="Hotel", hours=8, transport=1,
            submission_type="work",
        ) | overrides

    def submit(self, data):
        response = self.server_client.post("/submit-shift", json=data)
        self.assertEqual(response.status_code, 201, response.json)
        conn = self.server.get_db_connection()
        row = dict(conn.execute(
            "SELECT * FROM incoming_submissions WHERE sync_id=?", (data["request_id"],),
        ).fetchone())
        conn.close()
        return response, row

    def desktop_save(self, rate="", hours=8):
        d = self.desktop
        window = d.SmartReportApp.__new__(d.SmartReportApp)
        d.QMainWindow.__init__(window)
        self.addCleanup(window.close)
        window.date_input = d.QLineEdit("2026-10-23")
        for attr, text in (("emp_cb", "Price Test"), ("comp_cb", "Renatur"), ("obj_cb", "Hotel")):
            combo = d.QComboBox()
            combo.addItem(text, text)
            setattr(window, attr, combo)
        window.record_type_cb = d.QComboBox()
        window.record_type_cb.addItem("Work", "work")
        window.hours_input = d.QLineEdit(str(hours))
        window.rate_input = d.QLineEdit(rate)
        window.trans_input = d.QLineEdit("1")
        window.comment_input = d.QLineEdit("Synthetic")
        with patch.object(window, "background_sync_with_cloud"), \
                patch.object(window, "load_shifts_history"), \
                patch.object(d.QMessageBox, "information"), \
                patch.object(d.QMessageBox, "warning") as warning:
            window.save_shift()
        warning.assert_not_called()
        conn = d.get_db_connection()
        row = dict(conn.execute("SELECT * FROM shifts ORDER BY id DESC LIMIT 1").fetchone())
        queued = json.loads(conn.execute("SELECT payload_json FROM shift_sync_outbox ORDER BY id DESC LIMIT 1").fetchone()[0])
        conn.close()
        return row, queued

    def push(self, sync_id, payload, operation_id="new-price", version=0):
        response = self.server_client.post("/sync/push", headers={"X-Sync-Key": "isolated-sync-test-key"},
            json={"changes": [{"sync_id": sync_id, "operation_id": operation_id,
                               "operation": "upsert", "base_version": version, "shift": payload}]})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json["results"][0]["ok"])
        return response

    def test_worker_receives_no_client_price_but_server_snapshots_both_prices(self):
        with self.server_client.session_transaction() as session:
            session.update(role="user", employee_id=self.employee_id)
        response, row = self.submit(self.payload())
        quote = json.loads(row["customer_pricing"])
        self.assertEqual((row["rate"], quote["base"], quote["travel"]), (0, 520, 450))
        self.assertEqual(quote["source"], NEW_SHIFT_PRICE_SOURCE)
        self.assertNotIn("520", response.get_data(as_text=True))
        self.assertNotIn("customer_pricing", response.json)
        repeated = self.server_client.post("/submit-shift", json=self.payload(request_id=row["sync_id"]))
        self.assertEqual(repeated.status_code, 200, repeated.json)
        self.assertTrue(repeated.json["idempotent"])

    def test_desktop_blank_uses_catalog_manual_and_zero_are_preserved(self):
        for manual, expected in (("", 520), ("610", 610), ("0", 0)):
            row, queued = self.desktop_save(manual)
            self.assertEqual((row_prices(row)["base"], row_prices(row)["travel"]), (expected, 450))
            self.assertEqual(json.loads(queued["customer_pricing"])["base"], expected)
            if manual:
                self.assertTrue(row_prices(row)["source"].startswith("Ручн"))

    def test_same_name_in_other_company_and_no_matching_object(self):
        conn = self.server.get_db_connection()
        cursor = conn.cursor()
        for company, expected in (("Renatur", 520), ("Other", 999)):
            quote = new_shift_pricing(cursor, "incoming_submissions", {
                "date": "2026-10-23", "company": company, "object_name": "Hotel",
            })
            self.assertEqual(quote["base"], expected)
        with self.assertRaisesRegex(ValueError, "Объект"):
            new_shift_pricing(cursor, "incoming_submissions", {
                "date": "2026-10-23", "company": "Absent", "object_name": "Hotel",
            })
        conn.close()

    def test_catalog_update_only_affects_new_records_and_retry_keeps_snapshot(self):
        data = self.payload()
        _, original = self.submit(data)
        conn = self.server.get_db_connection()
        conn.execute("UPDATE meta_objects SET rate=600,transport_rate=500 WHERE company='Renatur'")
        conn.execute("UPDATE incoming_submissions SET invoiced=1 WHERE sync_id=?", (data["request_id"],))
        conn.commit()
        conn.close()
        repeated = self.server_client.post("/submit-shift", json=data)
        self.assertEqual(repeated.status_code, 200)
        _, new = self.submit(self.payload(hours=5))
        self.assertEqual(json.loads(new["customer_pricing"])["base"], 600)
        self.assertEqual(json.loads(new["customer_pricing"])["travel"], 500)
        conn = self.server.get_db_connection()
        self.assertEqual(conn.execute("SELECT customer_pricing FROM incoming_submissions WHERE sync_id=?",
                                     (data["request_id"],)).fetchone()[0], original["customer_pricing"])
        conn.close()

    def test_new_desktop_sync_server_resolves_stale_catalog_then_pull_after_ack(self):
        row, queued = self.desktop_save()
        conn = self.server.get_db_connection()
        conn.execute("UPDATE meta_objects SET rate=530,transport_rate=460 WHERE company='Renatur'")
        conn.commit()
        conn.close()
        first = self.push(row["sync_id"], queued)
        self.push(row["sync_id"], queued)
        conn = self.server.get_db_connection()
        remote = self.server.serialize_submission(conn.execute(
            "SELECT * FROM incoming_submissions WHERE sync_id=?", (row["sync_id"],),
        ).fetchone(), cursor=conn.cursor())
        conn.close()
        self.assertEqual(json.loads(remote["customer_pricing"])["base"], 530)
        local = self.desktop.get_db_connection()
        local.execute("DELETE FROM shift_sync_outbox")
        local.execute("UPDATE shifts SET sync_version=?", (first.json["results"][0]["version"],))
        self.desktop.SmartReportApp.apply_remote_shift(SimpleNamespace(), local.cursor(), remote)
        after = dict(local.execute("SELECT * FROM shifts WHERE sync_id=?", (row["sync_id"],)).fetchone())
        self.assertEqual((row_prices(after)["base"], row_prices(after)["travel"]), (530, 460))
        audit_count = local.execute("SELECT COUNT(*) FROM customer_price_audit").fetchone()[0]
        self.desktop.SmartReportApp.apply_remote_shift(SimpleNamespace(), local.cursor(), remote)
        self.assertEqual(local.execute("SELECT COUNT(*) FROM customer_price_audit").fetchone()[0], audit_count)
        local.close()

    def test_sync_legacy_new_assigns_prices_existing_missing_is_not_repriced(self):
        data = self.payload()
        data.pop("request_id")
        self.push("new-legacy", data)
        conn = self.server.get_db_connection()
        quote = conn.execute("SELECT customer_pricing FROM incoming_submissions WHERE sync_id='new-legacy'").fetchone()[0]
        self.assertEqual(json.loads(quote)["base"], 520)
        conn.execute("UPDATE incoming_submissions SET customer_pricing=NULL")
        conn.commit()
        conn.close()
        self.push("new-legacy", dict(data, comment="Comment only"), operation_id="edit-legacy", version=1)
        conn = self.server.get_db_connection()
        self.assertIsNone(conn.execute("SELECT customer_pricing FROM incoming_submissions").fetchone()[0])
        conn.close()

    def test_period_contract_precedes_catalog_and_conflicts_require_review(self):
        _, row = self.submit(self.payload())
        conn = self.server.get_db_connection()
        quote = json.loads(row["customer_pricing"])
        quote.update(base=610, travel=480, source="Confirmed contract", date_from="2026-10-01", date_to="2026-10-31")
        store_pricing(conn.cursor(), "incoming_submissions", row["sync_id"], quote)
        conn.commit()
        conn.close()
        _, second = self.submit(self.payload(hours=5))
        self.assertEqual(json.loads(second["customer_pricing"])["base"], 610)
        conn = self.server.get_db_connection()
        quote["base"] = 620
        store_pricing(conn.cursor(), "incoming_submissions", second["sync_id"], quote)
        conn.commit()
        conn.close()
        failed = self.server_client.post("/submit-shift", json=self.payload(hours=2))
        self.assertEqual(failed.status_code, 400, failed.json)
        self.assertIn("противоречащие", failed.json["error"])

    def test_unknown_catalog_warns_without_fabricating_zero_base(self):
        conn = self.server.get_db_connection()
        conn.execute("UPDATE meta_objects SET rate=0 WHERE company='Renatur'")
        conn.commit()
        conn.close()
        response, row = self.submit(self.payload())
        self.assertTrue(response.json["pricing_warning"])
        self.assertIsNone(json.loads(row["customer_pricing"])["base"])


if __name__ == "__main__":
    unittest.main()
