import copy
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox, QPushButton, QTextEdit

from expense_documents import parse_invoice_pages
from mail_import import recognition_status
from offline_review import OfflineReviewStore, ReviewError, digest, packed
from offline_review_ui import OfflineReviewWindow
from test_mail_automation import pdf


class OfflineReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        originals = self.source / "intake" / "originals"
        originals.mkdir(parents=True)
        self.base_fields = {
            "document_type": "invoice", "raw_text": "Faktura\nInvoice number 3539",
            "supplier_name": "Test Supplier AB", "invoice_number": "3539",
            "document_date": "2026-10-21", "due_date": "2026-11-20",
            "currency_code": "SEK", "net_amount": "100.00", "vat_amount": "25.00",
            "gross_amount": "125.00", "amount_due": "125.00", "rounding_amount": None,
            "payment_reference": "003539",
        }
        self.drafts = []
        for index in range(1, 6):
            content = pdf(f"Isolated fixture {index}")
            sha = digest(content)
            (originals / (sha + ".pdf")).write_bytes(content)
            fields = copy.deepcopy(self.base_fields)
            if index == 2:
                fields.update(document_type="receipt", raw_text="Foljesedel / Kvitto\nKortbetalning",
                              invoice_number=None, amount_due=None, due_date=None,
                              field_sources={"net_amount": [{"kind": "derived_from_stated_totals"}]})
            elif index == 4:
                # Intentionally retain a complete, incorrectly typed pre-fix reminder.
                fields.update(raw_text="Nordea\nPaminnelse\nFaktura-/Avinummer\n3539")
            elif index == 5:
                fields.update(invoice_number="999", supplier_name="Another Supplier AB",
                              raw_text="Faktura\nP1019\nP1035")
            self.drafts.append({"id": index, "sha256": sha, "original": sha + ".pdf",
                                "result": {"fields": fields, "state": "requires_review"}})
        (self.source / "result.json").write_text(packed({"drafts": self.drafts}), encoding="utf-8")
        self.before = {str(path): digest(path.read_bytes()) for path in self.source.rglob("*") if path.is_file()}
        self.profile = self.root / "profile"
        self.store = OfflineReviewStore(self.profile)
        self.addCleanup(lambda: self.store.close())
        self.store.import_snapshot(self.source)

    def approved(self, document_id=1):
        _, payload = self.store.latest(document_id)
        payload.update(confirmed_type=self.store.base(document_id)["document_type"],
                       original_checked=True, object_id=1)
        return payload

    def count(self):
        return self.store.conn.execute("SELECT COUNT(*) FROM register").fetchone()[0]

    def test_snapshot_and_revision_history_leave_originals_unchanged(self):
        payload = self.approved()
        payload["payment_reference"] = "00000003539"
        self.store.save(1, payload, 0)
        payload["payment_reference"] = "000003539"
        self.store.save(1, payload, 1)
        self.store.import_snapshot(self.source)
        self.assertEqual(self.store.latest(1)[0], 2)
        self.assertEqual(len(self.store.history(1)), 3)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.before, {str(p): digest(p.read_bytes())
                                      for p in self.source.rglob("*") if p.is_file()})
        stored = self.store.conn.execute("SELECT source_json FROM documents WHERE id=1").fetchone()[0]
        self.assertEqual(json.loads(stored), self.drafts[0])

    def test_object_type_and_original_are_separate_required_confirmations(self):
        _, payload = self.store.latest(1)
        self.assertIsNone(payload["object_id"])
        self.assertIsNone(payload["confirmed_type"])
        self.assertFalse(payload["original_checked"])
        self.store.save(1, payload, 0)
        with self.assertRaises(ReviewError):
            self.store.transfer(1, 1)
        payload.update(confirmed_type="invoice", original_checked=True, object_id=99)
        self.store.save(1, payload, 1)
        with self.assertRaisesRegex(ReviewError, "объект"):
            self.store.transfer(1, 2)
        self.assertEqual(self.count(), 0)

    def test_receipt_no_invoice_number_or_debt_and_derived_provenance(self):
        payload = self.approved(2)
        status = recognition_status({"fields": self.store.base(2)}, "requires_review")
        self.assertEqual(status["outcome"], "requires_review")
        self.assertEqual(status["missing_fields"], [])
        self.assertIsNone(payload["invoice_number"])
        self.assertIn("Вычислено", self.store.provenance(2, payload)["net_amount"])
        revision = self.store.save(2, payload, 0)
        card = self.store.transfer(2, revision)
        self.assertEqual(card["debt_amount"], "0.00")
        self.assertEqual(json.loads(card["payload_json"])["amount_due"], None)
        self.assertEqual(digest(self.store.original(2).read_bytes()), card["sha256"])

    def test_receipt_cannot_create_debt(self):
        payload = self.approved(2)
        payload["amount_due"] = "125"
        self.store.save(2, payload, 0)
        with self.assertRaisesRegex(ReviewError, "не создаёт задолженность"):
            self.store.transfer(2, 1)
        self.assertEqual(self.count(), 0)

    def test_stale_nordea_invoice_cannot_transfer_even_with_overridden_type(self):
        # Simulate a profile carrying the old pre-fix base as well as source.
        with self.store.conn:
            self.store.conn.execute("UPDATE documents SET base_json=? WHERE id=4",
                                    (packed(self.drafts[3]["result"]["fields"]),))
        payload = self.approved(4)
        payload["confirmed_type"] = "invoice"
        self.store.save(4, payload, 0)
        with self.assertRaisesRegex(ReviewError, "классификация"):
            self.store.transfer(4, 1)
        self.assertEqual(self.count(), 0)

    def test_stable_card_after_lost_ack_and_reopen_and_second_connection(self):
        self.store.save(1, self.approved(), 0)
        card = self.store.transfer(1, 1)
        # Caller loses the response after durable commit.
        other = OfflineReviewStore(self.profile)
        try:
            retried = other.transfer(1, 1)
            self.assertEqual(card, retried)
            self.assertEqual(other.conn.execute("SELECT COUNT(*) FROM register").fetchone()[0], 1)
        finally:
            other.close()
        with self.assertRaisesRegex(ReviewError, "уже перенесена"):
            self.store.save(1, self.approved(), 1)

    def test_different_pdf_same_invoice_is_blocked_not_merged(self):
        self.store.save(1, self.approved(), 0)
        self.store.transfer(1, 1)
        self.store.save(3, self.approved(3), 0)
        with self.assertRaisesRegex(ReviewError, "повтор покупки"):
            self.store.transfer(3, 1)
        self.assertEqual(self.count(), 1)
        self.assertIsNone(self.store.card(3))

    def test_receipt_and_invoice_same_purchase_require_manual_review(self):
        self.store.save(1, self.approved(), 0)
        self.store.transfer(1, 1)
        self.store.save(2, self.approved(2), 0)
        with self.assertRaisesRegex(ReviewError, "Совпадают продавец"):
            self.store.transfer(2, 1)
        self.assertEqual(self.count(), 1)

    def test_old_revision_cannot_overwrite_and_invalid_money_does_not_save(self):
        self.store.save(1, self.approved(), 0)
        with self.assertRaisesRegex(ReviewError, "другом окне"):
            self.store.save(1, self.approved(), 0)
        with self.assertRaisesRegex(ReviewError, "сумма"):
            self.store.save(1, {**self.approved(), "amount_due": "2.482.00"}, 1)
        with self.assertRaisesRegex(ReviewError, "слишком велика"):
            self.store.save(1, {**self.approved(), "amount_due": "9" * 100}, 1)
        self.assertEqual(self.store.latest(1)[0], 1)

    def test_invalid_totals_and_date_and_unknown_currency_block_transfer(self):
        payload = {**self.approved(), "amount_due": "126", "document_date": "2026-02-30",
                   "currency_code": "UNKNOWN"}
        self.store.save(1, payload, 0)
        issues = "\n".join(self.store.issues(1, payload))
        self.assertIn("Некорректная дата", issues)
        self.assertIn("не совпадают", issues)
        with self.assertRaises(ReviewError):
            self.store.transfer(1, 1)
        self.assertEqual(self.count(), 0)

    def test_damaged_original_blocks_and_transaction_rolls_back(self):
        path = self.store.original(1)
        path.write_bytes(b"synthetic corruption")
        self.store.save(1, self.approved(), 0)
        with self.assertRaisesRegex(ReviewError, "повреждена"):
            self.store.transfer(1, 1)
        self.assertEqual(self.count(), 0)

    def test_failure_before_commit_is_atomic_and_retry_creates_one(self):
        with self.store.conn:
            self.store.conn.execute("CREATE TRIGGER fail_transfer BEFORE INSERT ON register "
                                    "BEGIN SELECT RAISE(ABORT,'simulated crash'); END")
        self.store.save(1, self.approved(), 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.transfer(1, 1)
        self.assertEqual(self.count(), 0)
        with self.store.conn:
            self.store.conn.execute("DROP TRIGGER fail_transfer")
        self.store.transfer(1, 1)
        self.store.transfer(1, 1)
        self.assertEqual(self.count(), 1)

    def test_gui_side_by_side_default_no_save_and_transfer(self):
        window = OfflineReviewWindow(self.store)
        self.addCleanup(window.close)
        window.show()
        self.app.processEvents()
        self.assertEqual(window.splitter.count(), 2)
        self.assertIsNotNone(window.gallery)
        self.assertIsNone(window.object.currentData())
        window.inputs["payment_reference"].setText("0003539")
        window.kind.setCurrentIndex(window.kind.findData("invoice"))
        window.object.setCurrentIndex(window.object.findData(1))
        window.checked.setChecked(True)
        window.save_button.click()
        self.assertEqual(self.count(), 0)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question:
            window.transfer_button.click()
            self.assertEqual(question.call_args.args[-1], QMessageBox.StandardButton.No)
        self.assertEqual(self.count(), 0)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            window.transfer_button.click()
        self.assertEqual(self.count(), 1)
        self.assertFalse(window.transfer_button.isEnabled())
        window.selector.setCurrentIndex(3)
        self.assertIn("НАПОМИНАНИЕ", window.classification.text())
        self.assertFalse(window.transfer_button.isEnabled())
        window.selector.setCurrentIndex(4)
        self.assertIsNone(window.object.currentData())

    def test_non_test_profile_and_modified_source_fail_closed(self):
        with self.assertRaisesRegex(ReviewError, "новый каталог"):
            OfflineReviewStore(self.source)
        self.drafts[0]["result"]["fields"]["supplier_name"] = "Changed"
        (self.source / "result.json").write_text(packed({"drafts": self.drafts}), encoding="utf-8")
        with self.assertRaisesRegex(ReviewError, "снимок изменился"):
            self.store.import_snapshot(self.source)

    def test_unknown_currency_is_missing_not_a_manual_correction(self):
        payload = self.store.latest(1)[1]
        with self.store.conn:
            base = self.store.base(1)
            base["currency_code"] = "UNKNOWN"
            self.store.conn.execute("UPDATE documents SET base_json=? WHERE id=1", (packed(base),))
        payload["currency_code"] = None
        self.assertEqual(self.store.provenance(1, payload)["currency_code"],
                         "Не указано / не распознано")

    def test_unsaved_edit_cancel_prevents_losing_changes(self):
        window = OfflineReviewWindow(self.store)
        window.show()
        self.app.processEvents()
        window.inputs["payment_reference"].setText("000123")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
            window.selector.setCurrentIndex(1)
        self.assertEqual(window.document_id, 1)
        self.assertEqual(window.selector.currentIndex(), 0)
        self.assertEqual(window.inputs["payment_reference"].text(), "000123")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Save):
            window.close()
        self.assertEqual(self.store.latest(1)[1]["payment_reference"], "000123")

    def test_failed_approval_save_keeps_unsaved_input_visible(self):
        window = OfflineReviewWindow(self.store)
        self.addCleanup(window.deleteLater)
        window.inputs["payment_reference"].setText("000123456")
        window.kind.setCurrentIndex(window.kind.findData("invoice"))
        window.object.setCurrentIndex(window.object.findData(1))
        window.checked.setChecked(True)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                patch.object(QMessageBox, "warning"), \
                patch.object(self.store, "save", side_effect=sqlite3.OperationalError("database is locked")):
            window.confirm_transfer()
        self.assertEqual(window.inputs["payment_reference"].text(), "000123456")
        self.assertEqual(window.object.currentData(), 1)
        self.assertTrue(window.checked.isChecked())
        self.assertEqual(self.count(), 0)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
            window.close()

    def click_with_dialog(self, control, answer):
        timer = QTimer()
        timer.setInterval(10)
        observed = []

        def respond():
            box = QApplication.activeModalWidget()
            if isinstance(box, QMessageBox):
                observed.append(box.standardButton(box.defaultButton()))
                button = box.button(answer)
                if button is None:
                    box.reject()
                else:
                    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                timer.stop()

        timer.timeout.connect(respond)
        timer.start()
        try:
            QTest.mouseClick(control, Qt.MouseButton.LeftButton)
        finally:
            timer.stop()
        self.assertTrue(observed, "Expected real modal dialog")
        return observed[0]

    def test_native_clicks_real_confirmation_and_missing_object_warning(self):
        window = OfflineReviewWindow(self.store)
        self.addCleanup(window.close)
        window.show()
        self.app.processEvents()
        field = window.inputs["payment_reference"]
        field.selectAll()
        QTest.keyClicks(field, "000123456")
        window.kind.setCurrentIndex(window.kind.findData("invoice"))
        window.checked.setChecked(True)
        self.click_with_dialog(window.transfer_button, QMessageBox.StandardButton.Ok)
        self.assertEqual(self.count(), 0)
        window.object.setCurrentIndex(window.object.findData(2))
        QTest.mouseClick(window.save_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.store.latest(1)[1]["payment_reference"], "000123456")
        self.assertEqual(self.store.latest(1)[1]["object_id"], 2)
        self.assertEqual(self.count(), 0)
        default = self.click_with_dialog(window.transfer_button, QMessageBox.StandardButton.No)
        self.assertEqual(default, QMessageBox.StandardButton.No)
        self.assertEqual(self.count(), 0)
        self.click_with_dialog(window.transfer_button, QMessageBox.StandardButton.Yes)
        self.assertEqual(self.count(), 1)
        self.assertFalse(window.transfer_button.isEnabled())
        QTest.mouseDClick(window.transfer_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.count(), 1)
        window.close()
        reopened = OfflineReviewWindow(self.store)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.inputs["payment_reference"].text(), "000123456")
        self.assertEqual(reopened.object.currentData(), 2)
        self.assertFalse(reopened.transfer_button.isEnabled())

    def test_ui_lost_response_retry_returns_one_card_and_keeps_identity(self):
        window = OfflineReviewWindow(self.store)
        self.addCleanup(window.close)
        window.kind.setCurrentIndex(window.kind.findData("invoice"))
        window.object.setCurrentIndex(window.object.findData(1))
        window.checked.setChecked(True)
        transfer = self.store.transfer

        def committed_but_response_lost(*args):
            transfer(*args)
            raise OSError("simulated lost response after commit")

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                patch.object(QMessageBox, "warning"), \
                patch.object(self.store, "transfer", side_effect=committed_but_response_lost):
            window.confirm_transfer()
        first = self.store.card(1)
        self.assertEqual(self.count(), 1)
        self.assertTrue(window.transfer_button.isEnabled())
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            window.confirm_transfer()
        self.assertEqual(first, self.store.card(1))
        self.assertEqual(self.count(), 1)
        self.assertFalse(window.transfer_button.isEnabled())

    def test_registry_dialog_displays_card_and_opens_retained_pdf(self):
        self.store.save(1, self.approved(), 0)
        self.store.transfer(1, 1)
        window = OfflineReviewWindow(self.store)
        self.addCleanup(window.close)
        window.show()
        timer = QTimer()
        timer.setInterval(10)
        observed = []

        def inspect():
            dialog = QApplication.activeModalWidget()
            if dialog is None:
                return
            if dialog.windowTitle() == "ТЕСТОВЫЙ реестр — не рабочие расходы":
                if not observed:
                    observed.append(dialog.findChild(QTextEdit).toPlainText())
                    button = next(button for button in dialog.findChildren(QPushButton)
                                  if button.text() == "Открыть PDF выбранной карточки")
                    QTimer.singleShot(20, inspect)
                    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                elif len(observed) == 2:
                    dialog.accept()
                    timer.stop()
            elif dialog.windowTitle() == "PDF карточки":
                observed.append(len(dialog._pages))
                dialog.accept()

        timer.timeout.connect(inspect)
        timer.start()
        try:
            window.show_registry()
        finally:
            timer.stop()
        self.assertEqual(len(observed), 2)
        self.assertIn("Test Supplier AB", observed[0])
        self.assertIn(self.store.card(1)["sha256"], observed[0])
        self.assertEqual(observed[1], 1)


class ReferenceCaptionTests(unittest.TestCase):
    def test_reference_caption_forms_and_leading_zeroes(self):
        for caption in ("OCR-nr/meddelande:", "OCR-nummer:", "OCR-/referensnummer",
                        "OCR/Meddelande:"):
            with self.subTest(caption=caption):
                fields = parse_invoice_pages([{"text": f"Faktura\n{caption} 00314469748389051"}])
                self.assertEqual(fields["payment_reference"], "00314469748389051")

    def test_conflicting_references_are_not_guessed(self):
        fields = parse_invoice_pages([{"text":
                                       "Faktura\nOCR-nr/meddelande: 00123\nOCR-nummer: 456"}])
        self.assertIsNone(fields["payment_reference"])
