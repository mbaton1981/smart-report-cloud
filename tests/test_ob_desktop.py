import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PyQt6.QtCore import QDate, Qt
from PyQt6.QtWidgets import QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QWidget

import app as desktop
from migrate_ob import migrate_database


_QAPP = QApplication.instance() or QApplication([])


class ObDesktopHarness(QWidget):
    setup_hours_tab = desktop.SmartReportApp.setup_hours_tab
    setup_salary_tab = desktop.SmartReportApp.setup_salary_tab
    resize_salary_table_columns = desktop.SmartReportApp.resize_salary_table_columns
    update_ob_form_visibility = desktop.SmartReportApp.update_ob_form_visibility
    refresh_ob_preview = desktop.SmartReportApp.refresh_ob_preview
    save_shift = desktop.SmartReportApp.save_shift
    load_salary_table_template = desktop.SmartReportApp.load_salary_table_template
    export_salary_to_pdf = desktop.SmartReportApp.export_salary_to_pdf
    edit_shift = desktop.SmartReportApp.edit_shift
    delete_shift = desktop.SmartReportApp.delete_shift
    sync_with_cloud = desktop.SmartReportApp.sync_with_cloud
    load_objects_for_company = desktop.SmartReportApp.load_objects_for_company
    setup_invoice_tab = desktop.SmartReportApp.setup_invoice_tab
    setup_salary_tab = desktop.SmartReportApp.setup_salary_tab
    mark_period_as_invoiced = desktop.SmartReportApp.mark_period_as_invoiced
    unmark_period_as_invoiced = desktop.SmartReportApp.unmark_period_as_invoiced
    apply_remote_shift = desktop.SmartReportApp.apply_remote_shift
    load_shifts_history = desktop.SmartReportApp.load_shifts_history
    apply_shifts_filters = desktop.SmartReportApp.apply_shifts_filters
    load_dropdowns = desktop.SmartReportApp.load_dropdowns
    export_dagbok_to_pdf = desktop.SmartReportApp.export_dagbok_to_pdf
    load_salary_table_template = desktop.SmartReportApp.load_salary_table_template
    calculate_salary = desktop.SmartReportApp.calculate_salary
    calculate_salary_row = desktop.SmartReportApp.calculate_salary_row
    on_salary_cell_changed = desktop.SmartReportApp.on_salary_cell_changed
    export_salary_to_pdf = desktop.SmartReportApp.export_salary_to_pdf
    generate_invoice = desktop.SmartReportApp.generate_invoice
    export_invoice_to_pdf = desktop.SmartReportApp.export_invoice_to_pdf


class ObDesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temp_dir.name) / "desktop_ob.db")
        self.db_patch = patch.object(desktop, "DB_FILE", self.database_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)
        conn = sqlite3.connect(self.database_path)
        conn.executescript('''
            CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT UNIQUE, salary_rate REAL, is_active INTEGER);
            CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
            CREATE TABLE objects (
                id INTEGER PRIMARY KEY, name TEXT, markning TEXT, company TEXT,
                rate REAL DEFAULT 0, transport_rate REAL DEFAULT 0
            );
            CREATE TABLE shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, employee TEXT NOT NULL,
                company TEXT NOT NULL, object_name TEXT NOT NULL, hours REAL NOT NULL,
                rate REAL NOT NULL, transport REAL DEFAULT 0, comment TEXT,
                invoiced INTEGER DEFAULT 0, sync_id TEXT, sync_version INTEGER DEFAULT 0,
                entry_type TEXT DEFAULT 'work', work_rate_cache REAL DEFAULT 0,
                work_transport_cache REAL DEFAULT 0
            );
            CREATE TABLE shift_sync_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT UNIQUE NOT NULL,
                sync_id TEXT NOT NULL, operation TEXT NOT NULL, base_version INTEGER NOT NULL,
                payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
            );
            INSERT INTO employees VALUES (1, 'Alice', 100, 1);
            INSERT INTO companies VALUES (1, 'Privat');
            INSERT INTO objects VALUES (1, 'Bygg och renovering', 'Sthlm', 'Privat', 100, 0);
        ''')
        conn.commit()
        conn.close()
        migrate_database(self.database_path, Path(self.temp_dir.name) / "desktop_ob.pre-migration.db")

        self.window = ObDesktopHarness()
        self.window.date_input = desktop.create_date_field("2026-10-12")
        self.window.emp_cb = QComboBox()
        self.window.emp_cb.setEditable(True)
        self.window.emp_cb.addItem("Alice")
        self.window.emp_cb.setCurrentText("Alice")
        self.window.comp_cb = QComboBox()
        self.window.comp_cb.addItem("Privat")
        self.window.obj_cb = QComboBox()
        self.window.obj_cb.addItem("Bygg och renovering", userData="Bygg och renovering")
        self.window.record_type_cb = QComboBox()
        self.window.record_type_cb.addItem("Работа", desktop.RECORD_TYPE_WORK)
        self.window.record_type_cb.addItem("OB/сверхурочные", desktop.RECORD_TYPE_OB)
        self.window.record_type_cb.setCurrentIndex(1)
        self.window.hours_input = QLineEdit("23")
        self.window.rate_input = QLineEdit("99999")
        self.window.trans_input = QLineEdit("1")
        self.window.comment_input = QLineEdit("OB test")
        self.window.ob_start_time = desktop.create_half_hour_time_field("17:00")
        self.window.ob_end_time = desktop.create_half_hour_time_field("21:00")
        self.window.record_type_status_lbl = QLabel()
        self.window.background_sync_with_cloud = Mock()
        self.window.load_shifts_history = Mock()
        self.dialog_message = patch.object(desktop.QMessageBox, "information")
        self.dialog_message.start()
        self.addCleanup(self.dialog_message.stop)
        self.sync_network_patches = [
            patch.object(desktop.requests, "get", side_effect=AssertionError("unexpected network in desktop test")),
            patch.object(desktop.requests, "post", side_effect=AssertionError("unexpected network in desktop test")),
        ]
        for network_patch in self.sync_network_patches:
            network_patch.start()
            self.addCleanup(network_patch.stop)

    def database(self):
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_ob_form_shows_interval_preview_without_sync(self):
        window = ObDesktopHarness()
        window.tab_hours = QWidget()
        window.load_companies_into_combobox = lambda combo: combo.addItem("Privat")
        window.load_objects_for_company = lambda company, combo, record_type=None: combo.addItem("Object", userData="Object")
        window.reload_input_objects = Mock()
        window.load_shifts_history = Mock()
        window.apply_shifts_filters = Mock()
        window.background_sync_with_cloud = Mock()
        window.setup_hours_tab()
        window.emp_cb.addItem("Alice")
        window.emp_cb.setCurrentText("Alice")
        self.assertEqual(window.ob_start_time.count(), 48)
        self.assertEqual(window.ob_start_time.itemText(1), "00:30")
        window.ob_start_time.setCurrentText("17:00")
        window.ob_end_time.setCurrentText("21:00")
        window.record_type_cb.setCurrentIndex(window.record_type_cb.findData(desktop.RECORD_TYPE_OB))

        self.assertFalse(window.ob_start_time.isHidden())
        self.assertTrue(window.hours_input.isHidden())
        self.assertIn("17:00–18:00", window.ob_preview_lbl.text())
        self.assertIn("570.00 kr", window.ob_preview_lbl.text())
        window.background_sync_with_cloud.assert_not_called()

    def test_preview_is_local_and_save_uses_employee_profile_rate(self):
        result = desktop.calculate_work_interval("2026-10-12", "17:00", "21:00", 100)
        self.assertEqual(result["total_amount"], 570.0)
        self.window.background_sync_with_cloud.assert_not_called()

        self.window.save_shift()
        conn = self.database()
        row = conn.execute("SELECT * FROM shifts").fetchone()
        employees = [entry[0] for entry in conn.execute("SELECT name FROM employees ORDER BY name")]
        segments = conn.execute("SELECT start_time, end_time, multiplier, amount FROM ob_segments ORDER BY start_time").fetchall()
        conn.close()
        self.assertEqual(row["employee"], "Alice")
        self.assertEqual(row["hours"], 4.0)
        self.assertEqual(row["rate"], 0.0)
        self.assertEqual(row["transport"], 0.0)
        self.assertEqual((row["start_time"], row["end_time"], row["ob_base_rate"]), ("17:00", "21:00", 100.0))
        self.assertEqual(employees, ["Alice"])
        self.assertEqual([tuple(segment) for segment in segments], [
            ("17:00", "18:00", 1.0, 100.0),
            ("18:00", "20:00", 1.5, 300.0),
            ("20:00", "21:00", 1.7, 170.0),
        ])
        self.window.background_sync_with_cloud.assert_called_once()

    def test_edit_dialog_updates_same_row_and_replaces_segments(self):
        self.window.save_shift()
        conn = self.database()
        existing = conn.execute("SELECT id, sync_id FROM shifts").fetchone()
        conn.execute('''
            INSERT INTO shifts (
                date, employee, company, object_name, hours, rate, transport, comment,
                invoiced, sync_id, sync_version, entry_type, work_rate_cache, work_transport_cache
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 1, 'work', ?, ?)
        ''', ("2026-10-12", "Alice", "Privat", "Bygg och renovering", 8, 100, 0, "neighbor", "work-neighbor", 100, 0))
        conn.commit()
        neighbor_id = conn.execute("SELECT id FROM shifts WHERE sync_id = ?", ("work-neighbor",)).fetchone()[0]
        conn.close()
        self.window.table_shifts = QTableWidget(2, 11)
        self.window.table_shifts.setItem(0, 0, QTableWidgetItem(str(existing["id"])))
        self.window.table_shifts.setItem(1, 0, QTableWidgetItem(str(neighbor_id)))
        self.window.table_shifts.selectRow(0)

        original_exec = desktop.QDialog.exec

        def accept_edited_dialog(dialog):
            form = dialog.findChild(QFormLayout)
            fields = {}
            for row_index in range(form.rowCount()):
                label_item = form.itemAt(row_index, QFormLayout.ItemRole.LabelRole)
                field_item = form.itemAt(row_index, QFormLayout.ItemRole.FieldRole)
                if label_item and field_item and label_item.widget():
                    fields[label_item.widget().text()] = field_item.widget()
            fields["Тип записи:"].setCurrentIndex(fields["Тип записи:"].findData(desktop.RECORD_TYPE_OB))
            fields["Время начала:"].setCurrentText("18:00")
            fields["Время окончания:"].setCurrentText("20:00")
            fields["Комментарий:"].setText("updated OB")
            return int(desktop.QDialog.DialogCode.Accepted)

        with patch.object(desktop.QDialog, "exec", accept_edited_dialog):
            self.window.edit_shift()

        conn = self.database()
        rows = conn.execute("SELECT id, sync_id, hours, comment, entry_type, start_time, end_time, ob_base_rate FROM shifts ORDER BY id").fetchall()
        segment_rows = conn.execute("SELECT hours, multiplier, amount FROM ob_segments WHERE shift_id = ?", (existing["sync_id"],)).fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(tuple(rows[0]), (existing["id"], existing["sync_id"], 2.0, "updated OB", "ob", "18:00", "20:00", 100.0))
        self.assertEqual(tuple(rows[1]), (neighbor_id, "work-neighbor", 8.0, "neighbor", "work", None, None, None))
        self.assertEqual([tuple(row) for row in segment_rows], [(2.0, 1.5, 300.0)])

    def test_cancel_desktop_edit_keeps_existing_record_and_segments(self):
        self.window.save_shift()
        conn = self.database()
        original = tuple(conn.execute("SELECT id, sync_id, hours, comment, start_time, end_time, ob_base_rate FROM shifts").fetchone())
        original_segments = conn.execute("SELECT COUNT(*) FROM ob_segments").fetchone()[0]
        conn.close()
        self.window.table_shifts = QTableWidget(1, 11)
        self.window.table_shifts.setItem(0, 0, QTableWidgetItem(str(original[0])))
        self.window.table_shifts.selectRow(0)
        with patch.object(desktop.QDialog, "exec", return_value=int(desktop.QDialog.DialogCode.Rejected)):
            self.window.edit_shift()
        conn = self.database()
        self.assertEqual(tuple(conn.execute("SELECT id, sync_id, hours, comment, start_time, end_time, ob_base_rate FROM shifts").fetchone()), original)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ob_segments").fetchone()[0], original_segments)
        conn.close()

    def test_salary_base_includes_ob_and_employer_cost_is_separate(self):
        conn = self.database()
        conn.execute('''
            INSERT INTO shifts (
                date, employee, company, object_name, hours, rate, transport, comment,
                invoiced, sync_id, sync_version, entry_type, work_rate_cache, work_transport_cache,
                start_time, end_time, ob_base_rate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 1, ?, 0, 0, ?, ?, ?)
        ''', ("2026-10-12", "Alice", "Privat", "Bygg och renovering", 8, 100, 0, "work", "salary-work", desktop.RECORD_TYPE_WORK, None, None, None))
        ob_result = desktop.calculate_work_interval("2026-10-12", "17:00", "21:00", 100)
        conn.execute('''
            INSERT INTO shifts (
                date, employee, company, object_name, hours, rate, transport, comment,
                invoiced, sync_id, sync_version, entry_type, work_rate_cache, work_transport_cache,
                start_time, end_time, ob_base_rate
            ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, 0, ?, 1, ?, 0, 0, ?, ?, ?)
        ''', ("2026-10-12", "Alice", "Privat", "Bygg och renovering", ob_result["total_hours"], "OB", "salary-ob", desktop.RECORD_TYPE_OB, "17:00", "21:00", 100))
        desktop.replace_ob_segments(conn.cursor(), "salary-ob", ob_result["segments"])
        conn.commit()
        conn.close()

        self.window.sal_date_from = desktop.create_date_field("2026-10-01")
        self.window.sal_date_to = desktop.create_date_field("2026-10-31")
        self.window.sal_vacation_percent = QDoubleSpinBox()
        self.window.sal_vacation_percent.setValue(13.0)
        self.window.sal_tax_percent = QDoubleSpinBox()
        self.window.sal_tax_percent.setValue(30.06)
        self.window.sal_employer_fee_percent = QDoubleSpinBox()
        self.window.sal_employer_fee_percent.setValue(31.42)
        self.window.sal_atf_percent = QDoubleSpinBox()
        self.window.sal_atf_percent.setValue(0.0)
        self.window.sal_sick_percent = QDoubleSpinBox()
        self.window.sal_sick_percent.setValue(0.0)
        self.window.table_salary = QTableWidget(0, 14)
        self.window.sal_summary_hours_lbl = QLabel()
        self.window.sal_summary_ob_lbl = QLabel()
        self.window.sal_summary_net_lbl = QLabel()
        self.window.sal_summary_employer_lbl = QLabel()
        self.window.on_salary_cell_changed = Mock()
        self.window.load_salary_table_template()

        self.assertEqual(self.window.table_salary.item(0, 1).text(), "8.0")
        self.assertEqual(self.window.table_salary.item(0, 5).data(Qt.ItemDataRole.UserRole), 4.0)
        self.assertIn("dagtid", self.window.table_salary.item(0, 5).toolTip() or "")
        self.assertEqual(self.window.table_salary.item(0, 6).text(), "1370.00")
        self.assertEqual(self.window.table_salary.item(0, 7).text(), "178.10")
        self.assertEqual(self.window.table_salary.item(0, 10).text(), "465.36")
        self.assertEqual(self.window.table_salary.item(0, 11).text(), "1082.74")
        self.assertEqual(self.window.table_salary.item(0, 12).text(), "486.41")
        self.assertEqual(self.window.table_salary.item(0, 13).text(), "2034.51")
        self.assertIn("570.00 SEK", self.window.sal_summary_ob_lbl.text())

        captured_tables = []
        reportlab_long_table = desktop.LongTable

        def capture_long_table(data, **kwargs):
            table = reportlab_long_table(data, **kwargs)
            captured_tables.append(table)
            return table

        class DocumentStub:
            def __init__(self, *_args, **_kwargs):
                pass

            def build(self, _story):
                return None

        reports_path = Path(self.temp_dir.name) / "salary-reports"
        with patch.object(desktop, "REPORTS_DIR", str(reports_path)), \
             patch.object(desktop, "LongTable", side_effect=capture_long_table), \
             patch.object(desktop, "SimpleDocTemplate", DocumentStub), \
             patch.object(desktop.QMessageBox, "information"), \
             patch("os.startfile", create=True), \
             patch("subprocess.run"):
            self.window.export_salary_to_pdf()

        self.assertEqual(len(captured_tables), 1)
        salary_pdf_table = captured_tables[0]
        self.assertEqual(len(salary_pdf_table._argW), 14)
        self.assertEqual(salary_pdf_table._argW[5], 218)
        self.assertAlmostEqual(sum(salary_pdf_table._argW), 792)
        pdf_headers = [cell.getPlainText() for cell in salary_pdf_table._cellvalues[0]]
        self.assertIn("OB timmar / specifikation", pdf_headers)
        self.assertIn("Skatt (SEK)", pdf_headers)
        self.assertIn("Arbetsgivaravgifter (SEK)", pdf_headers)
        pdf_ob_cell = salary_pdf_table._cellvalues[1][5]
        self.assertIn("OB1", pdf_ob_cell.getPlainText())
        self.assertIn("OB2", pdf_ob_cell.getPlainText())
        self.assertIn("1.70", pdf_ob_cell.getPlainText())
        totals_row = salary_pdf_table._cellvalues[-1]
        self.assertEqual(totals_row[4], "")
        self.assertIn("OB totalt: 4 tim / 570.00 SEK", totals_row[5].getPlainText())

    def test_salary_tab_has_configurable_rates_ob_column_and_pdf_button(self):
        window = ObDesktopHarness()
        window.tab_salary = QWidget()
        window.setup_salary_tab()

        self.assertEqual(window.table_salary.columnCount(), 14)
        headers = [window.table_salary.horizontalHeaderItem(i).text() for i in range(14)]
        self.assertIn("OB timmar /\nspecifikation", headers)
        self.assertIn("Arbetsgivaravgifter\n(SEK)", headers)
        self.assertEqual(window.table_salary.columnWidth(0), 140)
        self.assertEqual(window.table_salary.columnWidth(1), 75)
        self.assertEqual(window.table_salary.columnWidth(4), 85)
        self.assertEqual(window.table_salary.columnWidth(5), 200)
        self.assertEqual(window.table_salary.columnWidth(7), 120)
        self.assertEqual(window.table_salary.columnWidth(12), 120)
        self.assertEqual(window.table_salary.horizontalHeader().sectionResizeMode(5), desktop.QHeaderView.ResizeMode.Interactive)
        self.assertEqual(window.table_salary.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        export_button = next(
            button for button in window.tab_salary.findChildren(QPushButton)
            if "PDF" in button.text() and "зарплат" in button.text().lower()
        )
        self.assertEqual(export_button.text(), "🖨 Экспорт зарплат в PDF")
        self.assertEqual(window.sal_vacation_percent.value(), 13.0)
        self.assertEqual(window.sal_tax_percent.value(), 30.06)
        self.assertEqual(window.sal_employer_fee_percent.value(), 31.42)
        self.assertEqual(window.sal_atf_percent.value(), 0.0)
        self.assertEqual(window.sal_sick_percent.value(), 0.0)

        window.tab_salary.resize(1800, 900)
        window.tab_salary.show()
        _QAPP.processEvents()
        window.resize_salary_table_columns()
        self.assertEqual(window.table_salary.columnWidth(5), 200)
        self.assertGreater(window.table_salary.columnWidth(0), 140)
        self.assertGreater(window.table_salary.columnWidth(13), 110)
        self.assertAlmostEqual(
            sum(window.table_salary.columnWidth(column) for column in range(14)),
            window.table_salary.viewport().width(),
            delta=2,
        )

    def test_payroll_components_apply_configured_atf_and_sick_percentages(self):
        result = desktop.calculate_payroll_components(
            work_hours=8, atf_hours=2, sick_hours=1, salary_rate=100,
            ob_amount=50, vacation_percent=13, tax_percent=30,
            employer_fee_percent=31.42, atf_percent=100, sick_percent=80,
        )
        self.assertEqual(result["base_salary"], 850.0)
        self.assertEqual(result["atf_pay"], 200.0)
        self.assertEqual(result["sick_pay"], 80.0)
        self.assertEqual(result["vacation_pay"], 110.5)
        self.assertEqual(result["tax"], 372.15)
        self.assertEqual(result["net_pay"], 868.35)

    def test_ob_outbox_requires_server_capability_before_release(self):
        self.window.save_shift()
        conn = self.database()
        cursor = conn.cursor()
        cursor.execute("SELECT sync_id, state FROM shift_sync_outbox")
        sync_id, initial_state = cursor.fetchone()
        self.assertEqual(initial_state, "blocked")
        released = desktop.compact_blocked_shift_operations(
            cursor,
            {desktop.RECORD_TYPE_WORK, desktop.RECORD_TYPE_ATF, desktop.RECORD_TYPE_SICK},
        )
        self.assertEqual(released, 0)
        self.assertEqual(conn.execute("SELECT state FROM shift_sync_outbox WHERE sync_id = ?", (sync_id,)).fetchone()[0], "blocked")

        released = desktop.compact_blocked_shift_operations(
            cursor,
            {desktop.RECORD_TYPE_WORK, desktop.RECORD_TYPE_ATF, desktop.RECORD_TYPE_SICK, desktop.RECORD_TYPE_OB},
        )
        self.assertEqual(released, 1)
        row = conn.execute("SELECT state, payload_json FROM shift_sync_outbox WHERE sync_id = ?", (sync_id,)).fetchone()
        conn.close()
        payload = json.loads(row["payload_json"])
        self.assertEqual(row["state"], "pending")
        self.assertEqual(payload["record_type"], "ob")
        self.assertEqual((payload["start_time"], payload["end_time"]), ("17:00", "21:00"))

    def test_delete_confirmation_cancel_and_confirm_affect_only_selected_ob(self):
        self.window.save_shift()
        conn = self.database()
        shift_id = conn.execute("SELECT id FROM shifts").fetchone()[0]
        conn.close()
        self.window.table_shifts = QTableWidget(1, 11)
        self.window.table_shifts.setItem(0, 0, QTableWidgetItem(str(shift_id)))
        self.window.table_shifts.selectRow(0)

        with patch.object(desktop.QMessageBox, "question", return_value=desktop.QMessageBox.StandardButton.No):
            self.window.delete_shift()
        conn = self.database()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ob_segments").fetchone()[0], 3)
        conn.close()

        with patch.object(desktop.QMessageBox, "question", return_value=desktop.QMessageBox.StandardButton.Yes):
            self.window.delete_shift()
        conn = self.database()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ob_segments").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT name FROM employees").fetchone()[0], "Alice")
        conn.close()

    def test_invoice_ob_column_and_pdf_include_segment_specification(self):
        conn = self.database()
        result = desktop.calculate_work_interval("2026-10-12", "17:00", "21:00", 100)
        sync_id = "invoice-ob-shift"
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO shifts (
                date, employee, company, object_name, hours, rate, transport, comment,
                invoiced, sync_id, sync_version, entry_type, work_rate_cache, work_transport_cache,
                start_time, end_time, ob_base_rate
            ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, 0, ?, 1, 'ob', 0, 0, ?, ?, ?)
        ''', (
            "2026-10-12", "Alice", "Privat", "Bygg och renovering",
            result["total_hours"], "", sync_id, "17:00", "21:00", 100,
        ))
        desktop.replace_ob_segments(cursor, sync_id, result["segments"])
        cursor.execute("UPDATE objects SET transport_rate = 15 WHERE name = ?", ("Bygg och renovering",))
        cursor.execute('''
            INSERT INTO shifts (
                date, employee, company, object_name, hours, rate, transport, comment,
                invoiced, sync_id, sync_version, entry_type, work_rate_cache, work_transport_cache
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 1, 'work', ?, ?)
        ''', (
            "2026-10-12", "Alice", "Privat", "Bygg och renovering", 8.0,
            100.0, 2.0, "work with transport", "invoice-work-shift", 100.0, 2.0,
        ))
        conn.commit()
        conn.close()

        self.window.inv_date_from = desktop.create_date_field("2026-10-01")
        self.window.inv_date_to = desktop.create_date_field("2026-10-31")
        self.window.inv_comp_cb = QComboBox()
        self.window.inv_comp_cb.addItem("Privat")
        self.window.inv_obj_cb = QComboBox()
        self.window.inv_obj_cb.addItem("Bygg och renovering", userData="Bygg och renovering")
        self.window.fast_pris_check = QCheckBox()
        self.window.fast_pris_input = QLineEdit()
        self.window.table_invoice = QTableWidget(0, 9)
        self.window.table_invoice.setHorizontalHeaderLabels([
            "Сотрудник", "Рабочие часы", "Ставка", "Сумма (работа)",
            "Resa (SEK)", "Итого (SEK)", "Статус", "OB часы / спецификация",
            "OB сумма отдельно",
        ])
        self.window.inv_total_hours_lbl = QLabel()
        self.window.inv_total_atf_lbl = QLabel()
        self.window.inv_total_sick_lbl = QLabel()
        self.window.inv_total_ob_lbl = QLabel()
        self.window.inv_total_sum_lbl = QLabel()
        self.window.generate_invoice()

        self.assertEqual(self.window.table_invoice.columnCount(), 9)
        invoice_headers = [
            self.window.table_invoice.horizontalHeaderItem(i).text()
            for i in range(self.window.table_invoice.columnCount())
        ]
        self.assertNotIn("ATF, ч", invoice_headers)
        self.assertNotIn("Больничный, ч", invoice_headers)
        self.assertIn("Resa (SEK)", invoice_headers)
        self.assertIn("OB часы / спецификация", invoice_headers)

        ob_cell = self.window.table_invoice.item(0, 7)
        self.assertEqual(ob_cell.text(), "4 tim · intervaller")
        self.assertIn("OB1", ob_cell.toolTip())
        self.assertIn("OB2", ob_cell.toolTip())
        self.assertEqual(self.window.table_invoice.columnCount(), 9)
        self.assertEqual(self.window.table_invoice.item(0, 4).text(), "30.00")
        self.assertEqual(self.window.table_invoice.item(0, 5).text(), "830.00")

        tab_window = ObDesktopHarness()
        tab_window.tab_invoice = QWidget()
        tab_window.load_companies_into_combobox = lambda combo: combo.addItem("Privat")
        tab_window.load_objects_for_company = lambda _company, combo: combo.addItem(
            "Bygg och renovering", userData="Bygg och renovering"
        )
        tab_window.setup_invoice_tab()
        setup_headers = [
            tab_window.table_invoice.horizontalHeaderItem(i).text()
            for i in range(tab_window.table_invoice.columnCount())
        ]
        self.assertNotIn("АТФ, ч", setup_headers)
        self.assertNotIn("Больничный, ч", setup_headers)
        self.assertEqual(
            tab_window.table_invoice.horizontalHeader().sectionResizeMode(7),
            desktop.QHeaderView.ResizeMode.Stretch,
        )

        captured_tables = []
        reportlab_table = desktop.Table

        def capture_table(data, **kwargs):
            table = reportlab_table(data, **kwargs)
            captured_tables.append(table)
            return table

        class DocumentStub:
            def __init__(self, *_args, **_kwargs):
                pass

            def build(self, _story):
                return None

        reports_path = Path(self.temp_dir.name) / "invoice-reports"
        with patch.object(desktop, "REPORTS_DIR", str(reports_path)), \
             patch.object(desktop, "Table", side_effect=capture_table), \
             patch.object(desktop, "SimpleDocTemplate", DocumentStub), \
             patch.object(desktop, "translate_to_swedish", side_effect=lambda text: text), \
             patch.object(desktop.QMessageBox, "information"), \
             patch("os.startfile", create=True), \
             patch("subprocess.run"):
            self.window.export_invoice_to_pdf()

        self.assertEqual(len(captured_tables), 1)
        invoice_table = captured_tables[0]
        self.assertEqual(len(invoice_table._argW), 8)
        self.assertEqual(invoice_table._argW[6], 170)
        header_labels = [cell.getPlainText() for cell in invoice_table._cellvalues[0]]
        self.assertNotIn("ATF h", header_labels)
        self.assertNotIn("Sjuk h", header_labels)
        self.assertIn("Resa (SEK)", header_labels)
        self.assertIn("OB h / specifikation", header_labels)
        self.assertEqual(invoice_table._cellvalues[1][4], "30.00")
        self.assertEqual(invoice_table._cellvalues[1][5], "830.00")
        pdf_ob_cell = next(
            row[6] for row in invoice_table._cellvalues
            if hasattr(row[6], "getPlainText") and "OB1" in row[6].getPlainText()
        )
        self.assertIn("OB1", pdf_ob_cell.getPlainText())
        self.assertIn("OB2", pdf_ob_cell.getPlainText())
        self.assertIn("1.50", pdf_ob_cell.getPlainText())
        self.assertIn("1.70", pdf_ob_cell.getPlainText())
        self.assertEqual(pdf_ob_cell.style.wordWrap, "CJK")
        wrapped_width, _ = invoice_table.wrap(535.28, 780)
        self.assertLessEqual(wrapped_width, 535.28)
        totals_row = invoice_table._cellvalues[-1]
        self.assertEqual(totals_row[1], "8.0")
        self.assertEqual(totals_row[3], "800.00")
        self.assertEqual(totals_row[4], "30.00")
        self.assertEqual(totals_row[5], "830.00")
        self.assertEqual(totals_row[6], "4 tim")
        self.assertFalse(any("ATF" in str(cell) or "Sjuk" in str(cell) for cell in totals_row))

    def test_apply_remote_shift_imports_and_repairs_ob_record(self):
        conn = self.database()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO shifts (
                date, employee, company, object_name, hours, rate, transport, comment,
                invoiced, sync_id, sync_version, entry_type, work_rate_cache, work_transport_cache
            ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, 0, ?, 1, 'work', 0, 0)
        ''', ("2026-10-12", "Alice", "Privat", "Bygg och renovering", 4.0, "stale work", "remote-ob-sync-1"))
        conn.commit()

        remote_payload = {
            "sync_id": "remote-ob-sync-1",
            "version": 1,
            "submission_type": "ob",
            "record_type": "ob",
            "date": "2026-10-12",
            "employee": "Alice",
            "company": "Privat",
            "object_name": "Bygg och renovering",
            "hours": 4.0,
            "rate": 0.0,
            "transport": 0.0,
            "comment": "cloud OB shift",
            "start_time": "17:00",
            "end_time": "21:00",
            "ob_base_rate": 100.0,
            "ob_segments": [
                {"segment_date": "2026-10-12", "start_time": "17:00", "end_time": "18:00", "hours": 1.0, "category": "weekday_day", "premium_percent": 0.0, "multiplier": 1.0, "base_rate": 100.0, "calculated_rate": 100.0, "amount": 100.0},
                {"segment_date": "2026-10-12", "start_time": "18:00", "end_time": "20:00", "hours": 2.0, "category": "weekday_early_evening", "premium_percent": 50.0, "multiplier": 1.5, "base_rate": 100.0, "calculated_rate": 150.0, "amount": 300.0},
                {"segment_date": "2026-10-12", "start_time": "20:00", "end_time": "21:00", "hours": 1.0, "category": "weekday_late_evening_night", "premium_percent": 70.0, "multiplier": 1.7, "base_rate": 100.0, "calculated_rate": 170.0, "amount": 170.0},
            ]
        }
        self.window.apply_remote_shift(cursor, remote_payload)
        conn.commit()

        row = cursor.execute("SELECT entry_type, start_time, end_time, ob_base_rate FROM shifts WHERE sync_id = 'remote-ob-sync-1'").fetchone()
        self.assertEqual(tuple(row), ("ob", "17:00", "21:00", 100.0))
        segments = cursor.execute("SELECT COUNT(*) FROM ob_segments WHERE shift_id = 'remote-ob-sync-1'").fetchone()[0]
        self.assertEqual(segments, 3)
        conn.close()

        self.window.table_shifts = QTableWidget()
        self.window.table_shifts.setColumnCount(13)
        self.window.filter_date_from = desktop.create_date_field("2026-10-01")
        self.window.filter_date_to = desktop.create_date_field("2026-10-31")
        self.window.filter_emp = QComboBox()
        self.window.filter_emp.addItem("-- Все сотрудники --")
        self.window.filter_comp = QComboBox()
        self.window.filter_comp.addItem("-- Все фирмы --")
        self.window.filter_obj = QComboBox()
        self.window.filter_obj.addItem("-- Все объекты --")
        self.window.filter_status = QComboBox()
        self.window.filter_status.addItem("-- Все статусы --")
        self.window.load_dropdowns = Mock()
        self.window.load_shifts_history = desktop.SmartReportApp.load_shifts_history.__get__(self.window, ObDesktopHarness)
        self.window.load_shifts_history()

        self.assertEqual(self.window.table_shifts.rowCount(), 1)
        self.assertEqual(self.window.table_shifts.item(0, 5).text(), "OB")
        self.assertIn("17:00–21:00 (4 ч)", self.window.table_shifts.item(0, 7).text())
        self.assertEqual(self.window.table_shifts.item(0, 12).text(), "570.00")

    def test_export_dagbok_to_pdf_excludes_atf_sick_and_shows_only_work_and_ob_hours(self):
        conn = self.database()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO shifts (
                date, employee, company, object_name, hours, rate, transport, comment,
                invoiced, sync_id, sync_version, entry_type, work_rate_cache, work_transport_cache
            ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, 0, ?, 1, 'work', 0, 0)
        ''', ("2026-10-10", "Alice", "Privat", "Bygg och renovering", 8.0, "normal work", "work-1"))
        cursor.execute('''
            INSERT INTO shifts (
                date, employee, company, object_name, hours, rate, transport, comment,
                invoiced, sync_id, sync_version, entry_type, work_rate_cache, work_transport_cache
            ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, 0, ?, 1, 'atf', 0, 0)
        ''', ("2026-10-11", "Alice", "Privat", "Bygg och renovering", 8.0, "internal atf", "atf-1"))
        cursor.execute('''
            INSERT INTO shifts (
                date, employee, company, object_name, hours, rate, transport, comment,
                invoiced, sync_id, sync_version, entry_type, work_rate_cache, work_transport_cache
            ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, 0, ?, 1, 'sick', 0, 0)
        ''', ("2026-10-12", "Alice", "Privat", "Bygg och renovering", 8.0, "internal sick", "sick-1"))
        cursor.execute('''
            INSERT INTO shifts (
                date, employee, company, object_name, hours, rate, transport, comment,
                invoiced, sync_id, sync_version, entry_type, work_rate_cache, work_transport_cache,
                start_time, end_time, ob_base_rate
            ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, 0, ?, 1, 'ob', 0, 0, '18:00', '21:00', 100.0)
        ''', ("2026-10-13", "Alice", "Privat", "Bygg och renovering", 3.0, "overtime work", "ob-1"))
        conn.commit()
        conn.close()

        self.window.dag_date_from = desktop.create_date_field("2026-10-01")
        self.window.dag_date_to = desktop.create_date_field("2026-10-31")
        self.window.dag_comp_cb = QComboBox()
        self.window.dag_comp_cb.addItem("Privat")
        self.window.dag_obj_cb = QComboBox()
        self.window.dag_obj_cb.addItem("Bygg och renovering", userData="Bygg och renovering")

        reports_path = Path(self.temp_dir.name) / "reports"
        with patch.object(desktop, "REPORTS_DIR", str(reports_path)), \
             patch("os.startfile", create=True) as mock_startfile, \
             patch("subprocess.run") as mock_subproc:
            self.window.export_dagbok_to_pdf()

        pdf_files = list(reports_path.glob("Arbetsdagbok_*.pdf"))
        self.assertEqual(len(pdf_files), 1)
        pdf_content = pdf_files[0].read_bytes()
        self.assertTrue(pdf_content.startswith(b"%PDF-"))
        self.assertNotIn(b"6609", pdf_content)
        self.assertNotIn(b"kr", pdf_content)
        self.assertNotIn(b"SEK", pdf_content)


if __name__ == "__main__":
    unittest.main()