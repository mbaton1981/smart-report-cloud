import hashlib
import json
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from mail_import import MailImportError, MailQueue, classify_mail_document, recognition_status
from mail_review import FIELD_LABELS, allocation_review, collect_review, export_review, render_review
from expense_documents import parse_invoice_pages


class MailClassificationTests(unittest.TestCase):
    def test_recognition_status_separates_non_invoice_missing_fields_and_failure(self):
        order = recognition_status({"fields": {
            "document_type": "order_confirmation", "raw_text": "ORDERBEKRÄFTELSE",
        }}, "requires_review")
        self.assertEqual(order["label"], "Подтверждение заказа — не фактура")
        self.assertEqual(order["outcome"], "not_invoice")
        self.assertEqual(order["missing_fields"], [])
        invoice = recognition_status({"fields": {
            "document_type": "invoice", "supplier_name": "Supplier AB",
        }}, "requires_review")
        self.assertEqual(invoice["outcome"], "incomplete")
        self.assertIn("invoice_number", invoice["missing_fields"])
        self.assertEqual(recognition_status({"error_code": "ValueError"}, "failed")["outcome"], "failed")
        self.assertEqual(recognition_status({"fields": {"document_type": "unclassified"}},
                                            "requires_review")["outcome"], "unclassified")

    def test_tax_receiver_is_not_supplier_and_no_vat_is_invented(self):
        fields = {"raw_text": "Beslut om trängselskatt\nDatum\n2026-10-12",
                  "document_type": "invoice", "supplier_name": "Receiver AB",
                  "supplier_org_number": "556000-0000", "supplier_vat_number": "SE556000000001",
                  "vat_amount": None}
        classify_mail_document(fields)
        self.assertEqual(fields["document_type"], "tax_decision")
        self.assertIsNone(fields["supplier_name"])
        self.assertIsNone(fields["supplier_org_number"])
        self.assertIsNone(fields["supplier_vat_number"])
        self.assertIsNone(fields["vat_amount"])
        self.assertTrue(fields["review_reasons"])

    def test_parser_does_not_use_receiver_as_supplier_for_non_invoice_documents(self):
        for text, document_type in (
            ("Beslut om trängselskatt\nSupplier: Receiver AB\nOrg.nr: 556000-0000", "tax_decision"),
            ("ORDERBEKRÄFTELSE\nSupplier: Customer AB\nOrg.nr: 556000-0000", "order_confirmation"),
        ):
            with self.subTest(document_type=document_type):
                fields = parse_invoice_pages([{"page": 1, "text": text}])
                self.assertEqual(fields["document_type"], document_type)
                self.assertIsNone(fields["supplier_name"])
                self.assertIsNone(fields["supplier_org_number"])
                self.assertIsNone(fields["supplier_vat_number"])
                self.assertIsNone(fields["invoice_number"])
                self.assertIsNone(fields["document_date"])

    def test_order_confirmation_cannot_keep_customer_as_supplier(self):
        fields = {
            "raw_text": "ORDERBEKRÄFTELSE\nKund: Customer AB\nOrg.nummer 556000-0000",
            "document_type": "invoice",
            "supplier_name": "Customer AB",
            "supplier_org_number": "556000-0000",
            "supplier_vat_number": "SE556000000001",
            "invoice_number": "ORDER-123",
            "document_date": "2026-10-13",
        }
        classify_mail_document(fields)
        self.assertEqual(fields["document_type"], "order_confirmation")
        self.assertIsNone(fields["supplier_name"])
        self.assertIsNone(fields["supplier_org_number"])
        self.assertIsNone(fields["supplier_vat_number"])
        self.assertIsNone(fields["invoice_number"])
        self.assertIsNone(fields["document_date"])
        self.assertTrue(any("не фактура" in reason for reason in fields["review_reasons"]))

    def test_order_heading_is_not_invoice_but_footer_reference_is_not_heading(self):
        fields = {"raw_text": "ORDERBEKRÄFTELSE\nOrdernummer\nSE-123", "document_type": "invoice"}
        classify_mail_document(fields)
        self.assertEqual(fields["document_type"], "order_confirmation")
        fields = {"raw_text": "Faktura\nReferens till orderbekräftelse SE-123",
                  "document_type": "invoice", "invoice_number": "FA-456"}
        classify_mail_document(fields)
        self.assertEqual(fields["document_type"], "invoice")

    def test_uninformative_image_is_not_assumed_to_be_an_invoice(self):
        fields = {"raw_text": "Company logo", "document_type": "invoice"}
        classify_mail_document(fields)
        self.assertEqual(fields["document_type"], "unclassified")
        self.assertTrue(fields["review_reasons"])

    def test_normal_invoice_receipt_and_credit_are_preserved(self):
        for kind, text in (("invoice", "Faktura"), ("receipt", "Kvitto"),
                           ("credit_note", "Kreditfaktura")):
            fields = {"document_type": kind, "raw_text": text}
            classify_mail_document(fields)
            self.assertEqual(fields["document_type"], kind)


class MailReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "trial"
        self.queue = MailQueue(self.root)
        self.addCleanup(self.queue.close)
        from reportlab.pdfgen.canvas import Canvas
        buffer = BytesIO()
        pdf = Canvas(buffer)
        pdf.drawString(50, 750, "Original fixture document")
        pdf.save()
        self.payload = buffer.getvalue()
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.queue.accept(self.payload, ".pdf", {
            "uidvalidity": "100", "uid": "5", "part": 2, "message_id": "PRIVATE_MESSAGE_ID",
            "sender": "PRIVATE_SENDER", "subject": "PRIVATE_SUBJECT",
            "received_at": "2026-10-20T12:00:00+00:00",
            "filename": "PRIVATE_FILENAME.pdf", "content_type": "application/pdf",
        }, lambda paths: {
            "state": "requires_review",
            "fields": {"supplier_name": "Seller AB", "document_type": "invoice",
                       "raw_text": "PRIVATE_RAW_TEXT", "vat_amount": None,
                       "review_reasons": ["PRIVATE_REASON"]},
        })
        self.assessment = {
            self.digest: {"status": "decision", "evidence": "Checked page 1",
                          "notes": ["<script>not executable</script>"],
                          "fields": {"supplier_name": "Reviewed Seller AB", "vat_amount": None,
                                     "document_type": "tax_decision"}},
        }

    def test_export_is_read_only_minimized_and_retains_original_evidence(self):
        database = self.root / "mail_queue.sqlite3"
        before_db = database.read_bytes()
        before_files = {path.name: path.read_bytes() for path in (self.root / "originals").iterdir()}
        target = export_review(self.root, self.assessment)
        report = json.loads((target.parent / "audit.json").read_text(encoding="utf-8"))
        html = target.read_text(encoding="utf-8")
        self.assertEqual(database.read_bytes(), before_db)
        self.assertEqual({path.name: path.read_bytes() for path in (self.root / "originals").iterdir()},
                         before_files)
        self.assertEqual(report["integrity"], "ok")
        self.assertEqual(report["documents_count"], 1)
        self.assertEqual(report["sources_count"], 1)
        self.assertEqual(report["unique_hashes"], 1)
        document = report["documents"][0]
        self.assertEqual(document["extracted"]["supplier_name"], "Seller AB")
        self.assertEqual(document["checked"]["supplier_name"], "Reviewed Seller AB")
        self.assertIsNone(document["checked"]["vat_amount"])
        self.assertTrue(document["sha_ok"])
        self.assertEqual(report["statuses"], {"decision": 1})
        for secret in ("PRIVATE_MESSAGE_ID", "PRIVATE_SENDER", "PRIVATE_SUBJECT",
                       "PRIVATE_FILENAME", "PRIVATE_RAW_TEXT", "PRIVATE_REASON"):
            self.assertNotIn(secret, html)
            self.assertNotIn(secret, json.dumps(report))
        self.assertIn("&lt;script&gt;not executable&lt;/script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("default-src 'none'", html)
        self.assertEqual(html.count("<article "), 1)
        self.assertEqual(len(document["extracted"]), len(FIELD_LABELS))
        self.assertEqual(document["original_page_count"], 1)
        preview = target.parent / document["preview"]
        self.assertTrue(preview.is_file())
        self.assertTrue((preview.parent / "page-1.png").is_file())
        self.assertIn('id="page-1"', preview.read_text(encoding="utf-8"))
        self.assertIn("<strong>Получение:</strong>", html)
        self.assertIn("<strong>Распознавание сохранённого результата:</strong>", html)
        self.assertIn("не означают полного распознавания", html)
        self.assertIn("recognition", document)

    def test_unassessed_document_is_explicit_not_claimed_verified(self):
        report = collect_review(self.root)
        self.assertEqual(report["documents"][0]["status"], "not_checked")
        self.assertEqual(report["documents"][0]["checked"], {})
        self.assertIn("Не сверено", render_review(report))

    def test_changed_or_missing_original_stops_instead_of_false_success(self):
        original = next((self.root / "originals").iterdir())
        original.write_bytes(b"changed")
        with self.assertRaisesRegex(MailImportError, "checksum"):
            collect_review(self.root, self.assessment)
        original.unlink()
        with self.assertRaises(MailImportError):
            collect_review(self.root, self.assessment)

    def test_wrong_assessment_hash_is_rejected(self):
        with self.assertRaisesRegex(MailImportError, "absent original"):
            collect_review(self.root, {"0" * 64: self.assessment[self.digest]})

    def test_assessment_cannot_export_arbitrary_personal_fields(self):
        self.assessment[self.digest]["fields"]["sender"] = "PRIVATE"
        with self.assertRaisesRegex(MailImportError, "Unknown reviewed field"):
            collect_review(self.root, self.assessment)

    def test_queue_outside_original_path_is_rejected(self):
        outside = self.root / "outside.pdf"
        outside.write_bytes(self.payload)
        with self.queue.conn:
            self.queue.conn.execute("UPDATE documents SET original='outside.pdf'")
        with self.assertRaisesRegex(MailImportError, "outside"):
            collect_review(self.root)

    def test_runs_export_only_whitelisted_counters(self):
        (self.root / "runs").mkdir()
        (self.root / "runs" / "run.json").write_text(json.dumps({
            "started_at": "2026-10-20T12:00:00+00:00",
            "documents_added": 1, "errors": 0, "completed": True,
            "stop_reason": "PRIVATE_SERVER_TEXT", "password": "PRIVATE_PASSWORD",
        }), encoding="utf-8")
        report = collect_review(self.root)
        self.assertEqual(report["runs"][0]["documents_added"], 1)
        self.assertNotIn("PRIVATE_", json.dumps(report))

    def test_existing_foreign_database_is_not_modified(self):
        foreign = Path(self.temp.name) / "foreign"
        foreign.mkdir()
        (foreign / "originals").mkdir()
        database = foreign / "mail_queue.sqlite3"
        conn = sqlite3.connect(database)
        conn.execute("CREATE TABLE keep (id INTEGER)")
        conn.commit()
        conn.close()
        before = database.read_bytes()
        with self.assertRaises(MailImportError):
            collect_review(foreign)
        self.assertEqual(database.read_bytes(), before)

    def test_refresh_shows_original_initial_current_and_verified_values_with_history(self):
        export_review(self.root, self.assessment)
        previous_snapshot = next((self.root / "review" / "history").glob("*.json"))
        previous_bytes = previous_snapshot.read_bytes()
        refreshed = {self.digest: {
            "fields": {"supplier_name": "Fixed Parser AB", "document_type": "receipt",
                       "vat_amount": None, "prepaid": True},
            "page_count": 3, "state": "requires_review",
        }}
        target = export_review(self.root, self.assessment, refreshed)
        report = json.loads((target.parent / "audit.json").read_text(encoding="utf-8"))
        document = report["documents"][0]
        self.assertEqual(document["extracted"]["supplier_name"], "Seller AB")
        self.assertEqual(document["current"]["supplier_name"], "Fixed Parser AB")
        self.assertEqual(document["checked"]["supplier_name"], "Reviewed Seller AB")
        self.assertEqual(document["page_count"], 3)
        self.assertIsNone(document["current"]["vat_amount"])
        self.assertEqual(previous_snapshot.read_bytes(), previous_bytes)
        self.assertEqual(len(list((target.parent / "history").glob("*.json"))), 2)
        self.assertTrue(any("Fixed Parser AB" in event["description"] for event in document["history"]))
        html = target.read_text(encoding="utf-8")
        self.assertIn('<iframe loading="lazy" name="original-1"', html)
        self.assertIn("frame-src file:", html)
        self.assertIn("Парсер сейчас", html)
        export_review(self.root)
        reopened = json.loads((target.parent / "audit.json").read_text(encoding="utf-8"))
        self.assertEqual(reopened["documents"][0]["current"], document["current"])
        self.assertEqual(reopened["documents"][0]["checked"], document["checked"])
        self.assertEqual(reopened["documents"][0]["history"], document["history"])
        self.assertTrue(reopened["documents"][0]["review_context"]["prepaid"])

    def test_order_totals_reuse_existing_validation_and_require_object_choice(self):
        fields = {
            "net_amount": "100.00", "vat_amount": "25.00", "amount_due": "125.00",
            "order_proposals": [
                {"order_id": "A-1", "original_marker": "1035", "project_code": "1035",
                 "net_amount": "100.00", "vat_amount": "25.00", "object_id": 123,
                 "sources": [{"page": 3, "text": "PRIVATE_NOT_EXPORTED"}],
                 "vat_items": [{"page": 2, "item": "10", "net_amount": "100.00",
                                "vat_rate": "25.00", "vat_amount": "25.00"}]},
            ],
        }
        allocation = allocation_review(fields)
        self.assertTrue(allocation["net_matches"])
        self.assertTrue(allocation["vat_matches"])
        self.assertEqual(allocation["net_total"], "100.00")
        self.assertEqual(allocation["items_count"], 1)
        self.assertEqual(allocation["orders"][0]["pages"], [2, 3])
        self.assertIsNone(allocation["orders"][0]["object_id"])
        self.assertEqual(allocation["orders"][0]["destination"], "unselected")
        self.assertTrue(any("назначение" in issue for issue in allocation["issues"]))
        self.assertNotIn("PRIVATE_NOT_EXPORTED", json.dumps(allocation))
        fields["net_amount"] = "101.00"
        self.assertFalse(allocation_review(fields)["net_matches"])


if __name__ == "__main__":
    unittest.main()
