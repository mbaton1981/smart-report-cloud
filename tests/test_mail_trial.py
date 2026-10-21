import copy
import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QLineEdit, QPushButton, QLabel, QMessageBox, QWidget,
)

from desktop_ui import ReportTable
from mail_import import MailImportError, MailQueue
from mail_review import collect_review
from mail_trial import TrialStore
from mail_viewer import MailViewer


def fields():
    markers = ("P1022", "1035", "TEST-DUP", "p1034", None)
    return {
        "supplier_name": "Synthetic Seller AB", "invoice_number": "INV-1",
        "document_date": "2026-10-16", "due_date": "2026-11-15", "due_date_kind": "explicit",
        "currency_code": "SEK", "net_amount": "100.00", "vat_amount": "25.00",
        "rounding_amount": "0.50", "amount_due": "125.50", "document_type": "invoice",
        "allocation_basis": "net",
        "order_proposals": [
            {"order_id": f"ORDER-{n}", "project_code": marker, "original_marker": marker,
             "net_amount": "20.00", "vat_amount": "5.00", "ambiguous": marker is None,
             "sources": [{"page": min(n + 1, 3), "attachment_index": 0}],
             "vat_items": [{"page": min(n + 1, 3), "item": "10", "net_amount": "20.00",
                            "vat_rate": "25.00", "vat_amount": "5.00"}]}
            for n, marker in enumerate(markers)
        ],
    }


class MailTrialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "trial"
        from reportlab.pdfgen.canvas import Canvas
        buffer = BytesIO()
        pdf = Canvas(buffer)
        for index in range(3):
            pdf.drawString(40, 750, f"Synthetic invoice page {index + 1}")
            pdf.showPage()
        pdf.save()
        queue = MailQueue(self.root)
        try:
            queue.accept(buffer.getvalue(), ".pdf", {
                "uidvalidity": "1", "uid": "1", "part": 1, "message_id": "test-id",
                "sender": "sender", "subject": "subject", "received_at": "2026-10-20T12:00:00+00:00",
                "filename": "test.pdf", "content_type": "application/pdf",
            }, lambda paths: {"fields": fields(), "state": "requires_review"})
        finally:
            queue.close()

    def payload(self):
        data = fields()
        data["verification_status"] = "draft"
        data["allocations"] = [
            {**row, "object_id": 101, "destination": "object"} for row in data["order_proposals"]
        ]
        return data

    def test_draft_revisions_reopen_stale_guard_and_no_initial_data_changes(self):
        store = TrialStore(self.root)
        before = {table: store.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                  for table in ("documents", "sources", "issues")}
        originals = {p.name: p.read_bytes() for p in (self.root / "originals").iterdir()}
        first = self.payload()
        self.assertEqual(store.save(1, first), 1)
        second = copy.deepcopy(first)
        second["allocations"][1].update(destination="company", object_id=None,
                                         expense_category="Тестовые общие расходы")
        self.assertEqual(store.save(1, second, expected_revision=1), 2)
        with self.assertRaisesRegex(MailImportError, "другом окне"):
            store.save(1, first, expected_revision=1)
        store.close()
        store = TrialStore(self.root)
        try:
            draft = store.latest(1)
            self.assertEqual(draft["revision"], 2)
            self.assertEqual(draft["payload"]["allocations"][1]["destination"], "company")
            self.assertIsNone(draft["payload"]["allocations"][1]["object_id"])
            revisions = store.conn.execute(
                "SELECT payload_json FROM trial_allocation_revisions ORDER BY revision"
            ).fetchall()
            self.assertEqual(json.loads(revisions[0][0])["allocations"][1]["destination"], "object")
            self.assertEqual(len(revisions), 2)
            for table, data in before.items():
                self.assertEqual(store.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall(), data)
            self.assertEqual({p.name: p.read_bytes() for p in (self.root / "originals").iterdir()}, originals)
            report = collect_review(self.root)
            draft_view = report["documents"][0]["draft"]
            self.assertEqual(draft_view["revision"], 2)
            self.assertEqual(draft_view["allocation"]["orders"][1]["destination"], "company")
            self.assertEqual(len(report["documents"][0]["draft_history"]), 2)
        finally:
            store.close()

    def test_no_confirmation_foreign_object_or_invalid_amount_can_be_saved(self):
        store = TrialStore(self.root)
        try:
            data = self.payload()
            data["verification_status"] = "confirmed"
            with self.assertRaisesRegex(MailImportError, "подтверждение отключено"):
                store.save(1, data)
            data = self.payload()
            data["allocations"][0]["object_id"] = 999999
            with self.assertRaisesRegex(MailImportError, "тестовом каталоге"):
                store.save(1, data)
            data = self.payload()
            data["allocations"][0]["net_amount"] = "bad"
            with self.assertRaisesRegex(MailImportError, "Некорректная сумма"):
                store.save(1, data)
            self.assertIsNone(store.latest(1))
        finally:
            store.close()

    def test_desktop_order_and_tax_decisions_cannot_be_confirmed_as_expenses(self):
        from invoice_review import InvoiceReviewMixin

        class ReviewHost(QWidget, InvoiceReviewMixin):
            pass

        window = ReviewHost()
        window._expense_objects = []
        parsed = {
            "supplier_name": "Verified Supplier AB",
            "invoice_number": "INV-1",
            "document_date": "2026-10-13",
            "currency_code": "SEK",
            "net_amount": "100.00",
            "vat_amount": "25.00",
            "rounding_amount": "0.00",
            "amount_due": "125.00",
            "non_invoice_document_type": "order_confirmation",
            "raw_text": "ORDERBEKRÄFTELSE",
            "order_proposals": [{
                "order_id": "ORD-1", "original_marker": "ORD-1",
                "net_amount": "100.00", "vat_amount": "25.00",
                "destination": "company", "expense_category": "Тест",
                "sources": [], "ambiguous": False,
            }],
        }

        def inspect_dialog(dialog):
            confirm = dialog.findChild(QPushButton, "invoiceConfirm")
            self.assertFalse(confirm.isEnabled())
            dialog.reject()
            return dialog.result()

        try:
            for document_type, text in (
                ("order_confirmation", "ORDERBEKRÄFTELSE"),
                ("tax_decision", "Beslut om trängselskatt"),
            ):
                with self.subTest(document_type=document_type):
                    candidate = {
                        **parsed, "non_invoice_document_type": document_type,
                        "raw_text": text, "document_type": document_type,
                    }
                    with patch.object(QDialog, "exec", inspect_dialog):
                        self.assertFalse(window._review_net_invoice({}, [], candidate))
        finally:
            window.close()

    def test_reprocess_requires_ui_confirmation_and_updates_only_recognition(self):
        viewer = MailViewer(self.root)
        try:
            before = viewer.store.conn.execute(
                "SELECT result_json FROM documents WHERE id=1"
            ).fetchone()["result_json"]
            original_hash = viewer.store.conn.execute(
                "SELECT sha256 FROM documents WHERE id=1"
            ).fetchone()["sha256"]
            source_rows = viewer.store.conn.execute(
                "SELECT COUNT(*) FROM sources WHERE document_id=1"
            ).fetchone()[0]
            viewer.table.selectRow(0)
            with patch(
                "mail_viewer.QMessageBox.question",
                side_effect=[QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes],
            ) as confirm, patch("mail_viewer.QMessageBox.information"):
                viewer.reprocess_selected()
                self.assertEqual(
                    viewer.store.conn.execute(
                        "SELECT result_json FROM documents WHERE id=1"
                    ).fetchone()["result_json"],
                    before,
                )
                viewer.reprocess_selected()
            self.assertEqual(confirm.call_count, 2)
            updated = json.loads(viewer.store.conn.execute(
                "SELECT result_json FROM documents WHERE id=1"
            ).fetchone()["result_json"])
            self.assertEqual(updated["methods"], ["текст PDF"])
            self.assertEqual(updated["page_count"], 3)
            self.assertIn("Synthetic invoice page 1", updated["fields"]["raw_text"])
            self.assertEqual(viewer.store.conn.execute(
                "SELECT sha256 FROM documents WHERE id=1"
            ).fetchone()["sha256"], original_hash)
            self.assertEqual(viewer.store.conn.execute(
                "SELECT COUNT(*) FROM sources WHERE document_id=1"
            ).fetchone()[0], source_rows)
            self.assertIsNone(viewer.store.latest(1))
        finally:
            viewer.close()

    def test_existing_form_exact_only_selection_edit_save_reopen_and_rounding(self):
        viewer = MailViewer(self.root)
        original_method = viewer._review_net_invoice.__func__
        from invoice_review import InvoiceReviewMixin
        self.assertIs(original_method, InvoiceReviewMixin._review_net_invoice)

        def first_open(dialog):
            table = dialog.findChild(ReportTable, "invoiceAllocations")
            self.assertEqual(table.rowCount(), 5)
            self.assertEqual([table.cellWidget(i, 3).currentData() for i in range(5)],
                             [101, None, None, 102, None])
            self.assertFalse(dialog.findChild(QPushButton, "invoiceConfirm").isEnabled())
            for i in (1, 2):
                combo = table.cellWidget(i, 3)
                combo.setCurrentIndex(combo.findData(103 if i == 1 else 104))
                dest = table.cellWidget(i, 2)
                dest.setCurrentIndex(dest.findData("object"))
            # Use the existing company destination and category editor.
            dest = table.cellWidget(4, 2)
            dest.setCurrentIndex(dest.findData("company"))
            table.cellWidget(4, 4).setText("Тестовые административные расходы")
            from PyQt6.QtWidgets import QCheckBox
            dialog.findChild(QCheckBox, "invoiceAmbiguityReviewed").setChecked(True)
            # Editing an amount invalidates its VAT/item evidence, not a guessed recomputation.
            table.item(0, 1).setText("19.00")
            table.item(0, 1).setText("20.00")
            self.assertIn("осталось 0.00", dialog.findChild(QLabel, "invoiceDistribution").text())
            self.assertIn("Назначения выбраны", dialog.findChild(QLabel, "invoiceIssues").text())
            self.assertFalse(dialog.findChild(QPushButton, "invoiceConfirm").isEnabled())
            dialog.findChild(QPushButton, "invoiceSaveDraft").click()
            return dialog.result()

        try:
            with patch("mail_viewer.recognize_invoice", return_value={"fields": fields(), "page_count": 3}), \
                    patch.object(QDialog, "exec", first_open):
                self.assertTrue(viewer.open_document(1))
        finally:
            viewer.close()

        reopened = MailViewer(self.root)
        def second_open(dialog):
            table = dialog.findChild(ReportTable, "invoiceAllocations")
            self.assertEqual([table.cellWidget(i, 3).currentData() for i in range(4)],
                             [101, 103, 104, 102])
            self.assertEqual(table.cellWidget(4, 2).currentData(), "company")
            self.assertEqual(table.cellWidget(4, 4).text(), "Тестовые административные расходы")
            self.assertEqual(dialog.findChild(QLineEdit, "invoice_rounding_amount").text(), "0.50")
            self.assertEqual(dialog.findChild(QLineEdit, "invoice_amount_due").text(), "125.50")
            self.assertFalse(dialog.findChild(QPushButton, "invoiceConfirm").isEnabled())
            dialog.reject()
            return dialog.result()
        try:
            with patch("mail_viewer.recognize_invoice", return_value={"fields": fields(), "page_count": 3}), \
                    patch.object(QDialog, "exec", second_open):
                self.assertFalse(reopened.open_document(1))
            draft = reopened.store.latest(1)["payload"]
            self.assertEqual(draft["trial_validation_issues"], [])
            self.assertEqual([r["destination"] for r in draft["allocations"]],
                             ["object", "object", "object", "object", "company"])
            self.assertEqual(reopened.store.latest(1)["revision"], 1)
        finally:
            reopened.close()

    def test_incomplete_edited_draft_keeps_unknown_vat_and_explicit_unselected_choice(self):
        viewer = MailViewer(self.root)
        def edit(dialog):
            table = dialog.findChild(ReportTable, "invoiceAllocations")
            table.item(0, 1).setText("19.00")
            destination = table.cellWidget(3, 2)
            destination.setCurrentIndex(destination.findData("unselected"))
            table.cellWidget(3, 3).setCurrentIndex(0)
            dialog.findChild(QLineEdit, "invoice_supplier_name").setText("Edited Synthetic Seller")
            self.assertIn("осталось 81.00", dialog.findChild(QLabel, "invoiceDistribution").text())
            self.assertFalse(dialog.findChild(QPushButton, "invoiceConfirm").isEnabled())
            dialog.findChild(QPushButton, "invoiceSaveDraft").click()
            return dialog.result()
        try:
            with patch("mail_viewer.recognize_invoice", return_value={"fields": fields()}), \
                    patch.object(QDialog, "exec", edit):
                self.assertTrue(viewer.open_document(1))
            draft = viewer.store.latest(1)["payload"]
            self.assertIsNone(draft["allocations"][0]["vat_amount"])
            self.assertEqual(draft["allocations"][0]["vat_items"], [])
            self.assertTrue(draft["trial_validation_issues"])
            self.assertEqual(viewer.table.item(0, 1).text(), "Edited Synthetic Seller")
        finally:
            viewer.close()
        reopened = MailViewer(self.root)
        def verify(dialog):
            table = dialog.findChild(ReportTable, "invoiceAllocations")
            self.assertEqual(table.cellWidget(3, 2).currentData(), "unselected")
            self.assertIsNone(table.cellWidget(3, 3).currentData())
            self.assertEqual(table.item(0, 1).text(), "19.00")
            dialog.reject()
            return dialog.result()
        try:
            with patch("mail_viewer.recognize_invoice", return_value={"fields": fields()}), \
                    patch.object(QDialog, "exec", verify):
                self.assertFalse(reopened.open_document(1))
        finally:
            reopened.close()

    def test_document_type_and_safe_warnings_survive_edit_save_reopen(self):
        for kind in ("receipt", "tax_decision", "order_confirmation", "proforma", "credit_note", "unclassified"):
            with self.subTest(kind=kind):
                viewer = MailViewer(self.root)
                data = fields()
                data.update(document_type=kind, vat_amount=None, amount_due=None,
                            review_reasons=["PRIVATE OCR OR MAIL CONTENT"])
                def save(dialog):
                    warning = dialog.findChild(QLabel, "invoiceTrialDocumentWarnings")
                    self.assertIsNotNone(warning)
                    self.assertIn("НДС", warning.text())
                    self.assertIn("долг", warning.text().lower())
                    self.assertNotIn("PRIVATE", warning.text())
                    self.assertFalse(dialog.findChild(QPushButton, "invoiceConfirm").isEnabled())
                    dialog.findChild(QPushButton, "invoiceSaveDraft").click()
                    return dialog.result()
                try:
                    with patch("mail_viewer.recognize_invoice", return_value={"fields": data}), \
                            patch.object(QDialog, "exec", save):
                        self.assertTrue(viewer.open_document(1))
                    draft = viewer.store.latest(1)["payload"]
                    self.assertEqual(draft["document_type"], kind)
                    self.assertEqual(draft["vat_amount"], "")
                    self.assertEqual(draft["amount_due"], "")
                    self.assertNotIn("PRIVATE", json.dumps(draft.get("mail_review_warnings")))
                    report = collect_review(self.root)
                    self.assertTrue(report["documents"][0]["draft"]["warnings"])
                finally:
                    viewer.close()
                reopened = MailViewer(self.root)
                def verify(dialog):
                    warning = dialog.findChild(QLabel, "invoiceTrialDocumentWarnings")
                    self.assertIn("НДС", warning.text())
                    self.assertEqual(dialog.findChild(QLineEdit, "invoice_vat_amount").text(), "")
                    self.assertEqual(dialog.findChild(QLineEdit, "invoice_amount_due").text(), "")
                    dialog.reject()
                    return dialog.result()
                try:
                    with patch("mail_viewer.recognize_invoice", return_value={"fields": data}), \
                            patch.object(QDialog, "exec", verify):
                        self.assertFalse(reopened.open_document(1))
                    self.assertEqual(reopened.store.latest(1)["payload"]["document_type"], kind)
                finally:
                    reopened.close()
                # Each synthetic case starts without the preceding case's saved type.
                store = TrialStore(self.root)
                with store.conn:
                    store.conn.execute("DELETE FROM trial_allocation_revisions")
                store.close()


if __name__ == "__main__":
    unittest.main()
