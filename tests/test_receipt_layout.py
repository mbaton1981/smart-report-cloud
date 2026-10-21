import unittest

from expense_documents import parse_invoice_pages


def cell(text, x, y, width=None):
    return {"text": text, "x0": x, "y0": y, "x1": x + (width or len(text) * 4), "y1": y + 8}


class ReceiptLayoutTests(unittest.TestCase):
    def bygghemma(self, total="7 506 kr", vat="1 501,20 kr", date="2026-10-13"):
        return {"page": 1, "text": "Följesedel / Kvitto", "elements": [
            cell("Följesedel / Kvitto", 250, 50),
            cell("Leveransadress", 35, 110), cell("Fakturaadress", 180, 110),
            cell("Customer AB", 35, 132, 120), cell("Customer AB", 180, 132, 120),
            cell("Följesedeldatum", 307, 132, 65), cell(date, 468, 132),
            cell("Artikel nr", 35, 244), cell("Beskrivning", 100, 244),
            cell("Antal", 275, 244), cell("A pris", 332, 244),
            cell("Rabatt %", 390, 244), cell("Rabatt kr", 447, 244),
            cell("Totalt", 503, 244),
            cell("1", 280, 270), cell("999,99", 340, 270), cell("7347,00", 505, 270),
            cell("Moms", 388, 352), cell(vat, 501, 352),
            cell("Totalt", 388, 367), cell(total, 501, 367),
            cell("Postadress", 35, 710), cell("Example Seller AB", 35, 725),
            cell("Box 123", 35, 739), cell("12345 Stockholm", 35, 750),
        ]}

    def power(self, total="613,00", vat="122,60", with_vat=True):
        caption = f"TOTAL (Inkl. moms SEK {vat})" if with_vat else "TOTAL"
        return {"page": 1, "text": "KONTANTNOTA\nOrg.nr: SE1234567890\nKvitto",
                "elements": [
                    cell("Org.nr: SE1234567890", 440, 70),
                    cell("KONTANTNOTA", 327, 110), cell("TEST95Y0", 499, 110),
                    cell("Ordernr:", 327, 126), cell("ORDER-99", 395, 126),
                    cell("Datum:", 327, 138), cell("2026.10.14 19:05", 395, 138),
                    cell("Varukod", 25, 257), cell("Beskrivning", 115, 257),
                    cell("Lev.datum", 370, 257), cell("Pris", 460, 257),
                    cell("Ant", 490, 257), cell("Belopp V", 536, 257),
                    cell(caption, 114, 319, 150), cell("SEK", 473, 319), cell(total, 540, 319),
                    cell("E-mail: private@example.test", 99, 795),
                    cell("Example Electronics AB", 441, 795),
                ]}

    def test_receipt_table_header_is_not_document_total(self):
        fields = parse_invoice_pages([self.bygghemma()])
        self.assertEqual(fields["supplier_name"], "Example Seller AB")
        self.assertEqual(fields["document_date"], "2026-10-13")
        self.assertEqual(fields["gross_amount"], "7506.00")
        self.assertEqual(fields["vat_amount"], "1501.20")
        self.assertEqual(fields["net_amount"], "6004.80")
        self.assertIsNone(fields["amount_due"])
        self.assertIsNone(fields["invoice_number"])
        self.assertEqual(fields["field_sources"]["net_amount"][0]["operation"], "gross_minus_vat")
        self.assertTrue(all(source["kind"] != "line_item" for source in fields["field_sources"]["gross_amount"]))

    def test_layout_rules_not_hardcoded_to_original_amounts_and_dates(self):
        fields = parse_invoice_pages([self.bygghemma("112 kr", "12 kr", "2026-09-09")])
        self.assertEqual(fields["gross_amount"], "112.00")
        self.assertEqual(fields["vat_amount"], "12.00")
        self.assertEqual(fields["net_amount"], "100.00")
        self.assertEqual(fields["document_date"], "2026-09-09")

    def test_cash_receipt_footer_number_timestamp_and_embedded_stated_vat(self):
        fields = parse_invoice_pages([self.power()])
        self.assertEqual(fields["document_type"], "receipt")
        self.assertEqual(fields["supplier_name"], "Example Electronics AB")
        self.assertEqual(fields["invoice_number"], "TEST95Y0")
        self.assertEqual(fields["document_date"], "2026-10-14")
        self.assertEqual(fields["currency_code"], "SEK")
        self.assertEqual(fields["gross_amount"], "613.00")
        self.assertEqual(fields["vat_amount"], "122.60")
        self.assertEqual(fields["net_amount"], "490.40")
        self.assertIsNone(fields["amount_due"])
        self.assertEqual(fields["field_sources"]["vat_amount"][0]["page"], 1)

    def test_cash_receipt_different_amount_and_missing_vat_do_not_guess_rate(self):
        fields = parse_invoice_pages([self.power("224,00", "24,00")])
        self.assertEqual(fields["net_amount"], "200.00")
        self.assertEqual(fields["vat_amount"], "24.00")
        fields = parse_invoice_pages([self.power(with_vat=False)])
        self.assertIsNone(fields["vat_amount"])
        self.assertIsNone(fields["net_amount"])
        self.assertIsNone(fields["amount_due"])

    def test_conflicting_document_totals_remain_unknown(self):
        page = self.bygghemma()
        page["elements"] += [cell("Totalt", 388, 400), cell("999 kr", 501, 400)]
        fields = parse_invoice_pages([page])
        self.assertIsNone(fields["gross_amount"])
        self.assertIsNone(fields["net_amount"])
        self.assertTrue(any("gross_amount" in reason for reason in fields["review_reasons"]))


if __name__ == "__main__":
    unittest.main()
