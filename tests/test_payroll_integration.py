from io import BytesIO
import json
import sqlite3
import unittest
import uuid
from unittest.mock import patch

from pypdf import PdfReader
import payroll_history as history
import payroll_ui
import test_catalog_sync as catalog
from test_payroll_history import PARAMS


class PayrollIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(catalog.CatalogSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog.CatalogSyncTests.tearDownClass.__func__)
    setUp = catalog.CatalogSyncTests.setUp
    tearDown = catalog.CatalogSyncTests.tearDown
    _add_company = catalog.CatalogSyncTests._add_company
    _worker = catalog.CatalogSyncTests._worker
    _confirm_fixture_customer_prices = catalog.CatalogSyncTests._confirm_fixture_customer_prices

    def prepare(self):
        conn = self.server.get_db_connection()
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO meta_employees(name,salary_rate,is_active) VALUES('Payroll Test',999,1)")
        conn.execute("""INSERT INTO incoming_submissions(sync_id,date,employee,company,object_name,hours,
                      rate,transport,submission_type,is_deleted,request_id,created_at,updated_at)
                      VALUES('payroll-one','2026-10-21','Payroll Test','Renatur','Site',4,520,1,'work',0,
                             'payroll-request','2026-10-21','2026-10-21')""")
        conn.execute("INSERT INTO meta_objects(company,name,markning) VALUES('Renatur','Site','test-site')")
        conn.commit()
        conn.close()

    def request(self, kind, **values):
        headers = {"X-Sync-Key": "isolated-sync-test-key"}
        current = self.server_client.get("/sync/payroll", headers=headers).get_json()
        body = dict(kind=kind, employee="Payroll Test", operation_id=str(uuid.uuid4()),
                    expected_seq=len(current["events"]), **values)
        return self.server_client.post("/sync/payroll", json=body, headers=headers), body

    def test_api_auth_preview_no_migration_backfill_and_retry(self):
        self.prepare()
        response = self.server_client.get("/sync/payroll")
        self.assertEqual(response.status_code, 403)
        preview, _ = self.request("preview", date_from="2026-10-01", date_to="2026-10-31", parameters=PARAMS)
        self.assertEqual(preview.status_code, 200, preview.get_json())
        self.assertIsNone(preview.get_json()["preview"]["amounts"])
        self.assertEqual(preview.get_json()["events"], [])
        with patch.object(history, "WRITES_ENABLED", True):
            rate, _ = self.request("rate", effective_from="2026-10-01", rate=100)
            self.assertEqual(rate.status_code, 200, rate.get_json())
            preview, _ = self.request("preview", date_from="2026-10-01", date_to="2026-10-31", parameters=PARAMS)
            draft = preview.get_json()["preview"]
            closed, body = self.request("close", date_from="2026-10-01", date_to="2026-10-31",
                                        parameters=PARAMS, preview_hash=draft["preview_hash"])
            self.assertEqual(closed.status_code, 200, closed.get_json())
            retried = self.server_client.post("/sync/payroll", json=body,
                                               headers={"X-Sync-Key": "isolated-sync-test-key"})
            self.assertEqual(retried.get_json()["events"], closed.get_json()["events"])
        with patch.object(history, "WRITES_ENABLED", False):
            refused, _ = self.request("rate", effective_from="2026-10-01", rate=999)
            self.assertEqual(refused.status_code, 400)
            visible, _ = self.request("preview", date_from="2026-10-01", date_to="2026-10-31", parameters=PARAMS)
            self.assertEqual(visible.get_json()["preview"]["previous"]["amounts"]["base_salary"], 400)
        conn = self.server.get_db_connection()
        self.assertEqual(conn.execute("SELECT salary_rate FROM meta_employees WHERE name='Payroll Test'").fetchone()[0], 999)
        conn.close()

    def test_salary_pdf_does_not_turn_unknown_into_zero(self):
        self.prepare()
        response, _ = self.request("preview", date_from="2026-10-01", date_to="2026-10-31", parameters=PARAMS)
        draft = response.get_json()["preview"]
        pdf = BytesIO()
        payroll_ui.write_pdf(pdf, [draft], self.desktop.DEFAULT_FONT, self.desktop.DEFAULT_FONT_BOLD)
        text = "\n".join(page.extract_text() for page in PdfReader(pdf).pages)
        self.assertIn("Требует подтверждения", text)
        self.assertIn("Payroll Test", text)
        self.assertNotIn("999.00", text)

    def test_full_sync_carries_ledger_without_reconfirmation(self):
        self.prepare()
        with patch.object(history, "WRITES_ENABLED", True):
            response, _ = self.request("rate", effective_from="2026-10-01", rate=100)
            self.assertEqual(response.status_code, 200)
        self._worker()
        self._worker()
        local = self.desktop.get_db_connection()
        self.assertEqual(len(history.events(local.cursor())), 1)
        self.assertEqual(json.loads(history.events(local.cursor())[0]["event_json"])["rate"], 100)
        local.close()

    def test_desktop_and_server_new_ob_resolve_same_dated_rates(self):
        self.prepare()
        with patch.object(history, "WRITES_ENABLED", True):
            for day, rate in (("2026-10-01", 100), ("2026-10-22", 200)):
                response, _ = self.request("rate", effective_from=day, rate=rate)
                self.assertEqual(response.status_code, 200, response.get_json())
            response, _ = self.request("zone", company="Renatur", object_name="Site",
                                      timezone="Europe/Stockholm")
            self.assertEqual(response.status_code, 200, response.get_json())
        conn = self.server.get_db_connection()
        try:
            server_result = self.server.calculate_ob_payload(
                conn.cursor(), "2026-10-21", "22:00", "04:00", "Payroll Test",
                company="Renatur", object_name="Site")
            incoming = history.events(conn.cursor())
        finally:
            conn.close()
        local = self.desktop.get_db_connection()
        try:
            history.replace_cache(local.cursor(), incoming)
            local.commit()
            result = history.entry_interval(local.cursor(), "Payroll Test", "Renatur", "Site",
                                            "2026-10-21", "22:00", "04:00", 999)
            self.assertEqual(result["segments"], server_result["segments"])
            self.assertEqual({s["base_rate"] for s in result["segments"]}, {100, 200})
        finally:
            local.close()


if __name__ == "__main__":
    unittest.main()
