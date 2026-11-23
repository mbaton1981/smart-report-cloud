import unittest

from expense_categories import (
    CATEGORY_MATERIALS,
    CATEGORY_SUBCONTRACTOR_MIXED,
    CATEGORY_SUBCONTRACTOR_WORK,
    CATEGORY_UNCLASSIFIED,
    suggest_expense_category,
)


class ExpenseCategorySuggestionTests(unittest.TestCase):
    def test_suggests_contractor_work_from_invoice_content_not_supplier(self):
        category, evidence = suggest_expense_category(
            {
                "supplier_name": "A supplier that does not identify category",
                "recognized_text": "Rad 1: Arbete utförd på objektet",
            },
            {},
            allocation_count=1,
        )

        self.assertEqual(category, CATEGORY_SUBCONTRACTOR_WORK)
        self.assertIn("Arbete utförd", evidence)

    def test_suggests_materials_from_allocation_note(self):
        category, evidence = suggest_expense_category(
            {"supplier_name": "A contractor"},
            {"note": "Material: virke och skruv"},
            allocation_count=2,
        )

        self.assertEqual(category, CATEGORY_MATERIALS)
        self.assertIn("virke", evidence)

    def test_explicit_mixed_description_is_not_silently_split(self):
        category, _evidence = suggest_expense_category(
            {"recognized_text": "Arbete och material, ej specificerat"},
            {},
            allocation_count=1,
        )

        self.assertEqual(category, CATEGORY_SUBCONTRACTOR_MIXED)

    def test_multi_allocation_invoice_needs_part_specific_content(self):
        category, _evidence = suggest_expense_category(
            {"recognized_text": "Arbete utfört; material levererat"},
            {},
            allocation_count=2,
        )

        self.assertEqual(category, CATEGORY_UNCLASSIFIED)

    def test_construction_products_are_recognized_from_order_lines(self):
        for content in ("FLATBORRSET DT70751\nKNIVBLAD FATMAX", "TRÄSKRUV 4,5X50\nBYGGREGEL G4-2",
                        "FRAKT 245,00\nGIPSSKIVA GNE 13X900X2500\nOSB/3 11X897X2500",
                        "FRAKT ZON 1\nFANERBALK 45X220\nEMBALLAGE"):
            with self.subTest(content=content):
                category, evidence = suggest_expense_category({}, {"note": content}, 3)
                self.assertEqual(category, CATEGORY_MATERIALS)
                self.assertIn("Пояснение к доле", evidence)
        category, _ = suggest_expense_category({}, {"note": "Hyra av verktyg; GIPSSKIVA"})
        self.assertEqual(category, CATEGORY_UNCLASSIFIED)

    def test_same_supplier_does_not_assign_a_category_without_content(self):
        category, _ = suggest_expense_category({"supplier_name": "BEIJER BYGGMATERIAL AB"}, {}, 2)
        self.assertEqual(category, CATEGORY_UNCLASSIFIED)


if __name__ == "__main__":
    unittest.main()
