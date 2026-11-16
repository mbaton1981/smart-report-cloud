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


if __name__ == "__main__":
    unittest.main()
