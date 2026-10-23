from io import BytesIO
from pathlib import Path
import json
import sqlite3
import tempfile
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
    def tearDown(self):
        if hasattr(self, "management_connection"):
            self.management_connection.close()
        catalog.CatalogSyncTests.tearDown(self)
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

    def salary_window(self, management_mode=False):
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_salary = self.desktop.QWidget()
        window.setCentralWidget(window.tab_salary)
        window.setup_salary_tab()
        if not management_mode:
            window.sal_report_mode.setCurrentIndex(1)
        self.addCleanup(window.close)
        return window

    def payroll_draft(self):
        self.prepare()
        response, _ = self.request("preview", date_from="2026-10-01", date_to="2026-10-31", parameters=PARAMS)
        return response.get_json()["preview"]

    def test_export_button_saves_unknown_unpaid_report_to_selected_path(self):
        draft = self.payroll_draft()
        window = self.salary_window()
        button = next(b for b in window.findChildren(self.desktop.QPushButton)
                      if "Экспорт зарплат" in b.text())
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        window.show()
        self.qt_app.processEvents()
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(payroll_ui, "reports", return_value=[draft]), \
                patch.object(payroll_ui.QFileDialog, "getSaveFileName",
                             return_value=(str(Path(folder)/"chosen.pdf"), "PDF (*.pdf)")) as choose, \
                patch.object(payroll_ui.QMessageBox, "information") as information:
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            choose.assert_called_once()
            information.assert_called_once()
            message = information.call_args.args[2]
            self.assertIn("Payroll Test", message)
            self.assertIn("2026-10-21", message)
            self.assertIn("payroll-one", message)
            self.assertIn("историческая ставка", message)
            self.assertIn(str(Path(folder)/"chosen.pdf"), message)
            reader = PdfReader(Path(folder)/"chosen.pdf")
            self.assertIn("Требует подтверждения", "\n".join(p.extract_text() for p in reader.pages))
        self.assertFalse(draft["paid"])

    def test_export_cancel_does_not_write_or_show_success(self):
        draft = self.payroll_draft()
        with patch.object(payroll_ui, "reports", return_value=[draft]), \
                patch.object(payroll_ui.QFileDialog, "getSaveFileName", return_value=("", "")), \
                patch.object(payroll_ui, "write_pdf") as write, \
                patch.object(payroll_ui.QMessageBox, "information") as information:
            payroll_ui.export(self.salary_window(), self.desktop)
        write.assert_not_called()
        information.assert_not_called()

    def test_export_reports_read_failure_is_logged_and_visible(self):
        window = self.salary_window()
        with patch.object(payroll_ui, "reports", side_effect=sqlite3.OperationalError("ledger unreadable")), \
                patch.object(payroll_ui.QMessageBox, "critical") as critical, \
                self.assertLogs("payroll_ui", level="ERROR") as log:
            payroll_ui.export(window, self.desktop)
        self.assertIn("ledger unreadable", critical.call_args.args[2])
        self.assertIn("ledger unreadable", "\n".join(log.output))

    def test_export_write_failure_is_logged_and_visible(self):
        draft = self.payroll_draft()
        with patch.object(payroll_ui, "reports", return_value=[draft]), \
                patch.object(payroll_ui.QFileDialog, "getSaveFileName", return_value=("locked.pdf", "")), \
                patch.object(payroll_ui, "write_pdf", side_effect=PermissionError("file is open")), \
                patch.object(payroll_ui.QMessageBox, "critical") as critical, \
                patch.object(payroll_ui.QMessageBox, "information") as information, \
                self.assertLogs("payroll_ui", level="ERROR"):
            payroll_ui.export(self.salary_window(), self.desktop)
        self.assertIn("file is open", critical.call_args.args[2])
        information.assert_not_called()

    def seed_management_shift(self):
        conn = self.desktop.get_db_connection()
        conn.execute("INSERT INTO employees(name,salary_rate,is_active) VALUES('Management Test',100,1)")
        conn.execute("""INSERT INTO shifts(sync_id,date,employee,company,object_name,hours,rate,
                      entry_type,start_time,end_time) VALUES('management-one','2026-10-21',
                      'Management Test','Renatur','Site',4,999,'ob','16:00','20:00')""")
        conn.commit()
        self.management_connection = conn
        return conn

    def test_management_is_default_hours_pdf_and_gui_match_without_confirmation(self):
        conn = self.seed_management_shift()
        before = list(conn.iterdump())
        window = self.salary_window(management_mode=True)
        window.load_salary_table_template()
        row = next(i for i in range(window.table_salary.rowCount())
                   if window.table_salary.item(i, 0).text() == "Management Test")
        self.assertEqual(window.table_salary.item(row, 1).text(), "4")
        self.assertEqual(window.table_salary.item(row, 9).text(), "100.00")
        self.assertEqual(window.table_salary.item(row, 12).text(), "500.00")
        self.assertIn("OB1", window.table_salary.horizontalHeaderItem(3).text())
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        window.show()
        self.qt_app.processEvents()
        button = next(b for b in window.findChildren(self.desktop.QPushButton) if "Экспорт зарплат" in b.text())
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(payroll_ui.QFileDialog, "getSaveFileName",
                             return_value=(str(Path(folder)/"hours.pdf"), "PDF (*.pdf)")), \
                patch.object(payroll_ui.QMessageBox, "information") as information:
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            text = "\n".join(p.extract_text() for p in PdfReader(Path(folder)/"hours.pdf").pages)
            self.assertIn("Предварительный расчёт по текущим ставкам", text)
            self.assertIn("500.00", text)
            self.assertIn("OB1 +20%: 1", text)
            self.assertIn("OB2 +40%: 2", text)
            self.assertIn("Всего отработано: 4", text)
            self.assertIn("Fortnox", text)
            self.assertNotIn("999.00", text)
            self.assertNotIn("историческая ставка", information.call_args.args[2])
        self.assertEqual(before, list(conn.iterdump()))
        window.sal_report_mode.setCurrentIndex(1)
        self.assertIn("Grundlön", window.table_salary.horizontalHeaderItem(7).text())
        window.sal_report_mode.setCurrentIndex(0)
        self.assertIn("База", window.table_salary.horizontalHeaderItem(10).text())
        self.assertEqual(before, list(conn.iterdump()))

    def test_management_missing_rate_exports_without_amount_and_no_period_close(self):
        conn = self.seed_management_shift()
        conn.execute("UPDATE employees SET salary_rate=0 WHERE name='Management Test'")
        conn.commit()
        before = list(conn.iterdump())
        window = self.salary_window(management_mode=True)
        items = payroll_ui.selected_reports(window, self.desktop)
        report = next(r for r in items if r["employee"] == "Management Test")
        self.assertIsNone(report["total"])
        pdf = BytesIO()
        payroll_ui.write_pdf(pdf, [report], self.desktop.DEFAULT_FONT, self.desktop.DEFAULT_FONT_BOLD)
        text = "\n".join(p.extract_text() for p in PdfReader(pdf).pages)
        self.assertIn("Всего отработано: 4", text)
        self.assertIn("без суммы", text)
        self.assertEqual(before, list(conn.iterdump()))


if __name__ == "__main__":
    unittest.main()
