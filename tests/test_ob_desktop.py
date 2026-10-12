import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PyQt6.QtCore import QDate, QTime, Qt
from PyQt6.QtWidgets import QApplication, QComboBox, QDoubleSpinBox, QFormLayout, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QTimeEdit, QWidget

import app as desktop
from migrate_ob import migrate_database


_QAPP = QApplication.instance() or QApplication([])


class ObDesktopHarness(QWidget):
    setup_hours_tab = desktop.SmartReportApp.setup_hours_tab
    update_ob_form_visibility = desktop.SmartReportApp.update_ob_form_visibility
    refresh_ob_preview = desktop.SmartReportApp.refresh_ob_preview
    save_shift = desktop.SmartReportApp.save_shift
    load_salary_table_template = desktop.SmartReportApp.load_salary_table_template
    edit_shift = desktop.SmartReportApp.edit_shift
    delete_shift = desktop.SmartReportApp.delete_shift
    sync_with_cloud = desktop.SmartReportApp.sync_with_cloud
    load_objects_for_company = desktop.SmartReportApp.load_objects_for_company


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
        self.window.emp_cb.setCurrentText("Private person")
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
        self.window.ob_start_time = QTimeEdit()
        self.window.ob_start_time.setTime(QTime(17, 0))
        self.window.ob_end_time = QTimeEdit()
        self.window.ob_end_time.setTime(QTime(21, 0))
        self.window.ob_base_rate_input = QLineEdit("100")
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
        window.ob_start_time.setTime(QTime(17, 0))
        window.ob_end_time.setTime(QTime(21, 0))
        window.record_type_cb.setCurrentIndex(window.record_type_cb.findData(desktop.RECORD_TYPE_OB))
        window.ob_base_rate_input.setText("100")

        self.assertFalse(window.ob_start_time.isHidden())
        self.assertTrue(window.hours_input.isHidden())
        self.assertIn("17:00–18:00", window.ob_preview_lbl.text())
        self.assertIn("570.00 kr", window.ob_preview_lbl.text())
        window.background_sync_with_cloud.assert_not_called()

    def test_preview_is_local_and_save_stores_manual_person_and_segments(self):
        result = desktop.calculate_work_interval("2026-10-12", "17:00", "21:00", 100)
        self.assertEqual(result["total_amount"], 570.0)
        self.window.background_sync_with_cloud.assert_not_called()

        self.window.save_shift()
        conn = self.database()
        row = conn.execute("SELECT * FROM shifts").fetchone()
        employees = [entry[0] for entry in conn.execute("SELECT name FROM employees ORDER BY name")]
        segments = conn.execute("SELECT start_time, end_time, multiplier, amount FROM ob_segments ORDER BY start_time").fetchall()
        conn.close()
        self.assertEqual(row["employee"], "Private person")
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
            fields["Время начала:"].setTime(QTime(18, 0))
            fields["Время окончания:"].setTime(QTime(20, 0))
            fields["Базовая ставка (SEK/ч):"].setValue(200)
            fields["Комментарий:"].setText("updated OB")
            return int(desktop.QDialog.DialogCode.Accepted)

        with patch.object(desktop.QDialog, "exec", accept_edited_dialog):
            self.window.edit_shift()

        conn = self.database()
        rows = conn.execute("SELECT id, sync_id, hours, comment, entry_type, start_time, end_time, ob_base_rate FROM shifts ORDER BY id").fetchall()
        segment_rows = conn.execute("SELECT hours, multiplier, amount FROM ob_segments WHERE shift_id = ?", (existing["sync_id"],)).fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(tuple(rows[0]), (existing["id"], existing["sync_id"], 2.0, "updated OB", "ob", "18:00", "20:00", 200.0))
        self.assertEqual(tuple(rows[1]), (neighbor_id, "work-neighbor", 8.0, "neighbor", "work", None, None, None))
        self.assertEqual([tuple(row) for row in segment_rows], [(2.0, 1.5, 600.0)])

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

    def test_ob_amount_is_separate_from_existing_salary_formula(self):
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
        self.window.table_salary = QTableWidget(0, 10)
        self.window.sal_summary_hours_lbl = QLabel()
        self.window.sal_summary_atf_lbl = QLabel()
        self.window.sal_summary_sick_lbl = QLabel()
        self.window.sal_summary_ob_lbl = QLabel()
        self.window.sal_summary_total_lbl = QLabel()
        self.window.on_salary_cell_changed = Mock()
        self.window.load_salary_table_template()

        self.assertEqual(self.window.table_salary.item(0, 1).text(), "8.0")
        self.assertEqual(self.window.table_salary.item(0, 8).text(), "4.00")
        self.assertEqual(self.window.table_salary.item(0, 9).text(), "570.00")
        self.assertEqual(self.window.table_salary.item(0, 7).text(), "632.26")
        self.assertIn("570.00 kr", self.window.sal_summary_ob_lbl.text())

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


if __name__ == "__main__":
    unittest.main()