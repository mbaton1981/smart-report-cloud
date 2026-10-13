import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PyQt6.QtWidgets import QApplication, QDialog, QLineEdit, QWidget

import app as desktop
from expense_documents import apply_expense_documents_migration, record_document_payment, save_supplier_document


_QAPP = QApplication.instance() or QApplication([])


class ExpenseHarness(QWidget):
    setup_expenses_tab = desktop.SmartReportApp.setup_expenses_tab
    _format_currency_totals = desktop.SmartReportApp._format_currency_totals
    load_expense_document_filters = desktop.SmartReportApp.load_expense_document_filters
    load_expense_documents = desktop.SmartReportApp.load_expense_documents
    _expense_object_candidates = desktop.SmartReportApp._expense_object_candidates
    upload_expense_document = desktop.SmartReportApp.upload_expense_document
    open_manual_document_dialog = desktop.SmartReportApp.open_manual_document_dialog
    review_selected_document = desktop.SmartReportApp.review_selected_document
    _expense_document_review_dialog = desktop.SmartReportApp._expense_document_review_dialog
    _update_supplier_document_record = desktop.SmartReportApp._update_supplier_document_record
    open_selected_document_original = desktop.SmartReportApp.open_selected_document_original
    record_selected_document_payment = desktop.SmartReportApp.record_selected_document_payment
    apply_expense_period_preset = desktop.SmartReportApp.apply_expense_period_preset
    on_expense_date_changed = desktop.SmartReportApp.on_expense_date_changed
    open_add_expense_dialog = desktop.SmartReportApp.open_add_expense_dialog
    apply_expense_journal_filters = desktop.SmartReportApp.apply_expense_journal_filters
    show_expense_details = desktop.SmartReportApp.show_expense_details
    edit_company_expense = desktop.SmartReportApp.edit_company_expense
    update_company_expense = desktop.SmartReportApp.update_company_expense
    auto_calculate_taxes_and_fora = desktop.SmartReportApp.auto_calculate_taxes_and_fora
    add_company_expense = desktop.SmartReportApp.add_company_expense
    load_expenses_table = desktop.SmartReportApp.load_expenses_table
    delete_company_expense = desktop.SmartReportApp.delete_company_expense
    setup_object_expenses_tab = desktop.SmartReportApp.setup_object_expenses_tab
    load_object_expense_objects = desktop.SmartReportApp.load_object_expense_objects
    load_object_expense_overview = desktop.SmartReportApp.load_object_expense_overview
    _set_object_expenses_period_enabled = desktop.SmartReportApp._set_object_expenses_period_enabled
    _create_object_summary_frame = desktop.SmartReportApp._create_object_summary_frame
    _format_object_currency_totals = desktop.SmartReportApp._format_object_currency_totals


class FakeButton:
    def __init__(self, text):
        self.text = text


class FakeMessageBox:
    class Icon:
        Information = 1

    class ButtonRole:
        AcceptRole = 1
        RejectRole = 0

    class StandardButton:
        Yes = 1
        No = 0

    instances = []
    information_messages = []
    warning_messages = []
    question_messages = []
    accept_save = False

    def __init__(self, *_args):
        self.buttons = []
        self.message = ""
        self.__class__.instances.append(self)

    def setWindowTitle(self, _title):
        pass

    def setIcon(self, _icon):
        pass

    def setText(self, text):
        self.message = text

    def addButton(self, text, _role):
        button = FakeButton(text)
        self.buttons.append(button)
        return button

    def exec(self):
        return 1

    def clickedButton(self):
        return self.buttons[0] if self.accept_save else None

    @classmethod
    def information(cls, *args):
        cls.information_messages.append(args)

    @classmethod
    def warning(cls, *args):
        cls.warning_messages.append(args)

    @classmethod
    def question(cls, *args):
        cls.question_messages.append(args)
        return cls.StandardButton.Yes

    @classmethod
    def reset(cls):
        cls.instances.clear()
        cls.information_messages.clear()
        cls.warning_messages.clear()
        cls.question_messages.clear()
        cls.accept_save = False


class ExpenseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temp_dir.name) / "expenses.sqlite")
        self._create_database()
        self.db_patch = patch.object(desktop, "get_db_connection", side_effect=self._connect)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.message_patch = patch.object(desktop, "QMessageBox", FakeMessageBox)
        self.message_patch.start()
        self.addCleanup(self.message_patch.stop)
        FakeMessageBox.reset()
        self.window = ExpenseHarness()
        self.window.tab_expenses = QWidget()
        self.window.setup_expenses_tab()
        self.window.background_sync_with_cloud = Mock()
        self.window.exp_date_from.setText("2026-10-01")
        self.window.exp_date_to.setText("2026-10-31")
        self.window.load_expenses_table()

    def _connect(self):
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _create_database(self):
        conn = sqlite3.connect(self.database_path)
        conn.executescript("""
            CREATE TABLE employees (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, salary_rate REAL DEFAULT 0
            );
            CREATE TABLE shifts (
                id INTEGER PRIMARY KEY, date TEXT NOT NULL, employee TEXT NOT NULL,
                hours REAL NOT NULL, entry_type TEXT NOT NULL DEFAULT 'work'
            );
            CREATE TABLE objects (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, markning TEXT, company TEXT
            );
            INSERT INTO objects (id, name, markning, company)
                VALUES (1, 'North Street Roof', 'NS-42', 'Sample customer');
            CREATE TABLE company_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
                category TEXT NOT NULL, description TEXT, amount REAL NOT NULL
            );
            INSERT INTO employees (id, name, salary_rate) VALUES (1, 'Sample worker', 100);
        """)
        conn.commit()
        conn.close()
        apply_expense_documents_migration(self.database_path, Path(self.temp_dir.name) / "backups")

    def _insert_expense(self, expense_date, category, description, amount):
        conn = self._connect()
        cursor = conn.execute(
            "INSERT INTO company_expenses (date, category, description, amount) VALUES (?, ?, ?, ?)",
            (expense_date, category, description, amount),
        )
        conn.commit()
        expense_id = cursor.lastrowid
        conn.close()
        return expense_id

    def _insert_shift(self, entry_type, hours):
        conn = self._connect()
        conn.execute(
            "INSERT INTO shifts (date, employee, hours, entry_type) VALUES (?, ?, ?, ?)",
            ("2026-10-12", "Sample worker", hours, entry_type),
        )
        conn.commit()
        conn.close()

    def test_selected_period_separates_manual_estimates_and_legacy_reviews(self):
        self._insert_expense("2026-10-05", "Tools", "Manual A", 100)
        self._insert_expense("2026-11-01", "Tools", "Outside", 900)
        fora_category = "FORA и страхование рабочих (Kollektivavtal)"
        employer_category = "Налоги компании (Arbetsgivareavgift 31.42%)"
        fora_description = "Авторасчет FORA за период 2026-10-01 - 2026-10-31"
        employer_description = "Авторасчет социального налога за период 2026-10-01 - 2026-10-31"
        self._insert_expense("2026-10-31", fora_category, fora_description, 10)
        self._insert_expense("2026-10-31", fora_category, fora_description, 20)
        self._insert_expense("2026-10-31", employer_category, employer_description, 30)
        self._insert_expense("2026-10-31", employer_category, employer_description, 40)

        self.window.load_expenses_table()
        self.assertEqual(self.window.exp_period_label.text(), "Выбранный период: 2026-10-01 — 2026-10-31")
        self.assertEqual(self.window.table_expenses.rowCount(), 5)
        self.assertEqual(self.window.exp_summary_expenses_lbl.text(), "100.00 SEK")
        self.assertEqual(self.window.exp_summary_fora_lbl.text(), "0.00 SEK")
        self.assertEqual(self.window.exp_summary_employer_lbl.text(), "0.00 SEK")
        self.assertEqual(self.window.exp_summary_review_lbl.text(), "4 шт.")
        for row in range(self.window.table_expenses.rowCount()):
            if self.window.table_expenses.item(row, 2).text() in (fora_category, employer_category):
                self.assertIn("Требует проверки", self.window.table_expenses.item(row, 5).text())

        self.window.exp_date_from.setText("2026-11-01")
        self.window.exp_date_to.setText("2026-11-30")
        self.window.load_expenses_table()
        self.assertEqual(self.window.table_expenses.rowCount(), 1)
        self.assertEqual(self.window.exp_summary_expenses_lbl.text(), "900.00 SEK")
        self.assertEqual(self.window.exp_summary_review_lbl.text(), "0 шт.")

    def test_auto_estimate_discloses_current_rate_excludes_ob_and_is_idempotent(self):
        self._insert_shift("work", 8)
        self._insert_shift("ob", 5)

        self.window.auto_calculate_taxes_and_fora()
        preview = FakeMessageBox.instances[-1].message
        self.assertIn("2026-10-01 — 2026-10-31", preview)
        self.assertIn("800.00 SEK", preview)
        self.assertIn("Sample worker: 8.00 ч × 100.00 SEK/ч", preview)
        self.assertIn("текущие ставки профиля", preview)
        self.assertIn("OB исключён", preview)
        self.assertIn("5.20%", preview)
        self.assertIn("31.42%", preview)
        self.assertIn("292.96 SEK", preview)
        self.assertIn("не подтверждённое налоговое обязательство", preview)
        conn = self._connect()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM company_expenses").fetchone()[0], 0)
        conn.close()

        FakeMessageBox.accept_save = True
        self.window.auto_calculate_taxes_and_fora()
        conn = self._connect()
        saved = conn.execute("SELECT amount, description FROM company_expenses").fetchall()
        self.assertEqual(len(saved), 2)
        self.assertAlmostEqual(sum(row["amount"] for row in saved), 292.96)
        self.assertTrue(all("employees.salary_rate" in row["description"] for row in saved))
        self.assertTrue(all("не подтверждены как обязательство" in row["description"] for row in saved))
        conn.close()
        self.assertEqual(self.window.exp_summary_fora_lbl.text(), "41.60 SEK")
        self.assertEqual(self.window.exp_summary_employer_lbl.text(), "251.36 SEK")

        self.window.auto_calculate_taxes_and_fora()
        conn = self._connect()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM company_expenses").fetchone()[0], 2)
        conn.close()
        self.assertIn("уже сохранены", FakeMessageBox.information_messages[-1][2])

    def test_missing_current_salary_rate_blocks_conditional_result(self):
        self._insert_shift("work", 8)
        conn = self._connect()
        conn.execute("UPDATE employees SET salary_rate = 0 WHERE name = 'Sample worker'")
        conn.commit()
        conn.close()
        self.window.auto_calculate_taxes_and_fora()
        self.assertIn("Sample worker", FakeMessageBox.warning_messages[-1][2])
        self.assertIn("не показываю и не сохраняю", FakeMessageBox.warning_messages[-1][2])
        conn = self._connect()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM company_expenses").fetchone()[0], 0)
        conn.close()

    def test_document_register_aggregates_confirmed_values_per_currency_and_period(self):
        def save_document(number, currency, net, vat, gross, status, doc_date="2026-10-10", payment_status="unknown"):
            fields = {
                "supplier_name": f"Supplier {number}", "supplier_org_number": None, "supplier_vat_number": None,
                "document_type": "invoice", "document_date": doc_date, "due_date": "2026-11-10",
                "invoice_number": number, "payment_reference": None, "currency_code": currency,
                "net_amount": net, "vat_amount": vat, "gross_amount": gross, "amount_due": gross,
                "expense_category": "Materials", "reverse_charge": False, "vat_lines": [],
                "raw_text": "test", "review_reasons": [], "payment_status": payment_status,
            }
            conn = self._connect()
            document_id = save_supplier_document(
                conn, self.temp_dir.name, fields, [],
                verification_status=status,
            )
            conn.close()
            return document_id

        confirmed_id = save_document("SEK-1", "SEK", "100.00", "25.00", "125.00", "confirmed", payment_status="unpaid")
        save_document("EUR-1", "EUR", "50.00", "0.00", "50.00", "confirmed")
        save_document("DRAFT-1", "SEK", "300.00", "75.00", "375.00", "requires_review")
        save_document("OUTSIDE-1", "SEK", "10.00", "2.50", "12.50", "confirmed", doc_date="2026-11-02")

        self.window.load_expense_documents()
        self.assertEqual(self.window.table_supplier_documents.rowCount(), 3)
        self.assertEqual(self.window.exp_docs_net_lbl.text(), "EUR 50.00 | SEK 100.00")
        self.assertEqual(self.window.exp_docs_vat_lbl.text(), "EUR 0.00 | SEK 25.00")
        self.assertEqual(self.window.exp_docs_gross_lbl.text(), "EUR 50.00 | SEK 125.00")
        self.assertEqual(self.window.exp_docs_unpaid_lbl.text(), "SEK 125.00")
        self.assertEqual(self.window.exp_docs_review_lbl.text(), "1")

        conn = self._connect()
        self.assertEqual(record_document_payment(conn, confirmed_id, "2026-10-20", "40.00"), "partially_paid")
        conn.close()
        self.window.load_expense_documents()
        self.assertEqual(self.window.exp_docs_unpaid_lbl.text(), "SEK 85.00")

        self.window.load_expense_document_filters()
        self.window.exp_filter_supplier.setCurrentText("Supplier EUR-1")
        self.window.load_expense_documents()
        self.assertEqual(self.window.table_supplier_documents.rowCount(), 1)
        self.assertEqual(self.window.exp_docs_gross_lbl.text(), "EUR 50.00")

    def test_draft_can_be_manually_corrected_and_original_moves_to_object(self):
        source = Path(self.temp_dir.name) / "manual-review.pdf"
        source.write_bytes(b"synthetic receipt for manual correction")
        fields = {
            "supplier_name": None, "supplier_org_number": None, "supplier_vat_number": None,
            "document_type": "invoice", "document_date": None, "due_date": None,
            "invoice_number": None, "payment_reference": None, "currency_code": "UNKNOWN",
            "net_amount": None, "vat_amount": None, "gross_amount": None, "amount_due": None,
            "expense_category": "Materials", "reverse_charge": False, "vat_lines": [],
            "document_comment": "Manual review notes",
            "raw_text": "unreadable", "review_reasons": ["manual entry"], "payment_status": "unknown",
        }
        conn = self._connect()
        document_id = save_supplier_document(
            conn, Path(self.temp_dir.name) / "documents", fields, [], source_path=source,
            verification_status="draft",
        )
        old_path = Path(conn.execute(
            "SELECT stored_path FROM supplier_documents WHERE id = ?", (document_id,)
        ).fetchone()[0])
        conn.close()
        self.assertIn("unallocated", str(old_path))

        fields.update({
            "supplier_name": "New Supplier AB", "supplier_org_number": "556123-4567",
            "document_date": "2026-10-10", "invoice_number": "MANUAL-1",
            "currency_code": "SEK", "net_amount": "100.00", "vat_amount": "25.00",
            "gross_amount": "125.00", "amount_due": "125.00", "review_reasons": [],
            "document_comment": "Manually verified receipt",
            "vat_lines": [{"rate": "25", "taxable_base": "100.00", "amount": "25.00"}],
        })
        allocations = [{
            "object_id": 1, "net_amount": "100.00", "vat_amount": "25.00",
            "gross_amount": "125.00", "note": "manual correction",
        }]
        document_root = Path(self.temp_dir.name) / "documents"
        update_conn = self._connect()
        self.addCleanup(update_conn.close)
        with patch.object(desktop, "document_storage_root", return_value=document_root):
            saved_id = self.window._update_supplier_document_record(
                update_conn, document_id, None, fields, allocations, "confirmed", False
            )
        self.assertEqual(saved_id, document_id)
        self.assertFalse(old_path.exists())
        conn = self._connect()
        self.addCleanup(conn.close)
        document = conn.execute("SELECT * FROM supplier_documents WHERE id = ?", (document_id,)).fetchone()
        self.assertEqual(document["supplier_name"], "New Supplier AB")
        self.assertEqual(document["document_comment"], "Manually verified receipt")
        self.assertEqual(document["verification_status"], "confirmed")
        self.assertIn("North_Street_Roof", document["stored_path"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM supplier_documents").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT allocated_gross FROM supplier_document_allocations WHERE document_id = ?", (document_id,)).fetchone()[0], "125.00")

    def test_object_tab_keeps_lifetime_and_period_totals_separate(self):
        def save_for_object(number, category, doc_date, currency, net, vat, gross, status="confirmed"):
            fields = {
                "supplier_name": f"Object supplier {number}", "supplier_org_number": None,
                "supplier_vat_number": None, "document_type": "invoice", "document_date": doc_date,
                "due_date": "2026-11-10", "invoice_number": number, "payment_reference": None,
                "currency_code": currency, "net_amount": net, "vat_amount": vat,
                "gross_amount": gross, "amount_due": gross, "expense_category": category,
                "document_comment": "", "reverse_charge": False, "vat_lines": [],
                "raw_text": "object project synthetic document", "review_reasons": [],
                "payment_status": "unpaid" if status == "confirmed" else "unknown",
            }
            conn = self._connect()
            doc_id = save_supplier_document(
                conn, self.temp_dir.name, fields,
                [{"object_id": 1, "net_amount": net, "vat_amount": vat, "gross_amount": gross}],
                verification_status=status,
            )
            conn.close()
            return doc_id

        october_id = save_for_object("OCT-1", "Материалы", "2026-10-10", "SEK", "100.00", "25.00", "125.00")
        save_for_object("SEP-1", "Услуги", "2026-09-10", "EUR", "50.00", "0.00", "50.00")
        save_for_object("DRAFT-1", "Материалы", "2026-10-11", "SEK", "80.00", "20.00", "100.00", "draft")
        conn = self._connect()
        self.assertEqual(record_document_payment(conn, october_id, "2026-10-20", "40.00"), "partially_paid")
        conn.close()

        window = ExpenseHarness()
        window.tab_object_expenses = QWidget()
        window.setup_object_expenses_tab()
        self.assertEqual(window.object_expenses_object.currentData(), 1)
        self.assertIn("EUR 50.00", window.object_expenses_lifetime_labels["gross"].text())
        self.assertIn("SEK 125.00", window.object_expenses_lifetime_labels["gross"].text())
        self.assertIn("SEK 40.00", window.object_expenses_lifetime_labels["paid"].text())
        self.assertIn("SEK 85.00", window.object_expenses_lifetime_labels["remaining"].text())
        self.assertIn("SEK 125.00", window.object_expenses_lifetime_labels["materials"].text())
        self.assertIn("EUR 50.00", window.object_expenses_lifetime_labels["other"].text())
        self.assertEqual(window.object_expenses_lifetime_labels["drafts"].text(), "1")

        window.object_expenses_period_enabled.setChecked(True)
        window.object_expenses_date_from.setText("2026-10-01")
        window.object_expenses_date_to.setText("2026-10-31")
        window.load_object_expense_overview()
        self.assertIn("SEK 125.00", window.object_expenses_period_labels["gross"].text())
        self.assertNotIn("EUR", window.object_expenses_period_labels["gross"].text())
        self.assertIn("EUR 50.00", window.object_expenses_lifetime_labels["gross"].text())
        self.assertEqual(window.object_expenses_period_labels["drafts"].text(), "1")

    def test_manual_add_edit_and_delete_target_exact_id(self):
        first_id = self._insert_expense("2026-10-05", "Tools", "First", 100)
        second_id = self._insert_expense("2026-10-05", "Tools", "Second", 200)
        self.window.load_expenses_table()

        class AcceptedEditDialog(QDialog):
            def exec(self):
                amount = next(
                    field for field in self.findChildren(QLineEdit)
                    if field.placeholderText() == "Сумма в SEK"
                )
                amount.setText("125")
                return QDialog.DialogCode.Accepted

        first_row = next(
            row for row in range(self.window.table_expenses.rowCount())
            if int(self.window.table_expenses.item(row, 0).text()) == first_id
        )
        self.window.table_expenses.selectRow(first_row)
        with patch.object(desktop, "QDialog", AcceptedEditDialog):
            self.window.edit_company_expense()

        conn = self._connect()
        self.assertEqual(
            conn.execute("SELECT amount FROM company_expenses WHERE id = ?", (first_id,)).fetchone()[0], 125
        )
        self.assertEqual(
            conn.execute("SELECT amount FROM company_expenses WHERE id = ?", (second_id,)).fetchone()[0], 200
        )
        conn.close()

        first_row = next(
            row for row in range(self.window.table_expenses.rowCount())
            if int(self.window.table_expenses.item(row, 0).text()) == first_id
        )
        self.window.table_expenses.selectRow(first_row)
        self.window.delete_company_expense()
        prompt = FakeMessageBox.question_messages[-1][2]
        self.assertIn(f"#{first_id}", prompt)
        self.assertIn("2026-10-05", prompt)
        self.assertIn("Tools", prompt)
        self.assertIn("125.00 SEK", prompt)
        conn = self._connect()
        self.assertIsNone(conn.execute("SELECT id FROM company_expenses WHERE id = ?", (first_id,)).fetchone())
        self.assertIsNotNone(conn.execute("SELECT id FROM company_expenses WHERE id = ?", (second_id,)).fetchone())
        conn.close()

        self.window.add_company_expense("2026-11-02", "Tools", "Outside period", 10)
        conn = self._connect()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM company_expenses").fetchone()[0], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()