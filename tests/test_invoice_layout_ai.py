import json
import os
import sqlite3
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import assistant_api
import invoice_layout_ai as ai
from invoice_processing import recognize_invoice


def pages():
    def cell(text, x, y):
        return dict(text=text, x0=x, y0=y, x1=x + 100, y1=y + 10)
    return [dict(source="private-original.pdf", page=1, text="Private invoice",
                 elements=[cell("Seller", 0, 0), cell("Private Supplier AB", 0, 20),
                           cell("Buyer", 0, 170), cell("Private Buyer AB", 0, 190),
                           cell("Invoice number", 0, 50), cell("TEST-543", 160, 50),
                           cell("Net amount", 0, 80), cell("100.00", 160, 80),
                           cell("VAT", 0, 100), cell("25.00", 160, 100),
                           cell("Gross amount", 0, 120), cell("125.00", 160, 120)])]


def selection(private):
    result = {field: dict(value=None, caption=None) for field in ai.FIELDS}
    for field, raw in (("supplier_name", "Private Supplier AB"), ("invoice_number", "TEST-543"),
                       ("net_amount", "100.00"), ("vat_amount", "25.00"), ("gross_amount", "125.00")):
        result[field] = dict(value=next(k for k, v in private.items() if v["value"] == raw),
                             caption=next(k for k, v in private.items()
                                          if v["label"] == ("seller" if field == "supplier_name" else field)))
    return result


class InvoiceLayoutAITests(unittest.TestCase):
    def test_only_opaque_geometry_reaches_provider_and_local_values_are_verified(self):
        layout, private = ai.masked_layout(pages())
        serialized = json.dumps(layout)
        for secret in ("Private", "TEST-543", "100.00", "25.00", "125.00", "private-original.pdf"):
            self.assertNotIn(secret, serialized)
        fields = ai.apply_selection({}, selection(private), private)
        self.assertEqual(fields["supplier_name"], "Private Supplier AB")
        self.assertEqual(fields["invoice_number"], "TEST-543")
        self.assertEqual(fields["gross_amount"], "125.00")
        self.assertTrue(fields["ai_recognition"]["requires_review"])
        self.assertEqual(fields["field_sources"]["supplier_name"][0]["kind"], "ai_layout_selection")

    def test_buyer_false_evidence_unknown_ids_and_inconsistent_totals_are_rejected(self):
        _, private = ai.masked_layout(pages())
        chosen = selection(private)
        chosen["supplier_name"]["value"] = next(k for k, v in private.items() if v["value"] == "Private Buyer AB")
        with self.assertRaises(ValueError):
            ai.apply_selection({}, chosen, private)
        chosen = selection(private)
        chosen["invoice_number"]["value"] = 9999
        with self.assertRaises(ValueError):
            ai.apply_selection({}, chosen, private)
        chosen = selection(private)
        with self.assertRaises(ValueError):
            ai.apply_selection({"net_amount": "99.00"}, chosen, private)
        layout, _ = ai.masked_layout(pages())
        layout["blocks"][0]["text"] = "Private content"
        with self.assertRaises(ValueError):
            ai.validate_layout(layout)

    def test_existing_fields_not_overwritten_and_pipeline_never_confirms(self):
        extraction = SimpleNamespace(method="test", text=pages()[0]["text"], pages=[pages()[0]["text"]],
                                     page_elements=[pages()[0]["elements"]])
        def fake_provider(layout):
            _, private = ai.masked_layout(pages())
            return selection(private)
        with patch("expense_documents.extract_local_document", return_value=extraction):
            result = recognize_invoice(["test.pdf"], parser=lambda _: {"supplier_name": "Manual preserved"},
                                       ai_fallback=fake_provider)
        self.assertEqual(result["fields"]["supplier_name"], "Manual preserved")
        self.assertEqual(result["fields"]["invoice_number"], "TEST-543")
        self.assertEqual(result["state"], "requires_review")

    def test_existing_budget_and_provider_contract_with_mock_response(self):
        layout, private = ai.masked_layout(pages())
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        assistant_api.ensure_schema(conn)
        conn.execute("UPDATE assistant_settings SET enabled=1")
        conn.commit()
        picked = selection(private)
        response = SimpleNamespace(status_code=200, json=lambda: dict(
            status="completed", output=[dict(type="message", content=[dict(type="output_text", text=json.dumps(picked))])]))
        def fake_post(url, **kwargs):
            self.assertFalse(kwargs["json"]["store"])
            self.assertFalse(kwargs["allow_redirects"])
            self.assertNotIn("Private", json.dumps(kwargs["json"]))
            return response
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-only"}):
            result = ai.request_selection(conn, {"user_id": 1}, layout, str(uuid.uuid4()), post=fake_post)
        self.assertEqual(result, picked)
        self.assertEqual(conn.execute("SELECT kind,state FROM assistant_requests").fetchone(),
                         ("invoice_layout", "selected"))

    def test_malformed_provider_responses_fail_explicitly_without_result(self):
        layout, _ = ai.masked_layout(pages())
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        assistant_api.ensure_schema(conn)
        conn.execute("UPDATE assistant_settings SET enabled=1")
        conn.commit()
        for value in (None, {"output": [None]}, {"output": [{"type": "message", "content": None}]}):
            with self.subTest(value=value), patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-only"}):
                response = SimpleNamespace(status_code=200, json=lambda: value)
                with self.assertRaises(assistant_api.AssistantError):
                    ai.request_selection(conn, {"user_id": 1}, layout, str(uuid.uuid4()),
                                         post=lambda *args, **kwargs: response)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM assistant_requests WHERE state='failed'").fetchone()[0], 3)
