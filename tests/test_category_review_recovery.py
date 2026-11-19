import copy
import unittest
from unittest.mock import patch

from expense_categories import CATEGORY_EQUIPMENT_RENTAL, CATEGORY_UNCLASSIFIED, suggest_expense_category
from expense_item_content import single_allocation_content
from expense_category_automation import category_review_amounts


class CategoryReviewRecoveryTests(unittest.TestCase):
    def test_rental_blocks_without_table_header(self):
        text = ('FAKTURA\nNr\n12785007\n'
                '1 PRODNR 8333225 KAPMASKIN -150MM VATTENKYLD\n'
                'UTH.DAT 261110 PERIOD 261110-261111 (2 BDAG) 376,00/STYCK 752,00\n'
                '2 ARTNR 05836528 DIAKLINGA BETONG 350X25,4MM\n'
                'DATUM 261111 Minsta avgift 494,50\n'
                'Summa orderrader 1.246,50\nSUMMA EXKL. MOMS 1.246,50\n')
        document = dict(invoice_number='12785007', currency_code='SEK', gross_amount='1558.12',
                        allocations=[dict(gross_amount='1558.12')])
        content = single_allocation_content([dict(text=text, elements=[])], document)
        self.assertIn('KAPMASKIN', content)
        self.assertNotIn('SUMMA', content)
        self.assertEqual(suggest_expense_category({}, {'note': content})[0], CATEGORY_EQUIPMENT_RENTAL)
        self.assertEqual(suggest_expense_category({'supplier_name': 'CRAMO AB'}, {})[0], CATEGORY_UNCLASSIFIED)
        mixed = content + '\n3 ARTNR 123456 GIPSSKIVA\nDATUM 261111 100,00'
        self.assertEqual(suggest_expense_category({}, {'note': mixed})[0], CATEGORY_UNCLASSIFIED)

    def test_review_recovers_without_changing_saved_document(self):
        document = dict(id=44, client_id='test', verification_status='confirmed', currency_code='SEK',
                        document_date='2026-11-01', allocations=[dict(object_id=1, destination='object',
                        gross_amount='125.00', net_amount=None, vat_amount=None, rounding_amount=None)])
        before = copy.deepcopy(document)
        part = dict(object_options=[dict(id=1)], gross_amount='125.00', net_amount='100.00',
                    vat_amount='25.00', rounding='0.00')
        from types import SimpleNamespace
        with patch('object_package_report.extract_local_document', return_value=SimpleNamespace(pages=['test'], text='test', page_elements=[])), \
                patch('object_package_report.verified_original_order_allocation_proposal', return_value=[part]):
            amounts = category_review_amounts(document, [dict(id=1, markning='P1')], ['test.pdf'])
        self.assertEqual(amounts[0]['net_amount'], '100.00')
        self.assertEqual(document, before)
        amounts = category_review_amounts(document, [dict(id=1, markning='P1')], errors='Оригинал недоступен')
        self.assertIsNone(amounts[0]['net_amount'])
        self.assertIn('недоступен', amounts[0]['explanation'])
