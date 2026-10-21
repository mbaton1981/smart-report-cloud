import unittest

from expense_documents import parse_invoice_pages
from offline_review import current_classification


def summary_page(total="48 280,00", page=4):
    def cell(text, x, y):
        return {"text": text, "x0": x, "y0": y, "x1": x + 75, "y1": y + 10}
    return {
        "page": page, "source": "synthetic-four-page-invoice.pdf",
        "text": f"Faktura\nSupplier: Example VVS AB\nFakturadatum 2025-09-30\n"
                f"Exkl. moms Totalt ATT BETALA\n48 280,00 {total} SEK {total}\nOmvänd betalningsskyldighet",
        "elements": [cell("Faktura", 20, 20),
                     cell("Supplier: Example VVS AB", 20, 50),
                     cell("Fakturadatum 2025-09-30", 20, 70),
                     cell("Exkl. moms", 20, 650), cell("Totalt", 230, 650),
                     cell("ATT BETALA", 450, 650),
                     cell("48 280,00", 20, 670), cell(total, 230, 670),
                     cell(f"SEK {total}", 450, 670)],
    }


class InvoiceSummaryTotalTests(unittest.TestCase):
    def test_four_page_reverse_charge_summary_total_does_not_require_vat(self):
        pages = [{"page": page, "text": "Faktura\nBenämning\nService item 100,00"} for page in (1, 2, 3)]
        fields = parse_invoice_pages([*pages, summary_page()])
        self.assertEqual(fields["gross_amount"], "48280.00")
        self.assertEqual(fields["object_cost_fields"]["gross_amount"], "48280.00")
        self.assertIsNone(fields["vat_amount"])
        self.assertEqual(fields["field_sources"]["gross_amount"][0]["page"], 4)
        self.assertEqual(fields["field_sources"]["gross_amount"][0]["caption"], "Totalt")

    def test_bare_item_table_total_is_not_document_total(self):
        page = summary_page()
        page["elements"] = [item for item in page["elements"] if item["x0"] == 230]
        page["text"] = "Faktura\nArtikel\nTotalt\n48 280,00"
        fields = parse_invoice_pages([page])
        self.assertIsNone(fields["gross_amount"])
        self.assertIsNone(fields["object_cost_fields"]["gross_amount"])

    def test_conflicting_summary_totals_are_not_replaced_with_zero(self):
        fields = parse_invoice_pages([summary_page(page=3), summary_page("49 000,00", page=4)])
        self.assertIsNone(fields["gross_amount"])
        self.assertIsNone(fields["object_cost_fields"]["gross_amount"])
        self.assertTrue(any("gross_amount" in reason for reason in fields["review_reasons"]))

    def test_current_classification_does_not_mutate_saved_reasons(self):
        original = {"raw_text": "Påminnelse", "document_type": "invoice", "review_reasons": ["Original"]}
        fresh = current_classification(original)
        self.assertEqual(original["review_reasons"], ["Original"])
        self.assertEqual(original["document_type"], "invoice")
        self.assertEqual(fresh["suggested_document_type"], "payment_reminder")
