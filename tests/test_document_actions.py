import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_incoming_integration as integration
from document_costs import extract_cost_fields
from expense_documents import parse_invoice_pages
from offline_review import ReviewError, packed


TAX_TEXT = """Beslut om trängselskatt
Datum
2026-10-12
Transportstyrelsen
Trängselskatt för september 2026 1208
För att undvika en tilläggsavgift på 500 kronor ska betalningen finnas bokförd i tid.
Totalt att betala (kronor) 1208"""


class CostExtractionTests(unittest.TestCase):
    def test_invoice_summary_with_period_column_and_rounding_is_extracted(self):
        def cell(text, x, y):
            return {"text": text, "x0": x, "x1": x + len(text) * 6,
                    "y0": y, "y1": y + 10}

        text = """Fakturanummer: 900123
Fakturadatum: 2026-01-02
Moms 25% Från 2026-01-01 till 2026-01-31 300.00 SEK
Öresavrundning 0.50 SEK
Totalt att betala 1,580.50 SEK
Kvitton och fakturor"""
        elements = [
            cell("Fakturanummer", 20, 10), cell("900123", 150, 10),
            cell("Fakturadatum", 20, 25), cell("2026-01-02", 150, 25),
            cell("Moms 25%", 20, 80),
            cell("Från 2026-01-01 till 2026-01-31", 150, 80),
            cell("300.00 SEK", 450, 80),
            cell("Öresavrundning", 20, 100), cell("0.50 SEK", 450, 100),
            cell("Totalt att betala", 20, 120), cell("1,580.50 SEK", 450, 120),
        ]
        fields = parse_invoice_pages([{"page": 1, "text": text, "elements": elements}])
        self.assertEqual(fields["document_type"], "invoice")
        self.assertEqual(fields["invoice_number"], "900123")
        self.assertEqual(fields["document_date"], "2026-01-02")
        self.assertEqual(fields["net_amount"], "1280.00")
        self.assertEqual(fields["vat_amount"], "300.00")
        self.assertEqual(fields["gross_amount"], "1580.00")
        self.assertEqual(fields["amount_due"], "1580.50")
        self.assertEqual(fields["rounding_amount"], "0.50")
        self.assertEqual(fields["field_sources"]["net_amount"][0]["kind"], "derived_from_stated_totals")

    def test_tax_amount_does_not_require_invoice_or_vat(self):
        fields = parse_invoice_pages([{"page": 1, "text": TAX_TEXT}])
        costs = fields["object_cost_fields"]
        self.assertEqual(costs["gross_amount"], "1208.00")
        self.assertEqual(costs["document_date"], "2026-10-12")
        self.assertEqual(costs["supplier_name"], "Transportstyrelsen")
        self.assertIsNone(costs["vat_amount"])
        self.assertIsNone(costs["invoice_number"])
        self.assertEqual([item["amount"] for item in costs["amount_candidates"]], ["1208.00"])

    def test_conflicting_totals_are_candidates_not_an_invented_choice(self):
        fields = extract_cost_fields([{"page": 1, "text": TAX_TEXT.replace(
            "Totalt att betala (kronor) 1208", "Totalt att betala (kronor) 1250")}], {})
        self.assertIsNone(fields["gross_amount"])
        self.assertEqual([item["amount"] for item in fields["amount_candidates"]], ["1208.00", "1250.00"])
        self.assertTrue(all(item["evidence"][0]["text"] for item in fields["amount_candidates"]))

    def test_year_penalty_reference_and_daily_passages_are_not_totals(self):
        text = """Beslut om trängselskatt
Trängselskatt för september 2026
För att undvika en tilläggsavgift på 500 kronor ska betalningen ske i tid.
OCR-nummer
18154211316718179
2026-09-01 2 55 Stockholm 55
Registreringsnummer HPZ 77H"""
        fields = extract_cost_fields([{"text": text}], {})
        self.assertIsNone(fields["gross_amount"])
        self.assertEqual(fields["amount_candidates"], [])
        self.assertEqual(fields["payment_reference"], "18154211316718179")

    def test_split_total_caption_and_amount_with_thousands(self):
        fields = extract_cost_fields([{"page": 2, "text": """Beslut om trängselskatt
Summaattbetala
1 208,00 kr"""}], {})
        self.assertEqual(fields["gross_amount"], "1208.00")
        self.assertEqual(fields["amount_candidates"][0]["evidence"][0]["page"], 2)


class DocumentActionTests(unittest.TestCase):
    setUpClass = classmethod(integration.IncomingIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(integration.IncomingIntegrationTests.tearDownClass.__func__)
    _add_company = integration.IncomingIntegrationTests._add_company
    setUp = integration.IncomingIntegrationTests.setUp
    stage = integration.IncomingIntegrationTests.stage
    request = integration.IncomingIntegrationTests.request
    sync = integration.IncomingIntegrationTests.sync
    approve = integration.IncomingIntegrationTests.approve

    def tax(self, text=TAX_TEXT):
        fields = parse_invoice_pages([{"page": 1, "text": text}])
        with self.store.conn:
            self.store.conn.execute("UPDATE documents SET base_json=? WHERE id=1", (packed(fields),))
        return fields

    def documents(self):
        return self.server_client.get("/api/expense-documents", headers={
            "X-Sync-Key": "isolated-sync-test-key"}).get_json()["documents"]

    def test_rerecognition_only_fills_empty_fields_and_preserves_review_state(self):
        document_id = 1
        base = self.store.base(document_id)
        revision, payload = self.store.latest(document_id)
        base.update({
            "document_type": "receipt",
            "supplier_name": "Manually verified supplier",
            "invoice_number": "MANUAL-123",
            "document_date": "2026-10-21",
            "net_amount": None,
            "vat_amount": None,
            "gross_amount": None,
            "amount_due": None,
            "rounding_amount": "0.50",
            "raw_text": "Preserve existing extraction text",
            "field_sources": {"supplier_name": [{"kind": "manual"}]},
        })
        payload.update({
            "supplier_name": base["supplier_name"],
            "invoice_number": base["invoice_number"],
            "document_date": base["document_date"],
            "net_amount": None,
            "vat_amount": None,
            "gross_amount": None,
            "amount_due": None,
            "rounding_amount": base["rounding_amount"],
            "confirmed_type": "receipt",
            "object_id": self.object_id,
            "allocations": [{"destination": "object", "object_id": self.object_id,
                             "gross_amount": "125.00"}],
            "original_checked": True,
        })
        source_before = self.store.conn.execute(
            "SELECT source_json FROM documents WHERE id=?", (document_id,)).fetchone()[0]
        with self.store.conn:
            self.store.conn.execute("UPDATE documents SET base_json=? WHERE id=?",
                                    (packed(base), document_id))
            self.store.conn.execute(
                "UPDATE revisions SET payload_json=? WHERE document_id=? AND revision=?",
                (packed(payload), document_id, revision))
        original_before = self.store.original(document_id).read_bytes()

        recognized = {
            "document_type": "invoice",
            "supplier_name": "Different recognized supplier",
            "invoice_number": "RECOGNIZED-456",
            "document_date": "2026-11-06",
            "net_amount": "200.00",
            "vat_amount": "50.00",
            "gross_amount": "250.00",
            "amount_due": "250.50",
            "rounding_amount": "0.50",
            "currency_code": "SEK",
            "field_sources": {"net_amount": [{"kind": "invoice_total"}]},
            "object_cost_fields": {
                "supplier_name": "Different recognized supplier",
                "document_date": "2026-11-06",
                "invoice_number": "RECOGNIZED-456",
                "currency_code": "SEK",
                "net_amount": "200.00",
                "vat_amount": "50.00",
                "gross_amount": "250.00",
                "rounding_amount": "0.50",
                "amount_due": "250.50",
                "field_sources": {"net_amount": [{"kind": "invoice_total"}]},
                "amount_candidates": [],
            },
        }
        conflicts = self.store.record_recognition(
            document_id, {"fields": recognized, "state": "requires_review"})

        saved_base = self.store.base(document_id)
        _, saved_payload = self.store.latest(document_id)
        self.assertEqual(saved_base["document_type"], "receipt")
        self.assertEqual(saved_base["supplier_name"], "Manually verified supplier")
        self.assertEqual(saved_base["invoice_number"], "MANUAL-123")
        self.assertEqual(saved_base["raw_text"], "Preserve existing extraction text")
        self.assertEqual(saved_base["field_sources"]["supplier_name"], [{"kind": "manual"}])
        self.assertEqual(saved_base["field_sources"]["net_amount"], [{"kind": "invoice_total"}])
        self.assertEqual(saved_payload["net_amount"], "200.00")
        self.assertEqual(saved_payload["vat_amount"], "50.00")
        self.assertEqual(saved_payload["gross_amount"], "250.00")
        self.assertEqual(saved_payload["amount_due"], "250.50")
        self.assertEqual(saved_payload["rounding_amount"], "0.50")
        self.assertEqual(saved_payload["confirmed_type"], "receipt")
        self.assertEqual(saved_payload["object_id"], self.object_id)
        self.assertEqual(saved_payload["allocations"], payload["allocations"])
        self.assertTrue(saved_payload["original_checked"])
        self.assertIn("document_type", {item["field"] for item in conflicts})
        self.assertIn("supplier_name", {item["field"] for item in conflicts})
        self.assertEqual(source_before, self.store.conn.execute(
            "SELECT source_json FROM documents WHERE id=?", (document_id,)).fetchone()[0])
        self.assertEqual(original_before, self.store.original(document_id).read_bytes())
        self.assertIsNone(self.store.card(document_id))
        self.assertIsNone(self.store.action(document_id)[1])

    def test_statuses_need_no_fields_preserve_pdf_history_and_dedup(self):
        self.tax()
        before = self.store.conn.execute("SELECT source_json,base_json FROM documents WHERE id=1").fetchone()
        original = self.store.original(1).read_bytes()
        with patch.object(self.store, "save", side_effect=AssertionError("Financial save forbidden")):
            self.assertEqual(self.store.set_action(1, "ignore", 0), 1)
            self.assertEqual(self.store.set_action(1, "ignore", 1), 1)
            self.assertEqual(self.store.set_action(1, "later", 1), 2)
        self.assertEqual(self.store.action(1), (2, "later"))
        from incoming_documents import IncomingStore
        reopened = IncomingStore(self.store.root, self.host)
        try:
            self.assertEqual(reopened.action(1), (2, "later"))
        finally:
            reopened.close()
        self.assertEqual(len(self.store.history(1)), 1)
        self.assertEqual(tuple(before), tuple(self.store.conn.execute(
            "SELECT source_json,base_json FROM documents WHERE id=1").fetchone()))
        self.assertEqual(original, self.store.original(1).read_bytes())
        self.assertFalse(self.store.add_pdf(original, "same.pdf", {"fields": {}}))
        self.assertEqual(self.documents(), [])
        self.assertIsNone(self.store.card(1))
        conn = self.desktop.get_db_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM expense_document_outbox").fetchone()[0], 0)
        conn.close()
        with self.assertRaises(ReviewError):
            self.store.set_action(1, "expense", 0)

    def test_tax_ui_autofill_expense_without_number_vat_lost_ack_and_pdf(self):
        from PyQt6.QtWidgets import QMessageBox
        from offline_review_ui import OfflineReviewWindow
        self.tax()
        window = OfflineReviewWindow(self.store, integrated=True)
        try:
            window.show()
            self.qt_app.processEvents()
            self.assertTrue(window.financial_panel.isVisible())
            window.inputs["vat_amount"].setText("invalid unsaved financial text")
            with patch.object(self.store, "save", side_effect=AssertionError("Financial save forbidden")):
                window.ignore_button.click()
                window.later_button.click()
            self.assertEqual(self.store.action(1)[1], "later")
            self.assertTrue(window.financial_panel.isVisible())
            window.inputs["vat_amount"].clear()
            self.qt_app.processEvents()
            self.assertTrue(window.financial_panel.isVisible())
            self.assertTrue(window.gallery.isVisible())
            self.assertEqual(window.inputs["gross_amount"].text(), "1208.00")
            self.assertEqual(window.inputs["supplier_name"].text(), "Transportstyrelsen")
            self.assertEqual(window.inputs["document_date"].text(), "2026-10-12")
            self.assertEqual(window.inputs["invoice_number"].text(), "")
            self.assertEqual(window.inputs["vat_amount"].text(), "")
            self.assertTrue(any("назначение" in problem for problem in self.store.issues(1, window.payload())))
            destination = window.allocations.cellWidget(0, 0)
            destination.setCurrentIndex(destination.findData(self.object_id))
            window.checked.setChecked(True)
            self.assertEqual(self.store.issues(1, window.payload()), [])
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                window.confirm_transfer()
            self.assertIsNotNone(self.store.card(1))
            self.mode = "lost_ack"
            self.assertEqual(self.sync()["synced"], 0)
            self.assertEqual(self.sync()["synced"], 1, self.last_sync)
            self.store.transfer(1, self.store.latest(1)[0])
            self.assertEqual(self.sync()["synced"], 0)
            documents = self.documents()
            self.assertEqual(len(documents), 1)
            self.assertEqual(documents[0]["gross_amount"], "1208.00")
            self.assertIsNone(documents[0]["vat_amount"])
            self.assertEqual(documents[0]["invoice_number"], "")
            self.assertIsNone(documents[0]["amount_due"])
            self.assertTrue(documents[0]["has_original"])
            self.host.incoming_register_snapshot = self.documents
            window.select_document()
            self.assertEqual(window.status.text(), "Учтён на объекте")
            self.assertFalse(window.ignore_button.isEnabled())
        finally:
            window.loaded_payload = window.payload()
            window.close()

    def test_ambiguous_tax_ui_selects_an_extracted_candidate(self):
        from offline_review_ui import OfflineReviewWindow
        self.tax(TAX_TEXT.replace("Totalt att betala (kronor) 1208", "Totalt att betala (kronor) 1250"))
        window = OfflineReviewWindow(self.store, integrated=True)
        try:
            self.assertEqual(window.inputs["gross_amount"].text(), "")
            self.assertEqual(window.amount_choices.count(), 3)
            window.amount_choices.setCurrentIndex(window.amount_choices.findData("1250.00"))
            self.assertEqual(window.inputs["gross_amount"].text(), "1250.00")
            self.assertIn("Выбрано из считанных сумм", self.store.provenance(1, window.payload())["gross_amount"])
            self.assertTrue(window.save_edits())
            self.assertEqual(self.store.latest(1)[1]["gross_amount"], "1250.00")
        finally:
            window.loaded_payload = window.payload()
            window.close()

    def test_disposition_blocks_transfer_until_expense_action_and_reminder_stays_blocked(self):
        revision = self.approve(2)
        self.store.set_action(2, "later", 0)
        with self.assertRaisesRegex(ReviewError, "Учесть как расход"):
            self.store.transfer(2, revision)
        self.store.set_action(2, "expense", 1)
        self.store.transfer(2, revision)
        with self.assertRaises(ReviewError):
            self.store.set_action(2, "ignore", 2)
        self.store.set_action(3, "expense", 0)
        with self.assertRaisesRegex(ReviewError, "Напоминание"):
            self.store.transfer(3, self.approve(3))

    def test_ordinary_register_reopens_the_linked_incoming_card_not_invoice_form(self):
        from offline_review_ui import OfflineReviewWindow
        card = self.store.transfer(2, self.approve(2))
        window = OfflineReviewWindow(self.store, integrated=True)
        host = SimpleNamespace(open_incoming_documents=lambda: None, _incoming_viewer=window)
        try:
            self.assertTrue(self.desktop.SmartReportApp._expense_document_review_dialog(
                host, document={"client_id": card["client_id"], "usage": "object_cost"}))
            self.assertEqual(window.document_id, 2)
            self.assertTrue(window.inputs["gross_amount"].isReadOnly())
        finally:
            window.loaded_payload = window.payload()
            window.close()
