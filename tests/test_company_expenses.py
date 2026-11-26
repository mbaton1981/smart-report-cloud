import unittest
from contextlib import closing
from datetime import date
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import test_catalog_sync as catalog
from company_expenses import (FUEL, ROAD, SOFTWARE, TAX, UNKNOWN, VEHICLE_TAX, report_shares,
                              review_proposals, section, suggest, totals)
from company_expenses_ui import ArticleReview, CompanyExpensesWidget, comparison_range
from expense_documents import migrate_invoice_net_schema


def example():
    return dict(id=10, sync_version=1, verification_status="confirmed", currency_code="SEK",
                supplier_name="St1", document_date=date.today().isoformat(), invoice_number="EXAMPLE",
                allocations=[dict(allocation_id=7, object_id=99, destination="object", gross_amount="100"),
                             dict(allocation_id=8, destination="company", net_amount="80", gross_amount="100", note="Diesel")])


class ReportingTests(unittest.TestCase):
    def test_company_shares_only_unknown_net_credit_currency_and_tax(self):
        doc = example()
        share = doc["allocations"][1]
        documents = [doc, dict(doc, verification_status="draft"), dict(doc, supporting_to_document_id=1),
                     dict(doc, allocations=[dict(share, destination="unselected")]),
                     dict(doc, allocations=[dict(share, net_amount=None)]),
                     dict(doc, allocations=[dict(share, net_amount="-20")])]
        rows = report_shares(documents)
        self.assertEqual(totals(rows, "net"), ({UNKNOWN: Decimal(60)}, {UNKNOWN: 1}))
        self.assertEqual(totals(rows, "gross"), ({UNKNOWN: Decimal(300)}, {}))
        self.assertEqual(totals(report_shares([dict(doc, currency_code=None)]), "gross"), ({}, {UNKNOWN: 1}))
        self.assertEqual(suggest(doc, share)[0], FUEL)
        self.assertEqual(suggest(dict(doc, recognized_text="Diesel", supplier_name="OpenAI"), {})[0], UNKNOWN)
        self.assertEqual(suggest({}, dict(note="Fordonsskatt"))[0], VEHICLE_TAX)
        self.assertEqual(section(VEHICLE_TAX), TAX)
        self.assertEqual(suggest({}, dict(note="Trängselskatt"))[0], ROAD)
        self.assertEqual(suggest(dict(doc, allocations=[share], supplier_name="GitHub, Inc."), {})[0], SOFTWARE)

    def test_review_uses_real_ai_response_and_only_company_description(self):
        doc = example()
        doc["allocations"][1]["note"] = "ANONYM TJÄNST"
        calls = []
        def request(method, path, **kw):
            if method == "GET":
                return {"document": doc}
            calls.append(kw["json"]["groups"])
            return {"suggestions": [dict(id="1", category=SOFTWARE, evidence_lines=[1], confidence=.99)]}
        chosen = review_proposals(report_shares([doc]), request, Mock(), [])
        self.assertEqual(calls, [[dict(id="1", lines=["ANONYM TJÄNST"])]])
        self.assertEqual(chosen[10][8]["category"], SOFTWARE)
        self.assertNotIn("company_article", doc["allocations"][1])

    def test_equal_elapsed_months_and_leap_year(self):
        self.assertEqual(comparison_range(date(2024, 3, 1), date(2024, 3, 31), True),
                         (date(2024, 2, 1), date(2024, 2, 29)))
        self.assertEqual(comparison_range(date(2026, 1, 1), date(2026, 1, 4), True),
                         (date(2025, 12, 1), date(2025, 12, 4)))

    def test_company_ai_taxonomy_has_separate_provider_cache(self):
        import json
        import os
        import sqlite3
        from types import SimpleNamespace
        from unittest.mock import patch
        import assistant_api
        from expense_category_ai import request_categories
        from expense_categories import CATEGORY_OTHER
        groups = [dict(id="1", lines=["Fordonskostnad"])]
        replies = iter((CATEGORY_OTHER, VEHICLE_TAX))
        def post(_url, **kw):
            label = next(replies)
            item = dict(id="1", category=label, confidence=.99, evidence_lines=[1])
            schema = kw["json"]["text"]["format"]["schema"]
            self.assertIn(label, schema["properties"]["suggestions"]["items"]["properties"]["category"]["enum"])
            payload = json.dumps({"suggestions": [item]})
            return SimpleNamespace(status_code=200, json=lambda: dict(status="completed", output=[
                dict(type="message", content=[dict(type="output_text", text=payload)])]))
        with closing(sqlite3.connect(":memory:")) as conn, patch.dict(os.environ, OPENAI_API_KEY="synthetic"):
            assistant_api.ensure_schema(conn)
            conn.execute("UPDATE assistant_settings SET enabled=1")
            conn.commit()
            for company, expected in ((False, CATEGORY_OTHER), (True, VEHICLE_TAX), (True, VEHICLE_TAX)):
                result = request_categories(conn, {"user_id": 1}, dict(groups=groups, operation_id=str(uuid4())),
                                            post, company=company)
                self.assertEqual(result[0]["category"], expected)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM expense_category_ai_cache").fetchone()[0], 2)


class CompanyFlowTests(unittest.TestCase):
    setUpClass = classmethod(catalog.CatalogSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog.CatalogSyncTests.tearDownClass.__func__)
    setUp = catalog.CatalogSyncTests.setUp
    tearDown = catalog.CatalogSyncTests.tearDown
    _add_company = catalog.CatalogSyncTests._add_company

    def test_endpoint_atomic_review_preserves_money_and_survives_legacy_edit(self):
        with closing(self.server.get_db_connection()) as conn:
            migrate_invoice_net_schema(conn)
        headers = {"X-Sync-Key": "isolated-sync-test-key"}
        payload = dict(client_id=uuid4().hex, supplier_name="Test", invoice_number="11", currency_code="SEK",
                       document_date="2026-11-01", verification_status="confirmed", allocation_basis="net",
                       net_amount="100.00", vat_amount="25.00", gross_amount="125.00", amount_due="125.00",
                       rounding_amount="0.00", allocations=[
                           dict(destination="company", net_amount="80.00", vat_amount="20.00", note="diesel"),
                           dict(destination="company", net_amount="20.00", vat_amount="5.00", note="parkering")])
        client = self.server_client
        base = "/api/expense-documents"
        created = client.post(base, json=payload, headers=headers)
        self.assertEqual(created.status_code, 201, created.get_json())
        doc = created.get_json()["document"]
        path = base + "/company-articles"
        changes = [dict(document_id=doc["id"], expected_version=doc["sync_version"], allocation_id=s["allocation_id"],
                        article=a, evidence="Выбрано вручную") for s, a in zip(doc["allocations"], (FUEL, ROAD), strict=True)]
        self.assertEqual(client.post(path, json={"changes": changes}).status_code, 403)
        doc_path = base + "/" + str(doc["id"])
        self.assertEqual(client.post(path, json={"changes": [changes[0], dict(changes[1], expected_version=99)]}, headers=headers).status_code, 409)
        self.assertFalse(client.get(doc_path, headers=headers).get_json()["document"]["allocations"][0]["company_article"])
        result = client.post(path, json={"changes": changes}, headers=headers)
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual(client.post(path, json={"changes": changes}, headers=headers).status_code, 409)
        current = client.get(doc_path, headers=headers).get_json()["document"]
        payload.update(expected_version=current["sync_version"], document_comment="Проверено")
        saved = client.put(doc_path, json=payload, headers=headers)
        self.assertEqual(saved.status_code, 200, saved.get_json())
        parts = saved.get_json()["document"]["allocations"]
        self.assertEqual([(p["company_article"], p["net_amount"]) for p in parts], [(FUEL, "80.00"), (ROAD, "20.00")])

    def test_qt_overview_review_and_open_original_document_identity(self):
        from PyQt6.QtTest import QTest
        reveal = Mock()
        doc = example()
        rows = report_shares([doc])
        editor = ArticleReview(rows, {10: {8: dict(category=FUEL, evidence="Diesel")}}, None)
        self.assertEqual(editor.changes()[0]["allocation_id"], 8)
        editor.entries[0][0].setChecked(False)
        self.assertEqual(editor.changes(), [])
        editor.close()
        doc["allocations"][1]["company_article"] = FUEL
        tax = dict(doc, id=20, allocations=[dict(doc["allocations"][1], company_article=VEHICLE_TAX, net_amount="500")])
        window = CompanyExpensesWidget(lambda *_: [doc, tax], Mock(), Mock(), reveal, self.desktop.ObjectOverviewTask)
        window.resize(900, 600)
        window.show()
        window.reload()
        for _ in range(100):
            QTest.qWait(10)
            if window.loaded:
                break
        self.assertTrue(window.loaded)
        self.assertEqual(window.chart.values, {FUEL: Decimal(80)})
        window.select_article(FUEL)
        window.documents.selectRow(0)
        window.open_button.click()
        self.assertEqual(reveal.call_args.args[0]["id"], 10)
        window.sections.setCurrentIndex(1)
        self.assertEqual(window.chart.values, {VEHICLE_TAX: Decimal(500)})
        window.close()
