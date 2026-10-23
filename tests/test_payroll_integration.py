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
        self.assertEqual(window.table_salary.item(row, 1).text(), "100.00")
        self.assertEqual(window.table_salary.item(row, 2).text(), "4")
        self.assertEqual(window.table_salary.item(row, 9).text(), "500.00")
        self.assertIn("OB1", window.table_salary.horizontalHeaderItem(4).text())
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
            self.assertIn("Всего", text)
            self.assertIn("Fortnox", text)
            self.assertNotIn("999.00", text)
            self.assertNotIn("историческая ставка", information.call_args.args[2])
        self.assertEqual(before, list(conn.iterdump()))
        window.sal_report_mode.setCurrentIndex(1)
        self.assertIn("Grundlön", window.table_salary.horizontalHeaderItem(7).text())
        window.sal_report_mode.setCurrentIndex(0)
        self.assertIn("Зарплата", window.table_salary.horizontalHeaderItem(9).text())
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
        self.assertIn("Всего", text)
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
            self.assertEqual(dialog.document.pageCount(), len(PdfReader(dialog.source).pages))
            dialog.show_page(len(dialog._pages)-1)
            self.assertEqual(dialog._page_index, len(dialog._pages)-1)
            current = dialog.preview.zoomFactor()
            dialog.zoom(1.2)
            self.assertGreater(dialog.preview.zoomFactor(), current)
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
                target = Path(folder) / "locked.pdf"
                with patch.object(payroll_preview.QFileDialog, "getSaveFileName", return_value=(str(target), "")), \
                        patch.object(payroll_preview, "QSaveFile") as save_file, \
                        patch.object(payroll_preview.QMessageBox, "critical") as critical, \
                        patch.object(payroll_preview.QMessageBox, "information") as success, \
                        self.assertLogs("payroll_preview", level="ERROR"):
                    save_file.return_value.open.return_value = False
                    save_file.return_value.errorString.return_value = "file locked"
                    dialog.save_as()
                self.assertIn("file locked", critical.call_args.args[2])
                success.assert_not_called()
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
            self.assertIn("4 ч — OB не определён, учтена только базовая оплата", text)
            self.assertIn("400.00", text)
            self.assertNotIn("Не определено", text)
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
        self.assertEqual(window.table_salary.item(2, 2).text(), "6")
        self.assertEqual(window.table_salary.item(2, 9).text(), "900.00")
        window.sal_employee_filter.setCurrentIndex(window.sal_employee_filter.findData("Andrii Piskun"))
        self.assertEqual(window.table_salary.rowCount(), 2)
        self.assertEqual(window.table_salary.item(0, 0).text(), "Andrii Piskun")
        self.assertEqual(window.table_salary.item(0, 2).text(), "2")
        self.assertEqual(window.table_salary.item(1, 9).text(), "400.00")
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
        self.assertEqual(window.table_salary.item(0, 2).text(), "0")
        window.sal_employee_filter.setCurrentIndex(0)
        self.assertEqual(window.table_salary.item(0, 0).text(), "Management Test")
        self.assertEqual(window.table_salary.item(1, 2).text(), "4")
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

    def test_summary_rate_is_actual_current_salary_rate_per_employee_and_refreshes(self):
        conn = self.seed_management_shift()
        conn.execute("INSERT INTO employees(name,salary_rate,is_active) VALUES('Andrii Piskun',245.8,1)")
        conn.execute("""INSERT INTO shifts(sync_id,date,employee,company,object_name,hours,rate,entry_type)
                      VALUES('andrii','2026-10-22','Andrii Piskun','Renatur','Site',8,999,'work')""")
        conn.commit()
        window = self.salary_window(management_mode=True)
        window.load_salary_table_template()
        self.assertIn("Ставка", window.table_salary.horizontalHeaderItem(1).text())
        items = payroll_ui.selected_reports(window, self.desktop)
        by_name = {r["employee"]: r for r in items}
        for index, report in enumerate(items):
            self.assertEqual(window.table_salary.item(index, 1).text(), f"{report['rate']:,.2f}")
            self.assertTrue(all(line["rate"] == report["rate"] for line in report["lines"]))
        self.assertEqual(by_name["Andrii Piskun"]["base"], 8*245.8)
        self.assertEqual(window.table_salary.item(len(items), 1).text(), "")
        conn.execute("UPDATE employees SET salary_rate=250 WHERE name='Andrii Piskun'")
        conn.commit()
        window.load_salary_table_template()
        self.assertEqual(window.table_salary.item(0, 1).text(), "250.00")
        self.assertEqual(payroll_ui.selected_reports(window, self.desktop)[0]["base"], 2000)
        self.assertEqual(window.table_salary.item(1, 1).text(), "100.00")
        for rate in (None, 0):
            conn.execute("UPDATE employees SET salary_rate=? WHERE name='Andrii Piskun'", (rate,))
            conn.commit()
            window.load_salary_table_template()
            self.assertEqual(window.table_salary.item(0, 1).text(), "Не задана")
            self.assertIsNone(payroll_ui.selected_reports(window, self.desktop)[0]["total"])
        conn.execute("DELETE FROM employees WHERE name='Andrii Piskun'")
        conn.commit()
        self.assertIsNone(payroll_ui.selected_reports(window, self.desktop)[0]["rate"])

    def test_summary_vector_pdf_embeds_cyrillic_and_twelve_employees_all_columns_fit(self):
        self.seed_management_shift()
        window = self.salary_window(management_mode=True)
        report = payroll_ui.selected_reports(window, self.desktop)[0]
        items = [dict(report, employee=f"Андрей Пискун {i:02}", rate=245.8,
                      absence=dict(atf=1, sick=1, vacation=1, holiday=1)) for i in range(12)]
        pdf = BytesIO()
        with patch.object(payroll_ui, "LongTable", wraps=payroll_ui.LongTable) as table:
            payroll_ui.write_pdf(pdf, items, self.desktop.DEFAULT_FONT, self.desktop.DEFAULT_FONT_BOLD)
        reader = PdfReader(pdf)
        self.assertEqual(len(reader.pages), 1)
        page = reader.pages[0]
        self.assertEqual(len(page.images), 0)
        text = page.extract_text()
        self.assertIn("Андрей Пискун 00", text)
        self.assertIn("245.80", text)
        self.assertIn("Ставка,", text)
        self.assertGreater(float(page.mediabox.width), float(page.mediabox.height))
        font = None
        text_fonts = set()
        for operands, operator in page.get_contents().operations:
            if operator == b"Tf":
                font = operands[0]
                self.assertGreaterEqual(float(operands[1]), 9)
            if operator in (b"Tj", b"TJ"):
                text_fonts.add(font)
        for name in text_fonts:
            definition = page["/Resources"]["/Font"][name].get_object()
            self.assertIn("/ToUnicode", definition)
            descriptor = definition["/FontDescriptor"].get_object()
            self.assertIn("/FontFile2", descriptor)
        widths = table.call_args.kwargs["colWidths"]
        from reportlab.pdfbase.pdfmetrics import stringWidth
        self.assertEqual(len(widths), len(payroll_ui.MANAGEMENT_HEADERS))
        for header, width in zip(payroll_ui.MANAGEMENT_HEADERS, widths):
            self.assertGreaterEqual(width-8, max(stringWidth(w, self.desktop.DEFAULT_FONT_BOLD, 9)
                                                for w in header.split()))

    def test_native_pdf_preview_renders_zoomed_text_and_hidpi_without_page_bitmap_stretch(self):
        from PyQt6.QtCore import QSize
        from PyQt6.QtPdfWidgets import QPdfView
        with tempfile.TemporaryDirectory() as folder:
            _, window, source = self.preview_pdf_fixture(folder)
            dialog = payroll_preview.PayrollPdfPreviewDialog(source, source, window)
            try:
                dialog.show()
                self.qt_app.processEvents()
                self.assertIsInstance(dialog.preview, QPdfView)
                self.assertFalse(hasattr(dialog.preview, "_image"))
                self.assertEqual(dialog.preview.pageMode(), QPdfView.PageMode.MultiPage)
                dialog.set_zoom(2)
                self.assertEqual(dialog.preview.zoomFactor(), 2)
                self.assertEqual(dialog.preview.zoomMode(), QPdfView.ZoomMode.Custom)
                size = dialog.document.pagePointSize(0)
                normal = dialog.document.render(0, QSize(round(size.width()), round(size.height())))
                hidpi = dialog.document.render(0, QSize(round(size.width()*4), round(size.height()*4)))
                self.assertFalse(hidpi.isNull())
                self.assertEqual(hidpi.width(), normal.width()*4)
                self.assertGreater(len(set(hidpi.constBits().asstring(hidpi.sizeInBytes())[::997])), 2)
                self.assertIn("Management Test", dialog.document.getAllText(0).text())
                dialog.set_zoom(1)
                self.assertEqual(dialog.preview.zoomFactor(), 1)
                dialog.show_page(0)
            finally:
                dialog.release_resources()
            source.unlink()
            self.assertFalse(source.exists())

    def test_unknown_ob_details_button_preserves_records_and_shows_saved_times_and_reason(self):
        conn = self.seed_management_shift()
        conn.execute("UPDATE shifts SET start_time=NULL,end_time=NULL WHERE sync_id='management-one'")
        conn.commit()
        before = list(conn.iterdump())
        window = self.salary_window(management_mode=True)
        window.load_salary_table_template()
        self.assertTrue(window.sal_unknown_ob_button.isEnabled())
        self.assertIn("4 ч", window.sal_unknown_ob_button.text())
        rows = payroll_ui.unknown_ob_rows(payroll_ui.selected_reports(window, self.desktop))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["start_time"], rows[0]["end_time"]), (None, None))
        seen = []
        def inspect(dialog):
            table = dialog.findChild(payroll_ui.QTableWidget, "payrollUnknownObTable")
            self.assertEqual(table.rowCount(), 1)
            self.assertEqual(table.item(0, 0).text(), "Management Test")
            self.assertEqual(table.item(0, 1).text(), "2026-10-21")
            self.assertEqual(table.item(0, 3).text(), "4")
            self.assertEqual(table.item(0, 4).text(), "Не сохранено")
            self.assertEqual(table.item(0, 5).text(), "Не сохранено")
            self.assertEqual(table.item(0, 6).text(), "Отсутствуют")
            self.assertIn("начала и окончания", table.item(0, 7).text())
            self.assertEqual(table.item(0, 8).text(), "management-one")
            self.assertFalse(table.item(0, 4).flags() & self.desktop.Qt.ItemFlag.ItemIsEditable)
            seen.append(True)
            return 0
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        window.show()
        self.qt_app.processEvents()
        with patch.object(payroll_ui.QDialog, "exec", inspect):
            QTest.mouseClick(window.sal_unknown_ob_button, Qt.MouseButton.LeftButton)
        self.assertEqual(seen, [True])
        self.assertEqual(before, list(conn.iterdump()))
        conn.execute("UPDATE shifts SET start_time='16:00',end_time='20:00'")
        conn.commit()
        window.load_salary_table_template()
        self.assertFalse(window.sal_unknown_ob_button.isEnabled())
        self.assertEqual(payroll_ui.unknown_ob_rows(payroll_ui.selected_reports(window, self.desktop)), [])

    def test_management_norm_calendar_partial_period_holidays_and_unknown_hours(self):
        conn = self.seed_management_shift()
        conn.execute("UPDATE shifts SET hours=10,start_time=NULL,end_time=NULL,date='2026-12-24'")
        conn.commit()
        window = self.salary_window(management_mode=True)
        from PyQt6.QtCore import QDate
        for start, end, expected in (("2026-10-01", "2026-10-31", 176),
                                     ("2026-12-24", "2026-12-27", 8),
                                     ("2026-12-25", "2026-12-27", 0),
                                     ("2026-12-24", "2026-12-24", 8)):
            window.sal_date_from.setDate(QDate.fromString(start, "yyyy-MM-dd"))
            window.sal_date_to.setDate(QDate.fromString(end, "yyyy-MM-dd"))
            window.load_salary_table_template()
            self.assertIn(f"Норма периода: {expected:g} ч", window.sal_norm_label.text())
        report = payroll_ui.selected_reports(window, self.desktop)[0]
        self.assertEqual((report["worked"], report["norm"], report["above_norm"]), (10, 8, 2))
        self.assertEqual(report["total"], 1000)
        self.assertEqual(window.table_salary.item(0, 10).text(), "2")
        self.assertIn("10 ч — OB не определён, учтена только базовая оплата", window.payroll_history_note.text())
        self.assertEqual(self.desktop.swedish_workday_norm("2026-12-31", "2027-01-06"), 24)
        self.assertEqual(self.desktop.swedish_workday_norm("2026-04-02", "2026-04-06"), 8)

    def test_individual_norm_persists_only_for_employee_exact_period_and_never_changes_pay(self):
        conn = self.seed_management_shift()
        conn.execute("INSERT INTO employees(name,salary_rate,is_active) VALUES('Other',100,1)")
        conn.execute("""INSERT INTO shifts(sync_id,date,employee,company,object_name,hours,rate,entry_type)
                      VALUES('other','2026-10-22','Other','Renatur','Site',1,999,'work')""")
        conn.commit()
        before = list(conn.iterdump())
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(self.desktop.desktop_auth, "app_data_dir", return_value=Path(folder)):
            window = self.salary_window(management_mode=True)
            window.load_salary_table_template()
            def change(dialog):
                selector = dialog.findChild(payroll_ui.QComboBox, "payrollNormEmployee")
                selector.setCurrentText("Management Test")
                dialog.findChild(payroll_ui.QDoubleSpinBox, "payrollNormHours").setValue(2)
                button = next(b for b in dialog.findChildren(payroll_ui.QPushButton) if b.text() == "Сохранить норму")
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                return 0
            with patch.object(payroll_ui.QDialog, "exec", change):
                window.show()
                self.qt_app.processEvents()
                QTest.mouseClick(window.sal_norm_button, Qt.MouseButton.LeftButton)
            self.assertTrue(payroll_ui.norm_path(self.desktop).is_file())
            other_window = self.salary_window(management_mode=True)
            items = payroll_ui.selected_reports(other_window, self.desktop)
            first = next(r for r in items if r["employee"] == "Management Test")
            second = next(r for r in items if r["employee"] == "Other")
            self.assertEqual((first["norm"], first["above_norm"], first["total"]), (2, 2, 500))
            self.assertEqual((second["norm"], second["above_norm"], second["total"]), (176, 0, 100))
            self.assertEqual(payroll_ui.summary_total(items)["above_norm"], 2)
            pdf = BytesIO()
            payroll_ui.write_pdf(pdf, items, self.desktop.DEFAULT_FONT, self.desktop.DEFAULT_FONT_BOLD)
            text = PdfReader(pdf).pages[0].extract_text()
            self.assertIn("Сверх нормы", text)
            self.assertIn("Индивидуальная норма: Management Test: 2 ч", text)
            from PyQt6.QtCore import QDate
            other_window.sal_date_to.setDate(QDate(2026, 10, 30))
            self.assertFalse(payroll_ui.selected_reports(other_window, self.desktop)[0]["norm_override"])
            def reset(dialog):
                button = next(b for b in dialog.findChildren(payroll_ui.QPushButton) if b.text() == "Вернуть календарную")
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                return 0
            with patch.object(payroll_ui.QDialog, "exec", reset):
                payroll_ui.edit_norm(window, self.desktop)
            self.assertFalse(payroll_ui.selected_reports(window, self.desktop)[0]["norm_override"])
            self.assertEqual(before, list(conn.iterdump()))

    def test_unknown_base_only_summary_remains_one_page_for_twelve_employees(self):
        conn = self.seed_management_shift()
        conn.execute("UPDATE shifts SET start_time=NULL,end_time=NULL")
        conn.commit()
        window = self.salary_window(management_mode=True)
        report = payroll_ui.selected_reports(window, self.desktop)[0]
        items = [dict(report, employee=f"Employee {i:02}") for i in range(12)]
        pdf = BytesIO()
        payroll_ui.write_pdf(pdf, items, self.desktop.DEFAULT_FONT, self.desktop.DEFAULT_FONT_BOLD)
        reader = PdfReader(pdf)
        self.assertEqual(len(reader.pages), 1)
        text = reader.pages[0].extract_text()
        self.assertIn("4,800.00", text)
        self.assertIn("Сверх", text)
        self.assertIn("без OB за 48 ч", text)
        self.assertEqual(payroll_ui.summary_total(items)["total"], 4800)

    def test_invalid_norm_file_and_save_failure_are_visible_not_silent_defaults(self):
        self.seed_management_shift()
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(self.desktop.desktop_auth, "app_data_dir", return_value=Path(folder)):
            path = payroll_ui.norm_path(self.desktop)
            path.write_text('{"bad": -1}', encoding="utf-8")
            window = self.salary_window(management_mode=True)
            with patch.object(payroll_ui.QMessageBox, "critical") as critical, \
                    self.assertLogs("payroll_ui", level="ERROR"):
                window.load_salary_table_template()
            self.assertIn("Не удалось прочитать", critical.call_args.args[2])
            self.assertEqual(window.table_salary.rowCount(), 0)
            path.unlink()
            def fail(dialog):
                button = next(b for b in dialog.findChildren(payroll_ui.QPushButton) if b.text() == "Сохранить норму")
                button.click()
                return 0
            with patch.object(payroll_ui.QDialog, "exec", fail), \
                    patch.object(payroll_ui.QSaveFile, "open", return_value=False), \
                    patch.object(payroll_ui.QMessageBox, "critical") as critical, \
                    self.assertLogs("payroll_ui", level="ERROR"):
                payroll_ui.edit_norm(window, self.desktop)
            self.assertIn("Не удалось сохранить", critical.call_args.args[2])
            self.assertFalse(path.exists())

    def test_unknown_ob_details_follow_employee_period_and_show_existing_interval_mismatch(self):
        conn = self.seed_management_shift()
        conn.execute("UPDATE shifts SET hours=3")
        conn.execute("INSERT INTO employees(name,salary_rate,is_active) VALUES('Other',100,1)")
        conn.execute("""INSERT INTO shifts(sync_id,date,employee,company,object_name,hours,rate,entry_type)
                      VALUES('other','2026-10-22','Other','Renatur','Site',8,999,'work')""")
        conn.commit()
        window = self.salary_window(management_mode=True)
        window.load_salary_table_template()
        self.assertEqual(sum(r["hours"] for r in payroll_ui.unknown_ob_rows(
            payroll_ui.selected_reports(window, self.desktop))), 11)
        window.sal_employee_filter.setCurrentIndex(window.sal_employee_filter.findData("Management Test"))
        rows = payroll_ui.unknown_ob_rows(payroll_ui.selected_reports(window, self.desktop))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["start_time"], rows[0]["end_time"]), ("16:00", "20:00"))
        self.assertIn("перерыва", rows[0]["reason"])
        from PyQt6.QtCore import QDate
        window.sal_date_from.setDate(QDate(2026, 10, 22))
        window.sal_date_to.setDate(QDate(2026, 10, 22))
        window.load_salary_table_template()
        self.assertFalse(window.sal_unknown_ob_button.isEnabled())
        self.assertEqual(payroll_ui.unknown_ob_rows(payroll_ui.selected_reports(window, self.desktop)), [])


if __name__ == "__main__":
    unittest.main()
