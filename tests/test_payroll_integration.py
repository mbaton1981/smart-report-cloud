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
import payroll_preview
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
        self.assertEqual(window.table_salary.item(row, 8).text(), "500.00")
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
            self.assertIn("OB1", text)
            self.assertIn("OB2", text)
            self.assertIn("Отработано", text)
            self.assertIn("Fortnox", text)
            self.assertNotIn("999.00", text)
            self.assertNotIn("историческая ставка", information.call_args.args[2])
        self.assertEqual(before, list(conn.iterdump()))
        window.sal_report_mode.setCurrentIndex(1)
        self.assertIn("Grundlön", window.table_salary.horizontalHeaderItem(7).text())
        window.sal_report_mode.setCurrentIndex(0)
        self.assertIn("Зарплата", window.table_salary.horizontalHeaderItem(8).text())
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
        self.assertIn("Отработано", text)
        self.assertIn("Не определено", text)
        self.assertEqual(before, list(conn.iterdump()))

    def preview_pdf_fixture(self, folder):
        conn = self.seed_management_shift()
        window = self.salary_window(management_mode=True)
        source = Path(folder)/"preview.pdf"
        payroll_ui.write_pdf(source, payroll_ui.selected_reports(window, self.desktop),
                             self.desktop.DEFAULT_FONT, self.desktop.DEFAULT_FONT_BOLD)
        return conn, window, source

    def test_preview_button_opens_all_pages_without_save_picker_and_deletes_temporary_pdf(self):
        conn = self.seed_management_shift()
        before = list(conn.iterdump())
        window = self.salary_window(management_mode=True)
        captured = []
        def inspect(dialog):
            dialog.show()
            self.qt_app.processEvents()
            captured.append(dialog.source)
            text = "\n".join(p.extract_text() for p in PdfReader(dialog.source).pages)
            self.assertIn("Предварительный расчёт по текущим ставкам", text)
            self.assertIn("Fortnox", text)
            self.assertTrue(dialog.preview._image.pixmap().width() > 0)
            dialog.show_page(len(dialog._pages)-1)
            self.assertEqual(dialog._page_index, len(dialog._pages)-1)
            current = dialog.preview.transform().m11()
            dialog.preview.zoom(1.2)
            self.assertGreater(dialog.preview.transform().m11(), current)
            buttons = [b.text() for b in dialog.findChildren(self.desktop.QPushButton)]
            self.assertIn("Сохранить как…", buttons)
            self.assertIn("Печать", buttons)
            dialog.accept()
            return 1
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        window.show()
        self.qt_app.processEvents()
        button = next(b for b in window.findChildren(self.desktop.QPushButton) if b.text() == "Предпросмотр PDF")
        with patch.object(payroll_preview.PayrollPdfPreviewDialog, "exec", inspect), \
                patch.object(payroll_ui.QFileDialog, "getSaveFileName") as picker:
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        picker.assert_not_called()
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0].exists())
        self.assertEqual(before, list(conn.iterdump()))

    def test_preview_save_as_copies_identical_pdf_and_cancel_does_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            conn, window, source = self.preview_pdf_fixture(folder)
            before = list(conn.iterdump())
            dialog = payroll_preview.PayrollPdfPreviewDialog(source, Path(folder)/"suggested.pdf", window)
            try:
                target = Path(folder)/"chosen.pdf"
                with patch.object(payroll_preview.QFileDialog, "getSaveFileName", return_value=("", "")), \
                        patch.object(payroll_preview.QMessageBox, "information") as message:
                    dialog.save_as()
                message.assert_not_called()
                self.assertFalse(target.exists())
                with patch.object(payroll_preview.QFileDialog, "getSaveFileName", return_value=(str(target), "")), \
                        patch.object(payroll_preview.QMessageBox, "information") as message:
                    dialog.save_as()
                self.assertEqual(source.read_bytes(), target.read_bytes())
                message.assert_called_once()
                self.assertEqual(before, list(conn.iterdump()))
            finally:
                dialog.release_resources()

    def test_preview_save_failure_is_logged_and_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            _, window, source = self.preview_pdf_fixture(folder)
            dialog = payroll_preview.PayrollPdfPreviewDialog(source, source, window)
            try:
                with patch.object(payroll_preview.QFileDialog, "getSaveFileName", return_value=(str(source), "")), \
                        patch.object(payroll_preview.shutil, "copyfile", side_effect=PermissionError("file locked")), \
                        patch.object(payroll_preview.QMessageBox, "critical") as critical, \
                        self.assertLogs("payroll_preview", level="ERROR"):
                    dialog.save_as()
                self.assertIn("file locked", critical.call_args.args[2])
            finally:
                dialog.release_resources()

    def test_preview_print_cancel_and_pdf_printer_all_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            conn, window, source = self.preview_pdf_fixture(folder)
            before = list(conn.iterdump())
            dialog = payroll_preview.PayrollPdfPreviewDialog(source, source, window)
            try:
                with patch.object(payroll_preview.QPrintDialog, "exec", return_value=0), \
                        patch.object(dialog, "print_document") as print_document:
                    dialog.print_pdf()
                print_document.assert_not_called()
                printer = payroll_preview.QPrinter()
                printer.setResolution(72)
                printer.setPageOrientation(payroll_preview.QPageLayout.Orientation.Landscape)
                printer.setOutputFormat(payroll_preview.QPrinter.OutputFormat.PdfFormat)
                output = Path(folder)/"print.pdf"
                printer.setOutputFileName(str(output))
                dialog.print_document(printer)
                self.assertEqual(len(PdfReader(output).pages), len(PdfReader(source).pages))
                self.assertTrue(all(p.images for p in PdfReader(output).pages))
                self.assertEqual(before, list(conn.iterdump()))
            finally:
                dialog.release_resources()

    def test_preview_system_fallback_keeps_pdf_available_after_preview_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            _, window, source = self.preview_pdf_fixture(folder)
            original = source.read_bytes()
            with patch.object(payroll_preview, "PayrollPdfPreviewDialog", side_effect=ImportError("viewer missing")), \
                    patch.object(payroll_preview.QDesktopServices, "openUrl", return_value=True) as open_url, \
                    self.assertLogs("payroll_preview", level="ERROR"):
                payroll_preview.open_preview(source, source, window)
            external = Path(open_url.call_args.args[0].toLocalFile())
            try:
                self.assertNotEqual(source, external)
                self.assertEqual(external.read_bytes(), original)
            finally:
                external.unlink()

    def test_preview_print_dialog_accepts_selected_page_and_reports_print_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            _, window, source = self.preview_pdf_fixture(folder)
            reports = payroll_ui.selected_reports(window, self.desktop)
            payroll_ui.write_pdf(source, reports*60, self.desktop.DEFAULT_FONT, self.desktop.DEFAULT_FONT_BOLD)
            dialog = payroll_preview.PayrollPdfPreviewDialog(source, source, window)
            output = Path(folder)/"second-page.pdf"
            def accept_print(print_dialog):
                printer = print_dialog.printer()
                printer.setOutputFormat(payroll_preview.QPrinter.OutputFormat.PdfFormat)
                printer.setOutputFileName(str(output))
                printer.setResolution(72)
                printer.setFromTo(2, 2)
                return payroll_preview.QDialog.DialogCode.Accepted
            try:
                with patch.object(payroll_preview.QPrintDialog, "exec", accept_print):
                    dialog.print_pdf()
                self.assertGreaterEqual(len(PdfReader(source).pages), 2)
                self.assertEqual(len(PdfReader(output).pages), 1)
                with patch.object(payroll_preview.QPrintDialog, "exec", return_value=1), \
                        patch.object(dialog, "print_document", side_effect=RuntimeError("printer offline")), \
                        patch.object(payroll_preview.QMessageBox, "critical") as critical, \
                        self.assertLogs("payroll_preview", level="ERROR"):
                    dialog.print_pdf()
                self.assertIn("printer offline", critical.call_args.args[2])
            finally:
                dialog.release_resources()

    def test_preview_keeps_unknown_ob_warning_and_does_not_confirm_history(self):
        conn = self.seed_management_shift()
        conn.execute("UPDATE shifts SET start_time=NULL,end_time=NULL WHERE sync_id='management-one'")
        conn.commit()
        before = list(conn.iterdump())
        window = self.salary_window(management_mode=True)
        def inspect(dialog):
            text = "\n".join(p.extract_text() for p in PdfReader(dialog.source).pages)
            self.assertIn("Предварительный расчёт", text)
            self.assertIn("OB не определён", text)
            self.assertIn("нет точных временных данных", text)
            self.assertIn("Не определено", text)
            return 0
        with patch.object(payroll_preview.PayrollPdfPreviewDialog, "exec", inspect), \
                patch.object(payroll_ui.QFileDialog, "getSaveFileName") as picker:
            payroll_ui.preview(window, self.desktop)
        picker.assert_not_called()
        self.assertEqual(before, list(conn.iterdump()))

    def test_preview_fallback_failure_shows_clear_error(self):
        self.seed_management_shift()
        window = self.salary_window(management_mode=True)
        with patch.object(payroll_preview, "PayrollPdfPreviewDialog", side_effect=RuntimeError("render failed")), \
                patch.object(payroll_preview.QDesktopServices, "openUrl", return_value=False) as open_url, \
                patch.object(payroll_ui.QMessageBox, "critical") as critical, \
                self.assertLogs(level="ERROR"):
            payroll_ui.preview(window, self.desktop)
        text = critical.call_args.args[2]
        self.assertIn("render failed", text)
        self.assertIn("Системная программа", text)
        self.assertFalse(Path(open_url.call_args.args[0].toLocalFile()).exists())

    def test_preview_generation_error_and_empty_period_are_visible(self):
        window = self.salary_window(management_mode=True)
        with patch.object(payroll_ui, "selected_reports", return_value=[]), \
                patch.object(payroll_ui.QMessageBox, "warning") as warning:
            payroll_ui.preview(window, self.desktop)
        warning.assert_called_once()
        with patch.object(payroll_ui, "selected_reports", side_effect=sqlite3.OperationalError("database unavailable")), \
                patch.object(payroll_ui.QMessageBox, "critical") as critical, \
                self.assertLogs("payroll_ui", level="ERROR"):
            payroll_ui.preview(window, self.desktop)
        self.assertIn("database unavailable", critical.call_args.args[2])

    def test_summary_employee_and_period_filter_shared_by_table_preview_and_export(self):
        conn = self.seed_management_shift()
        conn.execute("INSERT INTO employees(name,salary_rate,is_active) VALUES('Andrii Piskun',200,1)")
        conn.execute("""INSERT INTO shifts(sync_id,date,employee,company,object_name,hours,rate,entry_type,start_time,end_time)
                        VALUES('andrii','2026-10-22','Andrii Piskun','Renatur','Site',2,999,'work','10:00','12:00')""")
        conn.commit()
        before = list(conn.iterdump())
        window = self.salary_window(management_mode=True)
        window.load_salary_table_template()
        self.assertEqual(window.sal_employee_filter.currentText(), "Все сотрудники")
        self.assertEqual(window.table_salary.rowCount(), 3)
        self.assertEqual(window.table_salary.item(2, 0).text(), "ИТОГО")
        self.assertEqual(window.table_salary.item(2, 1).text(), "6")
        self.assertEqual(window.table_salary.item(2, 8).text(), "900.00")
        window.sal_employee_filter.setCurrentIndex(window.sal_employee_filter.findData("Andrii Piskun"))
        self.assertEqual(window.table_salary.rowCount(), 2)
        self.assertEqual(window.table_salary.item(0, 0).text(), "Andrii Piskun")
        self.assertEqual(window.table_salary.item(0, 1).text(), "2")
        self.assertEqual(window.table_salary.item(1, 8).text(), "400.00")
        self.assertEqual([r["employee"] for r in payroll_ui.selected_reports(window, self.desktop)], ["Andrii Piskun"])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/"selected.pdf"
            with patch.object(payroll_ui.QFileDialog, "getSaveFileName", return_value=(str(output), "")), \
                    patch.object(payroll_ui.QMessageBox, "information"):
                window.export_salary_to_pdf()
            text = "\n".join(p.extract_text() for p in PdfReader(output).pages)
            self.assertIn("Andrii Piskun", text)
            self.assertNotIn("Management Test", text)
            self.assertEqual(len(PdfReader(output).pages), 1)
            def inspect(dialog):
                preview_text = "\n".join(p.extract_text() for p in PdfReader(dialog.source).pages)
                self.assertEqual(preview_text, text)
                return 0
            with patch.object(payroll_preview.PayrollPdfPreviewDialog, "exec", inspect):
                payroll_ui.preview(window, self.desktop)
        from PyQt6.QtCore import QDate
        window.sal_date_from.setDate(QDate(2026, 10, 21))
        window.sal_date_to.setDate(QDate(2026, 10, 21))
        window.load_salary_table_template()
        self.assertEqual(window.sal_employee_filter.currentData(), "Andrii Piskun")
        self.assertEqual(payroll_ui.selected_reports(window, self.desktop), [])
        self.assertEqual(window.table_salary.rowCount(), 1)
        self.assertEqual(window.table_salary.item(0, 1).text(), "0")
        window.sal_employee_filter.setCurrentIndex(0)
        self.assertEqual(window.table_salary.item(0, 0).text(), "Management Test")
        self.assertEqual(window.table_salary.item(1, 1).text(), "4")
        window.sal_report_mode.setCurrentIndex(1)
        window.sal_employee_filter.setCurrentIndex(window.sal_employee_filter.findData("Andrii Piskun"))
        self.assertEqual(payroll_ui.selected_reports(window, self.desktop), [])
        self.assertEqual(before, list(conn.iterdump()))

    def test_summary_one_page_total_and_unknown_salary_not_partial_success(self):
        conn = self.seed_management_shift()
        window = self.salary_window(management_mode=True)
        items = payroll_ui.selected_reports(window, self.desktop)
        items = [dict(items[0], employee=f"Employee {i:02}") for i in range(12)]
        pdf = BytesIO()
        payroll_ui.write_pdf(pdf, items, self.desktop.DEFAULT_FONT, self.desktop.DEFAULT_FONT_BOLD)
        reader = PdfReader(pdf)
        self.assertEqual(len(reader.pages), 1)
        text = reader.pages[0].extract_text()
        self.assertIn("ИТОГО", text)
        self.assertIn("6,000.00", text)
        self.assertEqual(payroll_ui.summary_total(items)["worked"], 48)
        items[0] = dict(items[0], total=None)
        self.assertIsNone(payroll_ui.summary_total(items)["total"])
        pdf = BytesIO()
        payroll_ui.write_pdf(pdf, items, self.desktop.DEFAULT_FONT, self.desktop.DEFAULT_FONT_BOLD)
        self.assertIn("Не определено", PdfReader(pdf).pages[0].extract_text())
        self.assertNotIn("5,500.00", PdfReader(pdf).pages[0].extract_text())

    def test_summary_many_employees_readable_multipage_with_repeated_header_and_one_total(self):
        self.seed_management_shift()
        window = self.salary_window(management_mode=True)
        report = payroll_ui.selected_reports(window, self.desktop)[0]
        pdf = BytesIO()
        payroll_ui.write_pdf(pdf, [dict(report, employee=f"Employee {i:03}") for i in range(100)],
                             self.desktop.DEFAULT_FONT, self.desktop.DEFAULT_FONT_BOLD)
        reader = PdfReader(pdf)
        self.assertGreater(len(reader.pages), 1)
        self.assertTrue(all("ФИО" in p.extract_text() for p in reader.pages))
        text = "\n".join(p.extract_text() for p in reader.pages)
        self.assertEqual(text.count("ИТОГО"), 1)
        self.assertIn("50,000.00", text)


if __name__ == "__main__":
    unittest.main()
