import copy
import json
import os
import sqlite3
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import assistant_api
import expense_category_ai as ai
from expense_categories import CATEGORY_MATERIALS, CATEGORY_UNCLASSIFIED
from expense_item_content import single_allocation_content
import test_assistant as assistant_tests


class ExpenseCategoryAITests(unittest.TestCase):
    def groups(self):
        return [{"id": "1", "lines": ["SLIPPAPPER STF GR D125", "MASKERINGSTEJP UV STANDARD"]}]

    def suggestion(self, **changes):
        return dict(dict(id="1", category=CATEGORY_MATERIALS, confidence=.99, evidence_lines=[1, 2]), **changes)

    def connection(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        assistant_api.ensure_schema(conn)
        conn.execute("UPDATE assistant_settings SET enabled=1")
        conn.commit()
        return conn

    def test_evidence_confidence_ids_and_categories_are_checked(self):
        for change in ({"id": "other"}, {"category": "invented"}, {"evidence_lines": [3]},
                       {"evidence_lines": [True]}, {"confidence": float("nan")}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ai.validate_suggestions(self.groups(), [self.suggestion(**change)])
        for change in ({"confidence": .89}, {"evidence_lines": []}):
            result = ai.validate_suggestions(self.groups(), [self.suggestion(**change)])
            self.assertEqual(result["1"]["category"], CATEGORY_UNCLASSIFIED)

    def test_provider_budget_cache_and_no_raw_content_in_cache(self):
        conn = self.connection()
        calls = []
        response = SimpleNamespace(status_code=200, json=lambda: dict(status="completed", output=[dict(
            type="message", content=[dict(type="output_text", text=json.dumps({"suggestions": [self.suggestion()]}))])]))
        def post(url, **kwargs):
            calls.append(kwargs)
            self.assertFalse(kwargs["json"]["store"])
            self.assertFalse(kwargs["allow_redirects"])
            return response
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-only"}):
            for _ in range(2):
                result = ai.request_categories(conn, {"user_id": 1}, dict(operation_id=str(uuid.uuid4()), groups=self.groups()), post)
                self.assertEqual(result[0]["category"], CATEGORY_MATERIALS)
        self.assertEqual(len(calls), 1)
        self.assertEqual(conn.execute("SELECT kind,state FROM assistant_requests").fetchall(), [("expense_categories", "suggested")])
        self.assertNotIn("SLIPPAPPER", str(conn.execute("SELECT * FROM expense_category_ai_cache").fetchall()))

    def test_failed_or_disabled_provider_never_returns_guessed_category(self):
        conn = self.connection()
        response = SimpleNamespace(status_code=200, json=lambda: dict(status="incomplete"))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-only"}):
            with self.assertRaises(assistant_api.AssistantError):
                ai.request_categories(conn, {"user_id": 1}, dict(operation_id=str(uuid.uuid4()), groups=self.groups()), lambda *a, **k: response)
            self.assertEqual(conn.execute("SELECT state FROM assistant_requests").fetchone()[0], "failed")
            conn.execute("UPDATE assistant_settings SET enabled=0")
            conn.commit()
            with self.assertRaises(assistant_api.AssistantError):
                ai.request_categories(conn, {"user_id": 1}, dict(operation_id=str(uuid.uuid4()), groups=self.groups()), lambda *a, **k: self.fail("must not call provider"))

    def test_client_only_changes_unknown_categories_and_sends_descriptions(self):
        proposals = {99: {0: dict(category=CATEGORY_UNCLASSIFIED, content="008649517 MASKERINGSTEJP UV STANDARD 1,00 RLE 45,60", evidence=""),
                          1: dict(category=CATEGORY_MATERIALS, content="confirmed", evidence="keep")}}
        before = copy.deepcopy(proposals)
        def request(payload):
            self.assertEqual(payload["groups"], [{"id": "1", "lines": ["MASKERINGSTEJP UV STANDARD"]}])
            self.assertNotIn("99", json.dumps(payload["groups"]))
            return {"suggestions": [self.suggestion(evidence_lines=[1])]}
        ai.classify_unknown(proposals, request)
        self.assertEqual(proposals[99][0]["category"], CATEGORY_MATERIALS)
        self.assertEqual(proposals[99][0]["content"], before[99][0]["content"])
        self.assertEqual(proposals[99][1], before[99][1])
        self.assertEqual(ai.product_lines("SWIFT SECRET\nEmail me@example.com\nIBAN private"), [])

    def test_two_row_items_do_not_require_vat_order_numbers_or_read_footer(self):
        def cell(text, x, y):
            return dict(text=text, x0=x, y0=y, x1=x+80, y1=y+8)
        elements = [cell("Produktbeskrivning", 20, 10), cell("Antal", 220, 10),
                    cell("1282509", 20, 30), cell("1 ST", 220, 30), cell("72,72", 300, 30),
                    cell("G 45X70 G4-2 REGEL 3,6M", 20, 45), cell("25,0%", 300, 45),
                    cell("Bank: private", 20, 70), cell("Private address", 20, 85)]
        document = dict(gross_amount="125.00", allocations=[dict(gross_amount="125.00")])
        with patch("expense_item_content.parse_invoice_pages", return_value={}):
            self.assertEqual(single_allocation_content([dict(elements=elements)], document), "G 45X70 G4-2 REGEL 3,6M")
        document["allocations"].append(dict(gross_amount="125.00"))
        self.assertEqual(single_allocation_content([], document), "")


class ExpenseCategoryEndpointTests(unittest.TestCase):
    setUpClass = classmethod(assistant_tests.AssistantApiTests.setUpClass.__func__)
    tearDownClass = classmethod(assistant_tests.AssistantApiTests.tearDownClass.__func__)
    _add_company = assistant_tests.AssistantApiTests._add_company
    headers = assistant_tests.AssistantApiTests.headers
    setUp = assistant_tests.AssistantApiTests.setUp

    def test_authenticated_suggestion_has_no_business_writes(self):
        path = "/api/assistant/expense-categories"
        groups = [{"id": "1", "lines": ["MASKERINGSTEJP UV STANDARD"]}]
        payload = dict(operation_id=str(uuid.uuid4()), groups=groups)
        self.assertEqual(self.server_client.post(path, json=payload).status_code, 403)
        self.assertEqual(self.server_client.post(path, json=payload, headers=self.headers("user")).status_code, 403)
        before = assistant_tests.AssistantApiTests.business(self)
        suggestion = dict(id="1", category=CATEGORY_MATERIALS, confidence=.99, evidence_lines=[1])
        response = SimpleNamespace(status_code=200, json=lambda: dict(status="completed", output=[dict(type="message", content=[dict(type="output_text", text=json.dumps({"suggestions": [suggestion]}))])]))
        with patch("expense_category_ai.requests.post", return_value=response):
            result = self.server_client.post(path, json=payload, headers=self.headers())
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual(result.get_json()["suggestions"], [suggestion])
        self.assertIn("no-store", result.headers["Cache-Control"])
        self.assertEqual(assistant_tests.AssistantApiTests.business(self), before)
