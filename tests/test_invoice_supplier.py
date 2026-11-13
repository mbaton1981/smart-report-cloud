import unittest

from expense_documents import parse_invoice_pages


class InvoiceSupplierTests(unittest.TestCase):
    @staticmethod
    def cell(text, x0, y0, x1, y1):
        return {"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1}

    def test_right_issuer_identity_beats_unlabelled_buyer_and_reads_fiscal_summary(self):
        elements = [
            self.cell("Issuer Parking AB", 420, 77, 520, 85),
            self.cell("Box 12", 420, 89, 460, 97),
            self.cell("111 22 Stockholm", 420, 101, 510, 109),
            self.cell("Buyer AB", 45, 110, 150, 118),
            self.cell("Kundvägen 1", 45, 122, 150, 130),
            self.cell("123 45 Stockholm", 45, 134, 150, 142),
            self.cell("Organisationsnummer", 306, 189, 395, 197),
            self.cell("556000-0000", 420, 189, 480, 197),
            self.cell("Momsregistreringsnummer", 306, 177, 413, 185),
            self.cell("SE556000000001", 420, 177, 500, 185),
            self.cell("Fakturanummer", 45, 280, 130, 288),
            self.cell("TEST1234", 45, 295, 130, 303),
            self.cell("Fakturadatum", 240, 280, 320, 288),
            self.cell("2026-11-05", 240, 295, 320, 303),
            self.cell("Totalt belopp exkl moms", 36, 737, 145, 745),
            self.cell("SEK 1.000,00", 520, 737, 575, 745),
            self.cell("25% MOMS", 36, 752, 85, 760),
            self.cell("SEK 1.000,00", 340, 752, 395, 760),
            self.cell("SEK 250,00", 528, 752, 575, 760),
            self.cell("Oss tillhanda 2026-11-29", 36, 766, 145, 774),
            self.cell("Totalt belopp inkl moms", 292, 766, 395, 774),
            self.cell("SEK 1.250,00", 520, 766, 575, 774),
            self.cell("OCR nummer: 1234567890", 36, 790, 180, 798),
        ]
        fields = parse_invoice_pages([{
            "text": "Faktura\nIssuer Parking ABBox 12\nBuyer AB",
            "page": 1, "elements": elements,
        }])
        self.assertEqual(fields["supplier_name"], "Issuer Parking AB")
        self.assertEqual(fields["supplier_org_number"], "556000-0000")
        self.assertEqual(fields["supplier_vat_number"], "SE556000000001")
        self.assertEqual(fields["net_amount"], "1000.00")
        self.assertEqual(fields["vat_amount"], "250.00")
        self.assertEqual(fields["amount_due"], "1250.00")
        self.assertEqual(fields["payment_reference"], "1234567890")
        self.assertEqual(fields["due_date"], "2026-11-29")

    def test_order_heading_after_buyer_address_is_not_an_invoice(self):
        text = "\n".join(["Bank: Example"] * 8 + [
            "Kundnr.: 12345", "Buyer AB", "Street 1", "111 22 Stockholm",
            "Leveransadress:", "Project: P1039", "EDI nr.: 0000",
            "Orderbekräftelse", "Sida: 1 av 3", "Seller AB",
            "Ordernr.: 12345", "Skapad datum: 06.11.2026",
        ])
        fields = parse_invoice_pages([{"text": text, "page": 1}])
        self.assertEqual(fields["document_type"], "order_confirmation")
        self.assertIsNone(fields["invoice_number"])
        self.assertIsNone(fields["supplier_name"])

    def test_supplier_uses_invoice_issuer_not_buyer_collector_or_later_operator(self):
        first_page = {
            "source": "synthetic-invoice.pdf",
            "page": 1,
            "attachment_index": 0,
            "text": "\n".join((
                "EasyPark Business",
                "Faktura",
                "Fakturadatum 2026-11-02",
                "Fakturanummer 1948924082",
                "Org.nr 5595404392",
                "Våra kundfordringar administreras av Amili Collection AS.",
                "Netto 919,74",
                "Moms totalt 229,94",
                "Totalbelopp inkl. moms i SEK 1 149,68",
                "Att betala 1 149,68 SEK",
                "Buyer Company AB",
                "Kundvägen 1",
                "123 45 Stockholm",
                "EasyPark AB",
                "Birger Jarlsgatan 57",
                "113 56 Stockholm",
                "EasyPark org. nummer Kontaktinformation IBAN, BIC/SWIFT",
                "556626-7893 www.easypark.se SE2091900000091953400002",
                "EasyPark momsnummer support@easypark.net",
                "SE556626789301 0770-112200",
            )),
            "elements": [
                self.cell("Fakturadatum", 48, 110, 110, 118),
                self.cell("2026-11-02", 150, 110, 200, 118),
                self.cell("Fakturanummer", 48, 120, 120, 128),
                self.cell("1948924082", 150, 120, 205, 128),
                self.cell("Buyer Company AB", 450, 160, 560, 168),
                self.cell("Kundvägen 1", 450, 174, 520, 182),
                self.cell("123 45 Stockholm", 450, 185, 540, 193),
                self.cell("Org.nr", 48, 170, 80, 178),
                self.cell("5595404392", 100, 170, 150, 178),
                self.cell("Netto", 48, 320, 85, 328),
                self.cell("919,74", 200, 320, 235, 328),
                self.cell("Moms totalt", 48, 330, 100, 338),
                self.cell("229,94", 200, 330, 235, 338),
                self.cell("Totalbelopp inkl. moms i SEK", 48, 340, 165, 348),
                self.cell("1 149,68", 200, 340, 250, 348),
                self.cell("Att betala", 48, 350, 100, 358),
                self.cell("1 149,68", 200, 350, 250, 358),
                self.cell("EasyPark AB", 48, 710, 105, 718),
                self.cell("Birger Jarlsgatan 57", 48, 721, 135, 729),
                self.cell("113 56 Stockholm", 48, 732, 125, 740),
                self.cell("EasyPark org. nummer", 48, 742, 130, 750),
                self.cell("556626-7893", 48, 752, 95, 760),
                self.cell("EasyPark momsnummer", 48, 762, 130, 770),
                self.cell("SE556626789301", 48, 772, 100, 780),
            ],
        }
        later_page = {
            "source": "synthetic-invoice.pdf",
            "page": 2,
            "attachment_index": 0,
            "text": "Parkerings AB\nBox 12\n111 22 Stockholm\nParkerings Org nr\n556000-0000",
            "elements": [
                self.cell("Parkerings AB", 48, 710, 120, 718),
                self.cell("Box 12", 48, 721, 75, 729),
                self.cell("111 22 Stockholm", 48, 732, 125, 740),
                self.cell("Parkerings Org nr", 48, 742, 125, 750),
                self.cell("556000-0000", 48, 752, 100, 760),
            ],
        }

        fields = parse_invoice_pages([first_page, later_page])

        self.assertEqual(fields["supplier_name"], "EasyPark AB")
        self.assertEqual(fields["supplier_org_number"], "556626-7893")
        self.assertEqual(fields["supplier_vat_number"], "SE556626789301")
        self.assertEqual(fields["invoice_number"], "1948924082")
        self.assertEqual(fields["document_date"], "2026-11-02")
        self.assertEqual(fields["net_amount"], "919.74")
        self.assertEqual(fields["vat_amount"], "229.94")
        self.assertEqual(fields["gross_amount"], "1149.68")
        self.assertEqual(fields["amount_due"], "1149.68")
        self.assertIn(fields["field_sources"]["supplier_name"][0]["kind"],
                      {"seller_address_block", "seller_registration_block"})

    def test_registered_footer_issuer_beats_buyer_and_parses_combined_invoice_ocr_and_rounding(self):
        text = "\n".join((
            "Santander Consumer Bank",
            "FE 302, 171 75 Stockholm",
            "Faktura",
            "Bygger och renoverar i Sthlm AB",
            "INGRID HORNS VÄG 8",
            "136 36 HANDEN",
            "Fakturanr/OCR-nr 619719487",
            "Faktureringsdatum 2026-11-09",
            "Oss tillhanda senast 2026-12-01",
            "OCR-nr 619719487",
            "Hyra 20261201 - 20261231 4 142,00",
            "Administrationsavgift 59,00",
            "Moms 25 % 1 050,25",
            "Öresavrundning −0,25",
            "Att betala 5 251,00 kr",
            "Santander Consumer Bank AS Norge,Sverige filial orgnr 516406-0336 VATnr SE516406033601",
        ))

        fields = parse_invoice_pages([{"text": text, "page": 1}])

        self.assertEqual(fields["supplier_name"], "Santander Consumer Bank AS Norge, Sverige filial")
        self.assertEqual(fields["supplier_org_number"], "516406-0336")
        self.assertEqual(fields["supplier_vat_number"], "SE516406033601")
        self.assertEqual(fields["invoice_number"], "619719487")
        self.assertEqual(fields["payment_reference"], "619719487")
        self.assertEqual(fields["document_date"], "2026-11-09")
        self.assertEqual(fields["due_date"], "2026-12-01")
        self.assertEqual(fields["net_amount"], "4201.00")
        self.assertEqual(fields["vat_amount"], "1050.25")
        self.assertEqual(fields["gross_amount"], "5251.25")
        self.assertEqual(fields["rounding_amount"], "-0.25")
        self.assertEqual(fields["amount_due"], "5251.00")
        self.assertEqual(fields["field_sources"]["supplier_name"][0]["kind"],
                         "seller_registration_block")

    def test_receipt_totals_date_order_reference_and_stated_vat(self):
        fields = parse_invoice_pages([{
            "page": 1,
            "text": "\n".join((
                "Kvitto", "Kvittonummer: 8987285", "Datum: 20261112",
                "Ordernummer: 32549051", "Belopp exkl. moms: 4 342,40 kr",
                "Moms totalt: 1 085,60 kr", "Summa totalt: 5 428,00 kr",
                "Betald via Svea Checkout",
            )),
            "elements": [
                self.cell("Kvittonummer:", 10, 10, 75, 20),
                self.cell("8987285", 80, 10, 130, 20),
                self.cell("Datum:", 10, 30, 45, 40),
                self.cell("20261112", 80, 30, 140, 40),
                self.cell("Ordernummer:", 10, 50, 80, 60),
                self.cell("32549051", 90, 50, 150, 60),
                self.cell("Belopp exkl. moms:", 10, 70, 105, 80),
                self.cell("4 342,40 kr", 120, 70, 185, 80),
                self.cell("Moms", 10, 100, 40, 110),
                self.cell("totalt:", 45, 100, 80, 110),
                self.cell("1 085,60 kr", 90, 100, 150, 110),
                self.cell("Summa totalt:", 10, 120, 80, 130),
                self.cell("5 428,00 kr", 90, 120, 155, 130),
            ],
        }])
        self.assertEqual(fields["document_type"], "receipt")
        self.assertEqual(fields["invoice_number"], "8987285")
        self.assertEqual(fields["order_number"], "32549051")
        self.assertIsNone(fields["payment_reference"])
        self.assertEqual(fields["document_date"], "2026-11-12")
        self.assertEqual(fields["net_amount"], "4342.40")
        self.assertEqual(fields["vat_amount"], "1085.60")
        self.assertEqual(fields["gross_amount"], "5428.00")
        self.assertFalse(any("НДС не указана" in warning for warning in fields["review_reasons"]))

    def test_github_style_usd_receipt_keeps_transaction_id_separate_and_vat_unknown(self):
        fields = parse_invoice_pages([{
            "page": 1,
            "text": "\n".join((
                "Receipt", "Date 2026-11-12 03:31AM PST",
                "Transaction ID ch_123456789",
                "Description Amount",
                "Copilot Max $100.00 USD",
                "Copilot Usage $14.37 USD",
                "Total $114.37 USD*",
                "GitHub, Inc.",
                "* VAT/GST paid directly by GitHub, where applicable",
            )),
        }])
        self.assertEqual(fields["document_type"], "receipt")
        self.assertEqual(fields["supplier_name"], "GitHub, Inc.")
        self.assertEqual(fields["document_date"], "2026-11-12")
        self.assertEqual(fields["payment_reference"], "ch_123456789")
        self.assertIsNone(fields["invoice_number"])
        self.assertEqual(fields["currency_code"], "USD")
        self.assertEqual(fields["gross_amount"], "114.37")
        self.assertIsNone(fields["net_amount"])
        self.assertIsNone(fields["vat_amount"])
        self.assertTrue(any("НДС не указана" in warning for warning in fields["review_reasons"]))


if __name__ == "__main__":
    unittest.main()
