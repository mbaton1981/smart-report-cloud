import unittest
from unittest.mock import patch

from expense_documents import (
    ExtractionResult, net_invoice_issues, parse_invoice_pages, parse_project_order_allocations,
)
from invoice_processing import recognize_invoice


def cell(text, x, y):
    return {"text": text, "x0": x, "x1": x + len(text) * 4, "y0": y, "y1": y + 8}


def multipage_fixture(missing_rate=False, conflicting_total=False):
    def table_header():
        return [cell("Rad", 10, 200), cell("Pris", 130, 200),
                cell("Moms %", 220, 200), cell("Summa", 320, 200)]

    def item(number, net, y):
        return [cell(number, 10, y), cell("999.00", 130, y),
                *([] if missing_rate and number == "40" else [cell("25.00", 220, y)]),
                cell(net, 320, y)]

    first = [
        cell("Supplier: Example Supplier AB", 10, 20),
        cell("Fakturanummer: INV-TEST-1", 10, 40),
        cell("Fakturadatum: 2026-10-16", 10, 60),
        cell("Valuta: SEK", 10, 80),
        *table_header(), cell("Proj.nr.: P1", 10, 225), cell("Order nr: A1", 10, 245),
        *item("10", "100.00", 270), cell("Summa order: 100.00", 10, 300),
        cell("Proj.nr.: 1035", 10, 330), cell("Order nr: B1", 10, 350),
        *item("20", "200.00", 380),
    ]
    second = [
        *table_header(), *item("30", "300.00", 240), cell("Summa order: 500.00", 10, 270),
        cell("Proj.nr.: P2", 10, 300), cell("Order nr: C1", 10, 320),
        *item("40", "400.00", 350), cell("Summa order: 400.00", 10, 380),
    ]
    third = [
        *table_header(), cell("Proj.nr.: P2", 10, 240), cell("Order nr: C1", 10, 260),
        cell(f"Summa order: {'401.00' if conflicting_total else '400.00'}", 10, 285),
        cell("Netto: 1000.00", 10, 600), cell("Moms: 250.00", 10, 625),
        cell("Avrundning: 0.00", 10, 650), cell("Att betala: 1250.00", 10, 675),
    ]
    pages = (first, second, third)
    return ExtractionResult(
        text="Faktura test multi page", method="fixture", confidence=1.0,
        pages=tuple("\n".join(v["text"] for v in page) for page in pages),
        page_elements=tuple(tuple(page) for page in pages),
    )


class InvoiceProcessingTests(unittest.TestCase):
    def recognize(self, **kwargs):
        with patch("expense_documents.extract_local_document", return_value=multipage_fixture(**kwargs)):
            return recognize_invoice(["test.pdf"])

    def test_every_page_item_and_continuation_use_existing_order_parser(self):
        result = self.recognize()
        self.assertEqual(result["page_count"], 3)
        fields = result["fields"]
        orders = fields["order_proposals"]
        self.assertEqual(len(orders), 3)
        self.assertEqual([order["net_amount"] for order in orders], ["100.00", "500.00", "400.00"])
        self.assertEqual([order["vat_amount"] for order in orders], ["25.00", "125.00", "100.00"])
        self.assertEqual(sum(len(order["vat_items"]) for order in orders), 4)
        self.assertEqual([part["page"] for part in orders[1]["vat_items"]], [1, 2])
        self.assertEqual([source["page"] for source in orders[2]["sources"]], [2, 3])
        self.assertEqual(fields["net_amount"], "1000.00")
        self.assertEqual(fields["vat_amount"], "250.00")
        self.assertEqual(fields["order_proposal_warnings"], [])
        self.assertTrue(all("object_id" not in row for row in orders))
        self.assertTrue(any("назначение" in issue for issue in net_invoice_issues(fields, orders)))

    def test_missing_item_rate_keeps_net_proposals_and_reports_vat_failure(self):
        result = self.recognize(missing_rate=True)
        self.assertEqual(result["state"], "requires_review")
        orders = result["fields"]["order_proposals"]
        self.assertEqual(sum(float(row["net_amount"]) for row in orders), 1000.0)
        self.assertTrue(all(row.get("vat_amount") is None for row in orders))
        self.assertTrue(result["fields"]["order_proposal_warnings"])
        self.assertTrue(all("object_id" not in row for row in orders))

    def test_conflicting_repeated_order_total_stays_ambiguous(self):
        result = self.recognize(conflicting_total=True)
        order = result["fields"]["order_proposals"][2]
        self.assertTrue(order["ambiguous"])
        self.assertEqual(order["net_amount"], "400.00")
        self.assertTrue(any("разные суммы" in warning for warning in result["fields"]["order_proposal_warnings"]))

    def test_text_only_duplicate_order_totals_are_not_summed_twice(self):
        result = parse_project_order_allocations([
            {"page": 1, "text": "Proj.nr.: P1\nOrder nr: A1\nSumma order: 100.00"},
            {"page": 2, "text": "Proj.nr.: P1\nOrder nr: A1\nSumma order: 100.00"},
            {"page": 3, "text": "Proj.nr.: P2\nOrder nr: B1\nSumma order: 200.00"},
        ])
        self.assertEqual(len(result["orders"]), 2)
        self.assertEqual(result["net_total"], "300.00")

    def test_order_confirmation_does_not_propose_customer_as_invoice_supplier_or_date(self):
        fields = parse_invoice_pages([{
            "page": 1,
            "text": (
                "ORDERBEKRÄFTELSE\nSupplier: Customer AB\nOrg.nr: 556000-0000\n"
                "Ordernummer: ORD-123\nOrderdatum: 2026-10-13\n"
                "Netto: 100.00\nMoms: 25.00\nSumma totalt: 125.00"
            ),
        }])
        self.assertEqual(fields["non_invoice_document_type"], "order_confirmation")
        self.assertIsNone(fields["supplier_name"])
        self.assertIsNone(fields["supplier_org_number"])
        self.assertIsNone(fields["invoice_number"])
        self.assertIsNone(fields["document_date"])
        self.assertEqual(fields["net_amount"], "100.00")
        self.assertEqual(fields["vat_amount"], "25.00")
        self.assertEqual(fields["gross_amount"], "125.00")


if __name__ == "__main__":
    unittest.main()
