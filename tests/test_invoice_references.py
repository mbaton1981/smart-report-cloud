import unittest

from expense_documents import parse_invoice_pages


class InvoiceReferenceTests(unittest.TestCase):
    def page(self, lines):
        return {"page": 1, "source": "synthetic-invoice.pdf", "text": "\n".join(lines)}

    def test_storholmen_3539_explicit_due_rounding_and_reference(self):
        fields = parse_invoice_pages([self.page([
            "Faktura", "Supplier: Storholmen Enterprises AB", "Fakturanummer: 3539",
            "Fakturadatum: 2026-10-21", "Förfallodatum: 2026-11-20",
            "Exkl. moms", "14 625,00", "Moms", "3 656,25",
            "Öresavrundning", "-0,25", "Att betala (SEK)", "18 281,00",
            "Vänligen ange referens 3539 vid betalning",
        ])])
        self.assertEqual(fields["amount_due"], "18281.00")
        self.assertEqual(fields["rounding_amount"], "-0.25")
        self.assertEqual(fields["payment_reference"], "3539")
        self.assertTrue(fields["field_sources"]["amount_due"])

    def test_beijer_references_are_strings_and_keep_leading_zeros(self):
        for reference, net, vat, rounding, due in (
            ("260408939825", "1 459,71", "364,93", "0,36", "1 825,00"),
            ("260408149623", "34 500,00", "8 625,00", "0,00", "43 125,00"),
            ("00260408939825", "1 459,71", "364,93", "0,36", "1 825,00"),
        ):
            with self.subTest(reference=reference):
                fields = parse_invoice_pages([self.page([
                    "Faktura", "Supplier: BEIJER BYGGMATERIAL AB",
                    f"Fakturanummer: {reference}", f"OCR/Meddelande: {reference}",
                    f"Netto {net}", "Moms", vat, f"Öresavr. {rounding} SEK",
                    f"Att betala: {due} SEK",
                ])])
                self.assertEqual(fields["payment_reference"], reference)
                self.assertIsInstance(fields["payment_reference"], str)
                self.assertEqual(fields["amount_due"], due.replace(" ", "").replace(",", "."))
                self.assertEqual(fields["rounding_amount"], rounding.replace(",", "."))

    def test_1188_plain_ocr_does_not_regress(self):
        fields = parse_invoice_pages([self.page([
            "Faktura", "Supplier: LS Comfort Mälardalen AB", "Fakturanr 1188",
            "OCR 118869", "Fakturadatum 2026-10-19",
            "Netto 21 440,00", "Moms totalt 5 360,00", "Att betala 26 800,00 SEK",
        ])])
        self.assertEqual(fields["payment_reference"], "118869")
        self.assertEqual(fields["amount_due"], "26800.00")

    def test_missing_or_conflicting_due_is_not_computed(self):
        base = ["Faktura", "Netto 14 625,00", "Moms", "3 656,25", "Öresavrundning -0,25"]
        missing = parse_invoice_pages([self.page(base)])
        self.assertIsNone(missing["amount_due"])
        self.assertTrue(any("amount_due" in reason for reason in missing["review_reasons"]))
        conflict = parse_invoice_pages([self.page([*base, "Att betala (SEK): 18 280,00"])])
        self.assertEqual(conflict["amount_due"], "18280.00")
        self.assertTrue(any("не совпадает" in reason for reason in conflict["review_reasons"]))
        ambiguous = parse_invoice_pages([self.page([
            *base, "Att betala (SEK): 18 281,00", "Att betala (SEK): 18 280,00",
        ])])
        self.assertIsNone(ambiguous["amount_due"])
        self.assertTrue(any("amount_due" in reason for reason in ambiguous["review_reasons"]))

    def test_conflicting_references_require_review(self):
        fields = parse_invoice_pages([self.page([
            "Faktura", "OCR/Meddelande: 001234", "OCR/Meddelande: 005678",
        ])])
        self.assertIsNone(fields["payment_reference"])
        self.assertTrue(any("payment_reference" in reason for reason in fields["review_reasons"]))

    def test_order_does_not_get_payment_reference(self):
        fields = parse_invoice_pages([self.page([
            "ORDERBEKRÄFTELSE", "OCR/Meddelande: 3539", "Ordernummer SE-353275",
        ])])
        self.assertIsNone(fields["payment_reference"])


if __name__ == "__main__":
    unittest.main()
