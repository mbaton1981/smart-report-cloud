import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from expense_documents import (
    DuplicateDocumentError,
    apply_expense_documents_migration,
    extract_local_document,
    find_document_duplicates,
    get_object_expense_summary,
    is_material_expense_category,
    match_objects,
    parse_decimal,
    parse_document_text,
    record_document_payment,
    save_supplier_document,
    validate_allocations,
)


class ExpenseDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "local.sqlite"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript("""
            CREATE TABLE company_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL, category TEXT NOT NULL,
                description TEXT, amount REAL NOT NULL
            );
            CREATE TABLE objects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL, markning TEXT, company TEXT,
                rate REAL DEFAULT 0, transport_rate REAL DEFAULT 0
            );
            INSERT INTO objects (id, name, markning, company) VALUES
                (1, 'North Street Roof', 'NS-42', 'Client A'),
                (2, 'Harbor Office', 'HB-18', 'Client B');
        """)
        self.conn.commit()
        self.backups = self.root / "backups"
        backup = apply_expense_documents_migration(self.db_path, self.backups)
        self.assertIsNotNone(backup)
        self.backup_path = backup
        self.conn.close()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.conn.close)

    def fields(self, **updates):
        data = {
            "supplier_name": "Example Supplier AB",
            "supplier_org_number": "556000-0000",
            "supplier_vat_number": "SE556000000001",
            "document_type": "invoice",
            "document_date": "2026-10-10",
            "due_date": "2026-11-10",
            "invoice_number": "INV-1001",
            "payment_reference": "89001234",
            "currency_code": "SEK",
            "net_amount": "100.00",
            "vat_amount": "25.00",
            "gross_amount": "125.00",
            "amount_due": "125.00",
            "expense_category": "Materials",
            "document_comment": "Reviewed against source document",
            "reverse_charge": False,
            "verification_status": "requires_review",
            "payment_status": "unknown",
            "raw_text": "synthetic test document",
            "review_reasons": [],
            "vat_lines": [{"rate": "25", "taxable_base": "100.00", "amount": "25.00"}],
        }
        data.update(updates)
        return data

    def test_versioned_migration_is_additive_and_backup_precedes_schema(self):
        backup_conn = sqlite3.connect(self.backup_path)
        names = {row[0] for row in backup_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("company_expenses", names)
        self.assertNotIn("supplier_documents", names)
        backup_conn.close()

        names = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({
            "company_expenses", "objects", "expense_suppliers", "supplier_documents",
            "supplier_document_vat_lines", "supplier_document_allocations",
            "supplier_document_payments", "expense_object_aliases", "schema_migrations",
        }.issubset(names))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM company_expenses").fetchone()[0], 0)
        self.assertIsNone(apply_expense_documents_migration(self.db_path, self.backups))

    def test_decimal_formats_and_multi_rate_vat_supplier_fields(self):
        self.assertEqual(parse_decimal("1 234,56 SEK"), Decimal("1234.56"))
        self.assertEqual(parse_decimal("1,234.56"), Decimal("1234.56"))
        self.assertEqual(parse_decimal("(25,00)"), Decimal("-25.00"))
        parsed = parse_document_text("""
            Leverantör: Example Supplier AB
            Org.nr: 556000-0000
            VAT no: SE556000000001
            Köpare: Bygger och Renoverar i Sthm AB
            Fakturanummer: INV-1001
            OCR: 89001234
            Fakturadatum: 2026-10-10
            Förfallodatum: 2026-11-10
            Netto: 1 000,00 SEK
            Moms 25%: 250,00
            Moms 12%: 12,00
            Total inkl. moms: 1 262,00 SEK
            Att betala: 1 262,00 SEK
        """)
        self.assertEqual(parsed["supplier_name"], "Example Supplier AB")
        self.assertNotEqual(parsed["supplier_name"], "Bygger och Renoverar i Sthm AB")
        self.assertEqual(parsed["supplier_org_number"], "556000-0000")
        self.assertEqual(parsed["supplier_vat_number"], "SE556000000001")
        self.assertEqual(parsed["invoice_number"], "INV-1001")
        self.assertEqual(parsed["payment_reference"], "89001234")
        self.assertEqual(parsed["currency_code"], "SEK")
        self.assertEqual(parsed["vat_lines"], [
            {"rate": "25", "amount": "250.00"},
            {"rate": "12", "amount": "12.00"},
        ])
        self.assertEqual(parsed["gross_amount"], "1262.00")
        self.assertFalse(any("skiljer sig" in reason for reason in parsed["review_reasons"]))

    def test_six_percent_credit_note_and_reverse_charge_are_explicit(self):
        parsed = parse_document_text("""
            Leverantör: Local Shop AB
            Kreditfaktura
            Fakturadatum: 2026-10-02
            Fakturanummer: CR-15
            Netto: 100,00 SEK
            Moms 6%: 6,00
            Total inkl. moms: 106,00 SEK
        """)
        self.assertEqual(parsed["document_type"], "credit_note")
        self.assertEqual(parsed["vat_lines"][0]["rate"], "6")
        self.assertEqual(parsed["currency_code"], "SEK")

        no_vat = parse_document_text("""
            Leverantör: Local Service AB
            Kvitto
            Fakturadatum: 2026-10-02
            Netto: 100,00 SEK
            Moms 0%: 0,00
            Total inkl. moms: 100,00 SEK
        """)
        self.assertEqual(no_vat["document_type"], "receipt")
        self.assertEqual(no_vat["vat_amount"], "0.00")

        reverse = parse_document_text("""
            Leverantör: EU Services GmbH
            Invoice number: EU-7
            Invoice date: 2026-10-02
            Net: 500,00 EUR
            Total amount: 500,00 EUR
            Omvänd skattskyldighet
        """)
        self.assertTrue(reverse["reverse_charge"])
        self.assertIsNone(reverse["vat_amount"])
        self.assertTrue(any("omvänd skattskyldighet" in reason for reason in reverse["review_reasons"]))

    def test_inconsistent_totals_and_unknown_currency_require_review(self):
        parsed = parse_document_text("""
            Leverantör: Example AB
            Fakturadatum: 2026-10-10
            Fakturanummer: X-2
            Netto: 100,00
            Moms 25%: 25,00
            Total inkl. moms: 130,00
        """)
        self.assertEqual(parsed["currency_code"], "UNKNOWN")
        self.assertTrue(any("skiljer sig" in reason for reason in parsed["review_reasons"]))
        self.assertTrue(any("valuta saknas" in reason for reason in parsed["review_reasons"]))

        ocr_ascii_label = parse_document_text("""
            Leverantor: OCR Supplier AB
            Fakturanummer: OCR-1
            Fakturadatum: 2026-10-10
            Valuta: SEK
            Netto: 10,00 SEK
            Moms 0%: 0,00
            Total inkl. moms: 10,00 SEK
        """)
        self.assertEqual(ocr_ascii_label["supplier_name"], "OCR Supplier AB")
        self.assertEqual(ocr_ascii_label["vat_amount"], "0.00")

    def test_unreadable_image_and_searchable_pdf_are_local(self):
        blank = self.root / "blank.png"
        Image.new("RGB", (320, 160), "white").save(blank)
        result = extract_local_document(blank)
        self.assertEqual(result.method, "локальный OCR")
        parsed = parse_document_text(result.text)
        self.assertTrue(any("ingen läsbar text" in reason for reason in parsed["review_reasons"]))

        pdf_path = self.root / "text_invoice.pdf"
        pdf = canvas.Canvas(str(pdf_path), pagesize=A4)
        pdf.drawString(40, 800, "Fakturanummer: PDF-77")
        pdf.drawString(40, 780, "Total inkl. moms 12,50 SEK")
        pdf.save()
        pdf_result = extract_local_document(pdf_path)
        self.assertEqual(pdf_result.method, "текст PDF")
        self.assertIn("PDF-77", pdf_result.text)

    def test_object_match_reasons_multiple_matches_and_aliases(self):
        self.conn.execute("INSERT INTO expense_object_aliases (object_id, alias) VALUES (1, 'Roof renovation')")
        self.conn.commit()
        objects = [
            {"id": 1, "name": "North Street Roof", "markning": "NS-42", "aliases": ["Roof renovation"]},
            {"id": 2, "name": "Harbor Office", "markning": "HB-18", "aliases": []},
        ]
        one = match_objects("Märkn: NS-42", objects)
        self.assertEqual(len(one), 1)
        self.assertEqual(one[0]["object_id"], 1)
        self.assertIn("NS-42", one[0]["reason"])
        self.assertIn("Märkn", one[0]["reason"])
        alias = match_objects("Service for Roof renovation", objects)
        self.assertEqual(alias[0]["object_id"], 1)
        both = match_objects("NS-42 HB-18", objects)
        self.assertEqual({item["object_id"] for item in both}, {1, 2})
        self.assertEqual(match_objects("no project marker", objects), [])

    def test_allocation_decimal_reconciliation(self):
        self.assertTrue(validate_allocations("126.00", [
            {"gross_amount": "100.50"}, {"gross_amount": "25.50"},
        ])[0])
        valid, message = validate_allocations("126.00", [{"gross_amount": "125.99"}])
        self.assertFalse(valid)
        self.assertIn("не совпадает", message)

    def test_duplicate_detection_original_storage_multi_object_and_payments(self):
        source = self.root / "invoice.pdf"
        source.write_bytes(b"synthetic non-personal invoice bytes")
        fields = self.fields()
        allocations = [
            {"object_id": 1, "gross_amount": "60.00", "net_amount": "48.00", "vat_amount": "12.00", "note": "code NS-42"},
            {"object_id": 2, "gross_amount": "65.00", "net_amount": "52.00", "vat_amount": "13.00", "note": "code HB-18"},
        ]
        fields.update({"gross_amount": "125.00", "net_amount": "100.00", "vat_amount": "25.00"})
        doc_id = save_supplier_document(
            self.conn, self.root / "documents", fields, allocations,
            source_path=source, verification_status="confirmed",
        )
        document = self.conn.execute("SELECT * FROM supplier_documents WHERE id = ?", (doc_id,)).fetchone()
        self.assertEqual(document["verification_status"], "confirmed")
        self.assertEqual(document["currency_code"], "SEK")
        self.assertEqual(document["document_comment"], "Reviewed against source document")
        stored = Path(document["stored_path"])
        self.assertTrue(stored.is_file())
        self.assertIn("multiple_objects", str(stored))
        self.assertEqual(document["file_sha256"], __import__("hashlib").sha256(source.read_bytes()).hexdigest())
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM supplier_document_allocations WHERE document_id = ?", (doc_id,)).fetchone()[0],
            2,
        )
        duplicates = find_document_duplicates(self.conn, fields, document["file_sha256"])
        self.assertEqual(len(duplicates), 1)
        with self.assertRaises(DuplicateDocumentError):
            save_supplier_document(
                self.conn, self.root / "documents", fields, allocations,
                source_path=source, verification_status="confirmed",
            )

        self.assertEqual(record_document_payment(self.conn, doc_id, "2026-10-20", "40.00"), "partially_paid")
        self.assertEqual(record_document_payment(self.conn, doc_id, "2026-10-21", "85.00"), "paid")
        with self.assertRaises(ValueError):
            record_document_payment(self.conn, doc_id, "2026-10-22", "1.00")

    def test_single_object_storage_and_unallocated_document_path(self):
        source = self.root / "receipt.jpg"
        source.write_bytes(b"test bytes")
        from expense_documents import store_original_file

        one, _ = store_original_file(source, self.root / "docs", 1, ["Object A"])
        shared, _ = store_original_file(source, self.root / "docs", 2, [])
        self.assertIn("objects", str(one))
        self.assertIn("Object_A", str(one))
        self.assertIn("unallocated", str(shared))

    def test_manual_unknown_currency_draft_can_be_saved_without_supplier_or_object(self):
        fields = self.fields(
            supplier_name=None, supplier_org_number=None, supplier_vat_number=None,
            document_type="manual", document_date=None, due_date=None,
            invoice_number=None, payment_reference=None, currency_code="UNKNOWN",
            net_amount=None, vat_amount=None, gross_amount=None, amount_due=None,
            review_reasons=["manual document; fill values later"],
            document_comment="Need to check the paper receipt",
        )
        document_id = save_supplier_document(
            self.conn, self.root / "documents", fields, [], source_path=None,
            verification_status="draft",
        )
        document = self.conn.execute(
            "SELECT * FROM supplier_documents WHERE id = ?", (document_id,)
        ).fetchone()
        self.assertEqual(document["supplier_id"], None)
        self.assertEqual(document["supplier_name"], None)
        self.assertEqual(document["currency_code"], "UNKNOWN")
        self.assertEqual(document["verification_status"], "draft")
        self.assertEqual(document["document_comment"], "Need to check the paper receipt")
        self.assertIsNone(document["stored_path"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM company_expenses").fetchone()[0], 0)

    def test_object_lifetime_and_period_summary_apportions_payments_and_separates_materials(self):
        material_fields = self.fields(
            invoice_number="MAT-1", expense_category="Материалы",
            net_amount="100.00", vat_amount="25.00", gross_amount="125.00", amount_due="125.00",
        )
        document_id = save_supplier_document(
            self.conn, self.root / "documents", material_fields,
            [
                {"object_id": 1, "net_amount": "40.00", "vat_amount": "10.00", "gross_amount": "50.00"},
                {"object_id": 2, "net_amount": "60.00", "vat_amount": "15.00", "gross_amount": "75.00"},
            ], verification_status="confirmed",
        )
        save_supplier_document(
            self.conn, self.root / "documents",
            self.fields(
                invoice_number="SERVICE-1", expense_category="Услуги",
                net_amount="20.00", vat_amount="5.00", gross_amount="25.00", amount_due="25.00",
            ),
            [{"object_id": 1, "net_amount": "20.00", "vat_amount": "5.00", "gross_amount": "25.00"}],
            verification_status="confirmed",
        )
        self.conn.commit()
        self.assertTrue(is_material_expense_category("Материалы и расходники"))
        self.assertTrue(is_material_expense_category("Building materials"))
        self.assertFalse(is_material_expense_category("Услуги поставщика"))

        record_document_payment(self.conn, document_id, "2026-10-20", "40.00")
        summary = get_object_expense_summary(self.conn, 1, "2026-10-01", "2026-10-31")
        lifetime = summary["lifetime"]["SEK"]
        period = summary["period"]["SEK"]
        self.assertEqual(lifetime["net"], Decimal("60.00"))
        self.assertEqual(lifetime["vat"], Decimal("15.00"))
        self.assertEqual(lifetime["gross"], Decimal("75.00"))
        self.assertEqual(lifetime["paid"], Decimal("16.00"))
        self.assertEqual(lifetime["remaining"], Decimal("59.00"))
        self.assertEqual(lifetime["materials_gross"], Decimal("50.00"))
        self.assertEqual(lifetime["other_gross"], Decimal("25.00"))
        self.assertEqual(lifetime["documents"], {document_id, document_id + 1})
        self.assertEqual(period["gross"], lifetime["gross"])


if __name__ == "__main__":
    unittest.main()
