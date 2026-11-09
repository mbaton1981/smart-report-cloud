import unittest

from expense_documents import parse_invoice_pages


class InvoiceSupplierTests(unittest.TestCase):
    @staticmethod
    def cell(text, x0, y0, x1, y1):
        return {"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1}

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
        self.assertEqual(
            fields["field_sources"]["supplier_name"][0]["kind"],
            "seller_address_block",
        )


if __name__ == "__main__":
    unittest.main()
