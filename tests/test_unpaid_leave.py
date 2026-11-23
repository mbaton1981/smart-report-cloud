from contextlib import closing
from datetime import date, timedelta
from io import BytesIO
import unittest

from pypdf import PdfReader

import payroll_history
import payroll_management
import payroll_ui
import test_ob_transport as transport
from customer_billing import prepare_next_invoice
from service_categories import UNPAID_LEAVE, UNPAID_LEAVE_NAME


class UnpaidLeaveTests(unittest.TestCase):
    setUpClass = classmethod(transport.OBTransportTests.setUpClass.__func__)
    tearDownClass = classmethod(transport.OBTransportTests.tearDownClass.__func__)
    _add_company = transport.OBTransportTests._add_company

    def setUp(self):
        transport.OBTransportTests.setUp(self)
        self.headers = {"X-Sync-Key": "isolated-sync-test-key"}
        self.day = (date.today() - timedelta(days=1)).isoformat()
        with closing(self.server.get_db_connection()) as con:
            con.execute("""
                INSERT INTO meta_objects(company,name,markning,service_type,service_builtin)
                VALUES ('Renatur','Sjuk','p1001','sick',1)
            """)
            self.server.ensure_unpaid_leave_objects(con.cursor())
            con.commit()

    def test_builtin_category_seed_is_idempotent_and_keeps_existing_markings(self):
        self._add_company("Other")
        with closing(self.server.get_db_connection()) as con:
            con.execute("""
                INSERT INTO meta_objects(company,name,markning,service_type,service_builtin)
                VALUES ('Other','Sjuk','p1001','sick',1), ('Other','Existing work','P1003',NULL,0)
            """)
            for _ in range(2):
                self.server.ensure_unpaid_leave_objects(con.cursor())
            rows = con.execute("""
                SELECT company,name,service_type FROM meta_objects WHERE lower(markning)='p1003'
                ORDER BY company
            """).fetchall()
            self.assertEqual([tuple(row) for row in rows],
                             [("Other", "Existing work", None), ("Renatur", UNPAID_LEAVE_NAME, UNPAID_LEAVE)])
        endpoint = "/api/expense-objects?include_service_categories=1"
        legacy = self.server_client.get(endpoint, headers=self.headers)
        current = self.server_client.get(endpoint + "&include_unpaid_leave=1", headers=self.headers)
        self.assertEqual((legacy.status_code, current.status_code), (200, 200))
        self.assertFalse(any(o["service_type"] == UNPAID_LEAVE for o in legacy.json["objects"]))
        objects = current.json["objects"]
        self.assertEqual(sum(o["service_type"] == UNPAID_LEAVE for o in objects), 1)
        with closing(self.desktop.get_db_connection()) as con:
            self.desktop.merge_server_object_catalog(con.cursor(), objects)
            record, problem = self.desktop.resolve_special_object(con.cursor(), "Renatur", UNPAID_LEAVE)
            self.assertIsNone(problem)
            self.assertEqual((record["markning"], record["service_builtin"]), ("p1003", 1))

    def test_web_entry_and_payroll_keep_unpaid_hours_outside_work_and_billing(self):
        payload = dict(request_id="isolated-leave", date=self.day, employee="Travel Test",
                       company="Renatur", object_name=UNPAID_LEAVE_NAME, submission_type=UNPAID_LEAVE,
                       hours=4.5, rate=999, transport=2)
        response = self.server_client.post("/submit-shift", json=payload)
        self.assertEqual(response.status_code, 201, response.get_json())
        wrong = self.server_client.post("/submit-shift", json={**payload, "request_id": "wrong-type",
                                                              "submission_type": "work"})
        self.assertEqual(wrong.status_code, 400)
        pulled = self.server_client.get("/sync/pull", headers=self.headers).json["changes"]
        self.assertEqual([(row["record_type"], row["rate"], row["transport"]) for row in pulled],
                         [(UNPAID_LEAVE, 0, 0)])
        with closing(self.server.get_db_connection()) as con:
            daybook, error = self.server.fetch_arbetsdagbok_records(con.cursor(), "Travel Test", self.day[:7])
            self.assertIsNone(error)
            self.assertEqual(daybook["totals_by_type"][UNPAID_LEAVE], 4.5)
            self.assertEqual(daybook["totals_by_type"]["work"], 0)
            row = dict(con.execute("SELECT * FROM incoming_submissions").fetchone(), entry_type=UNPAID_LEAVE)
            historical = payroll_history.calculate_line(row, [], {}, {}, self.day, self.day)
            self.assertEqual(historical["amount"], 0)
            self.assertEqual(historical["warnings"], [])
            obj = con.execute("SELECT id FROM meta_objects WHERE service_type=?", (UNPAID_LEAVE,)).fetchone()[0]
            self.assertEqual(prepare_next_invoice([row], [], obj, self.day, {})[0], [])
        with closing(self.desktop.get_db_connection()) as con:
            window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
            self.desktop.QMainWindow.__init__(window)
            self.addCleanup(window.deleteLater)
            window.apply_remote_shift(con.cursor(), pulled[0])
            report, = payroll_management.reports(
                con.cursor(), self.day, self.day,
                payroll_management.swedish_public_holidays, payroll_management.swedish_workday_norm,
            )
            self.assertEqual(report["worked"], 0)
            self.assertEqual(report["absence"][UNPAID_LEAVE], 4.5)
            self.assertEqual(report["total"], 0)
            report["norm_override"] = False
            out = BytesIO()
            payroll_ui.write_management_pdf(out, [report], self.desktop.DEFAULT_FONT, self.desktop.DEFAULT_FONT_BOLD)
            page_text = "\n".join(page.extract_text() for page in PdfReader(out).pages)
            self.assertIn("Obetald ledighet", page_text)
            self.assertIn("P1003", page_text)
