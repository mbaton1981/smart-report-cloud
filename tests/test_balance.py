import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PyQt6.QtCore import QDate, Qt
from PyQt6.QtWidgets import QApplication, QPushButton, QWidget

import app as desktop_app


_QAPP = QApplication.instance() or QApplication([])


class BalanceWindowHarness:
    setup_balance_tab = desktop_app.SmartReportApp.setup_balance_tab
    load_balance_table = desktop_app.SmartReportApp.load_balance_table
    load_dropdowns = desktop_app.SmartReportApp.load_dropdowns
    add_balance_entry = desktop_app.SmartReportApp.add_balance_entry
    edit_balance_entry = desktop_app.SmartReportApp.edit_balance_entry
    update_balance_entry = desktop_app.SmartReportApp.update_balance_entry
    cancel_balance_edit = desktop_app.SmartReportApp.cancel_balance_edit
    delete_balance_entry = desktop_app.SmartReportApp.delete_balance_entry


class BalanceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temp_dir.name) / "balance_test.db")
        self.database_patch = patch.object(desktop_app, "DB_FILE", self.database_path)
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

        conn = sqlite3.connect(self.database_path)
        conn.executescript('''
            CREATE TABLE employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                salary_rate REAL DEFAULT 0.0,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE balance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                employee TEXT NOT NULL,
                description TEXT NOT NULL,
                change_amount REAL NOT NULL
            );
            INSERT INTO employees (name, salary_rate) VALUES ('Alice', 100);
            INSERT INTO employees (name, salary_rate) VALUES ('Bob', 120);
        ''')
        conn.commit()
        conn.close()

        self.network_patches = [
            patch.object(desktop_app.requests, "get", side_effect=AssertionError("network call in balance test")),
            patch.object(desktop_app.requests, "post", side_effect=AssertionError("network call in balance test")),
        ]
        for network_patch in self.network_patches:
            network_patch.start()
            self.addCleanup(network_patch.stop)
        self.information_patch = patch.object(desktop_app.QMessageBox, "information")
        self.information_message = self.information_patch.start()
        self.addCleanup(self.information_patch.stop)

        self.window = BalanceWindowHarness()
        self.window.tab_balance = QWidget()
        self.window.setup_balance_tab()
        self.window.background_sync_with_cloud = Mock()
        self.window.load_dropdowns()
        self.window.load_balance_table()

    def database(self):
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        return conn

    def set_form(self, person, amount, date="2026-10-12", description="Personal note"):
        self.window.bal_date.setDate(QDate.fromString(date, "yyyy-MM-dd"))
        self.window.bal_emp_cb.setCurrentText(person)
        self.window.bal_desc.setText(description)
        self.window.bal_amount.setText(str(amount))

    def add_record(self, person, amount, date="2026-10-12", description="Personal note"):
        self.set_form(person, amount, date, description)
        self.window.add_balance_entry()
        conn = self.database()
        row = conn.execute("SELECT id FROM balance ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        return row["id"]

    def select_balance_id(self, balance_id):
        for row_index in range(self.window.table_balance.rowCount()):
            item = self.window.table_balance.item(row_index, 0)
            if item.data(Qt.ItemDataRole.UserRole) == balance_id:
                self.window.table_balance.selectRow(row_index)
                return row_index
        self.fail(f"Balance id {balance_id} was not displayed")

    def click_edit_button(self, balance_id):
        row_index = self.select_balance_id(balance_id)
        button = self.window.table_balance.cellWidget(row_index, 4)
        self.assertIsInstance(button, QPushButton)
        button.click()

    def test_existing_employee_and_manual_person_are_balance_only(self):
        self.assertTrue(self.window.bal_emp_cb.isEditable())
        self.assertEqual(
            [self.window.bal_emp_cb.itemText(index) for index in range(self.window.bal_emp_cb.count())],
            ["Alice", "Bob"],
        )

        self.add_record("Alice", 15)
        self.add_record("Personal contact", 8)

        conn = self.database()
        balance_people = [row[0] for row in conn.execute("SELECT employee FROM balance ORDER BY id")]
        employees = [row[0] for row in conn.execute("SELECT name FROM employees ORDER BY name")]
        conn.close()
        self.assertEqual(balance_people, ["Alice", "Personal contact"])
        self.assertEqual(employees, ["Alice", "Bob"])
        self.assertEqual(self.window.bal_emp_cb.count(), 2)
        self.window.background_sync_with_cloud.assert_not_called()

    def test_dropdown_refresh_preserves_selected_employee_and_manual_name(self):
        conn = self.database()
        conn.execute("INSERT INTO employees (name, salary_rate) VALUES (?, ?)", ("Иван", 100))
        conn.commit()
        conn.close()

        self.window.load_dropdowns()
        self.window.bal_emp_cb.setCurrentText("Иван")
        self.window.load_dropdowns()
        self.assertEqual(self.window.bal_emp_cb.currentText(), "Иван")

        self.window.bal_emp_cb.setCurrentText("Мария")
        self.window.load_dropdowns()
        self.assertEqual(self.window.bal_emp_cb.currentText(), "Мария")
        self.assertEqual(
            self.window.bal_emp_cb.count(),
            3,
            "ручное имя не должно добавляться в список сотрудников",
        )

        self.set_form("Мария", 19)
        self.window.add_balance_entry()
        conn = self.database()
        self.assertEqual(
            conn.execute("SELECT employee FROM balance").fetchone()[0],
            "Мария",
        )
        self.assertEqual(
            [row[0] for row in conn.execute("SELECT name FROM employees ORDER BY name")],
            ["Alice", "Bob", "Иван"],
        )
        conn.close()
        self.window.background_sync_with_cloud.assert_not_called()

    def test_multiple_people_have_independent_totals(self):
        self.add_record("Alice", 12)
        self.add_record("Bob", 30)
        self.add_record("Alice", -2)

        totals = {
            self.window.table_bal_summary.item(row, 0).text():
            self.window.table_bal_summary.item(row, 1).text()
            for row in range(self.window.table_bal_summary.rowCount())
        }
        self.assertEqual(totals, {"Bob": "30.00 kr", "Alice": "10.00 kr"})
        self.assertEqual(self.window.total_sum_label.text(), "40.00 kr")
        self.assertEqual(self.window.table_balance.rowCount(), 3)

    def test_existing_balance_row_and_schema_are_not_migrated(self):
        conn = self.database()
        cursor = conn.execute(
            "INSERT INTO balance (date, employee, description, change_amount) VALUES (?, ?, ?, ?)",
            ("2020-01-02", "Old person", "Old note", 91.5),
        )
        legacy_id = cursor.lastrowid
        conn.commit()
        expected_columns = ["id", "date", "employee", "description", "change_amount"]
        original_row = tuple(conn.execute(
            "SELECT id, date, employee, description, change_amount FROM balance WHERE id = ?",
            (legacy_id,),
        ).fetchone())
        conn.close()

        self.window.load_balance_table()
        self.add_record("Alice", 4)

        conn = self.database()
        actual_columns = [row[1] for row in conn.execute("PRAGMA table_info(balance)")]
        actual_row = tuple(conn.execute(
            "SELECT id, date, employee, description, change_amount FROM balance WHERE id = ?",
            (legacy_id,),
        ).fetchone())
        conn.close()
        self.assertEqual(actual_columns, expected_columns)
        self.assertEqual(actual_row, original_row)

    def test_edit_updates_all_fields_without_duplicate(self):
        balance_id = self.add_record("Alice", 12, description="Initial note")
        self.click_edit_button(balance_id)

        self.window.bal_date.setDate(QDate.fromString("2026-10-05", "yyyy-MM-dd"))
        self.window.bal_emp_cb.setCurrentText("New personal contact")
        self.window.bal_desc.setText("Updated note")
        self.window.bal_amount.setText("27.5")
        self.window.add_balance_entry()

        conn = self.database()
        rows = conn.execute("SELECT id, date, employee, description, change_amount FROM balance").fetchall()
        employees = [row[0] for row in conn.execute("SELECT name FROM employees ORDER BY name")]
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            tuple(rows[0]),
            (balance_id, "2026-10-05", "New personal contact", "Updated note", 27.5),
        )
        self.assertEqual(self.window.table_balance.rowCount(), 1)
        self.assertEqual(self.window.table_balance.item(0, 1).text(), "New personal contact")
        self.assertEqual(self.window.total_sum_label.text(), "27.50 kr")
        self.assertEqual(employees, ["Alice", "Bob"])
        self.information_message.assert_called_once()
        self.window.background_sync_with_cloud.assert_not_called()

    def test_edit_selected_row_uses_database_id_and_changes_only_that_record(self):
        conn = self.database()
        conn.executemany(
            "INSERT INTO employees (name, salary_rate) VALUES (?, ?)",
            [("Иван", 100), ("Мария", 100)],
        )
        conn.commit()
        conn.close()

        self.window.load_dropdowns()
        ivan_id = self.add_record("Иван", 11, date="2026-10-10", description="Ivan note")
        maria_id = self.add_record("Мария", 22, date="2026-10-11", description="Maria note")
        manual_id = self.add_record("Частный контакт", 33, date="2026-10-12", description="Manual note")

        ivan_row = self.select_balance_id(ivan_id)
        self.window.table_balance.item(ivan_row, 1).setText("stale display value")
        self.window.bal_emp_cb.setCurrentText("wrong current selection")
        self.window.table_balance.cellWidget(ivan_row, 4).click()
        self.assertEqual(self.window.bal_emp_cb.currentText(), "Иван")
        self.assertEqual(self.window.bal_desc.text(), "Ivan note")

        self.window.bal_emp_cb.setCurrentText("Пётр")
        self.window.add_balance_entry()

        conn = self.database()
        rows = conn.execute(
            "SELECT id, date, employee, description, change_amount FROM balance ORDER BY id"
        ).fetchall()
        employees = [row[0] for row in conn.execute("SELECT name FROM employees ORDER BY name")]
        conn.close()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (ivan_id, "2026-10-10", "Пётр", "Ivan note", 11.0),
                (maria_id, "2026-10-11", "Мария", "Maria note", 22.0),
                (manual_id, "2026-10-12", "Частный контакт", "Manual note", 33.0),
            ],
        )
        self.assertEqual(employees, ["Alice", "Bob", "Иван", "Мария"])
        self.assertEqual(self.window.total_sum_label.text(), "66.00 kr")
        self.information_message.assert_called_once()
        self.window.background_sync_with_cloud.assert_not_called()

    def test_cancel_edit_does_not_change_saved_record(self):
        balance_id = self.add_record("Alice", 12, description="Saved note")
        self.click_edit_button(balance_id)
        self.window.bal_emp_cb.setCurrentText("Пётр")
        self.window.bal_amount.setText("99")
        self.window.cancel_balance_edit()

        self.assertIsNone(self.window._editing_balance_id)
        self.assertEqual(self.window.bal_emp_cb.currentText(), "")
        conn = self.database()
        row = conn.execute(
            "SELECT id, employee, description, change_amount FROM balance WHERE id = ?",
            (balance_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(tuple(row), (balance_id, "Alice", "Saved note", 12.0))
        self.assertEqual(self.window.total_sum_label.text(), "12.00 kr")
        self.window.background_sync_with_cloud.assert_not_called()

    def test_edit_missing_id_shows_clear_message(self):
        with patch.object(desktop_app.QMessageBox, "warning") as warning:
            self.window.edit_balance_entry(999)

        warning.assert_called_once()
        self.assertIn("Операция с указанным ID не найдена", warning.call_args.args[2])
        conn = self.database()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM balance").fetchone()[0], 0)
        conn.close()

    def test_delete_cancel_keeps_record_and_confirmation_delete_targets_only_id(self):
        first_id = self.add_record("Alice", 10, date="2026-10-10")
        second_id = self.add_record("Bob", 25, date="2026-10-11")
        self.select_balance_id(first_id)

        with patch.object(
            desktop_app.QMessageBox,
            "question",
            return_value=desktop_app.QMessageBox.StandardButton.No,
        ) as confirmation:
            self.window.delete_balance_entry()

        confirmation.assert_called_once()
        conn = self.database()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM balance").fetchone()[0], 2)
        conn.close()
        self.assertEqual(self.window.total_sum_label.text(), "35.00 kr")

        self.select_balance_id(first_id)
        with patch.object(
            desktop_app.QMessageBox,
            "question",
            return_value=desktop_app.QMessageBox.StandardButton.Yes,
        ) as confirmation:
            self.window.delete_balance_entry()

        confirmation.assert_called_once()
        conn = self.database()
        remaining = conn.execute("SELECT id, employee, change_amount FROM balance").fetchall()
        employees = [row[0] for row in conn.execute("SELECT name FROM employees ORDER BY name")]
        conn.close()
        self.assertEqual([tuple(row) for row in remaining], [(second_id, "Bob", 25.0)])
        self.assertEqual(employees, ["Alice", "Bob"])
        self.assertEqual(self.window.table_balance.rowCount(), 1)
        self.assertEqual(self.window.total_sum_label.text(), "25.00 kr")
        self.window.background_sync_with_cloud.assert_not_called()


if __name__ == "__main__":
    unittest.main()