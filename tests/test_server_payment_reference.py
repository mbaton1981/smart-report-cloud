import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ServerPaymentReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.environment = patch.dict(os.environ, {
            "DATABASE_PATH": str(Path(cls.temporary.name) / "bootstrap.db"),
            "SMARTREPORT_DOCUMENTS_DIR": str(Path(cls.temporary.name) / "originals"),
            "ADMIN_PASSWORD": "isolated-reference-test", "SECRET_KEY": "isolated-reference-test",
            "SYNC_API_KEY": "isolated-reference-test",
        })
        cls.environment.start()
        import server
        cls.server = server

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()
        cls.temporary.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        db = patch.object(self.server, "DB_FILE", str(Path(self.work.name) / "test.db"))
        db.start()
        self.addCleanup(db.stop)
        key = patch.object(self.server, "SYNC_API_KEY", "isolated-reference-test")
        key.start()
        self.addCleanup(key.stop)
        self.server.init_cloud_db()
        self.client = self.server.app.test_client()
        self.headers = {"X-Sync-Key": "isolated-reference-test"}
        self.payload = {
            "client_id": "reference-test", "supplier_name": "Isolated AB", "invoice_number": "R-1",
            "document_date": "2026-10-21", "currency_code": "SEK", "gross_amount": "125.00",
            "vat_amount": "25.00", "payment_reference": "00118869", "allocations": [],
        }

    def test_create_get_retry_update_omit_and_clear_preserve_string(self):
        caps = self.client.get("/api/expense-documents/capabilities", headers=self.headers).get_json()
        self.assertIn("payment_reference_v1", caps["features"])
        created = self.client.post("/api/expense-documents", json=self.payload, headers=self.headers)
        self.assertEqual(created.status_code, 201)
        card = created.get_json()["document"]
        self.assertEqual(card["payment_reference"], "00118869")
        endpoint = f"/api/expense-documents/{card['id']}"
        self.assertEqual(self.client.get(endpoint, headers=self.headers).get_json()["document"]["payment_reference"], "00118869")
        retry = self.client.post("/api/expense-documents", json=self.payload, headers=self.headers)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.get_json()["document"]["id"], card["id"])
        update = {**self.payload, "expected_version": card["sync_version"]}
        del update["payment_reference"]
        updated = self.client.put(endpoint, json=update, headers=self.headers)
        self.assertEqual(updated.status_code, 200, updated.get_json())
        card = updated.get_json()["document"]
        self.assertEqual(card["payment_reference"], "00118869")
        update.update(payment_reference="", expected_version=card["sync_version"])
        cleared = self.client.put(endpoint, json=update, headers=self.headers)
        self.assertEqual(cleared.status_code, 200, cleared.get_json())
        self.assertIsNone(cleared.get_json()["document"]["payment_reference"])

    def test_numeric_and_oversized_reference_are_rejected(self):
        for value in (118869, True, ["00123"], "0" * 201):
            with self.subTest(value=type(value).__name__):
                response = self.client.post(
                    "/api/expense-documents", json={**self.payload, "payment_reference": value},
                    headers=self.headers,
                )
                self.assertEqual(response.status_code, 400, response.get_json())
