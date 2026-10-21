import hashlib
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import test_incoming_integration as integration
from test_mail_automation import pdf


class ExpenseSharingTests(unittest.TestCase):
    setUpClass = classmethod(integration.IncomingIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(integration.IncomingIntegrationTests.tearDownClass.__func__)
    _add_company = integration.IncomingIntegrationTests._add_company
    setUp = integration.IncomingIntegrationTests.setUp
    stage = integration.IncomingIntegrationTests.stage
    request = integration.IncomingIntegrationTests.request
    sync = integration.IncomingIntegrationTests.sync

    def projects(self):
        conn = self.server.get_db_connection()
        result = [self.object_id]
        for name in ("Waste project B", "Waste project C"):
            result.append(conn.execute("""INSERT INTO meta_objects(company,name,markning,is_active)
                VALUES ('Renatur',?,?,1)""", (name, name)).lastrowid)
        conn.commit()
        conn.close()
        return result

    def documents(self):
        return self.server_client.get("/api/expense-documents", headers={
            "X-Sync-Key": "isolated-sync-test-key"}).get_json()["documents"]

    def desktop_snapshot(self):
        # The desktop reads the same server API; the browser edit itself uses its session.
        with patch.object(self.desktop, "request_expense_api",
                          return_value={"documents": self.documents()}):
            return self.desktop.SmartReportApp._expense_document_snapshot(
                SimpleNamespace(exp_api_status_lbl=self.desktop.QLabel(), _expense_objects=[]), fresh=True)[0]

    def test_one_pdf_three_allocations_sync_browser_edit_and_desktop_refresh(self):
        projects = self.projects()
        content = pdf("One waste removal invoice for three projects")
        path = self.store.root / "three-projects.pdf"
        path.write_bytes(content)
        payload = {
            "client_id": "one-invoice-three-objects", "supplier_name": "Waste test AB",
            "invoice_number": "WASTE-TEST", "document_date": "2026-10-20",
            "currency_code": "SEK", "gross_amount": "3000.00", "vat_amount": None,
            "usage": "object_cost", "allocation_basis": "gross",
            "verification_status": "confirmed",
            "allocations": [{"object_id": project, "gross_amount": amount, "vat_amount": None}
                            for project, amount in zip(projects, ("600.00", "900.00", "1500.00"))],
        }
        self.host.queue_expense_document_operation(
            payload["client_id"], "upsert", payload, attachment_paths=[str(path)])
        self.mode = "lost_ack"
        self.assertEqual(self.sync()["synced"], 0)
        self.assertEqual(self.sync()["synced"], 1, self.last_sync)
        self.assertEqual(self.sync()["synced"], 0)
        documents = self.documents()
        self.assertEqual(len(documents), 1)
        document = documents[0]
        self.assertEqual(document["allocated_gross"], "3000.00")
        self.assertEqual(document["unallocated_gross"], "0.00")
        self.assertEqual(len(document["attachments"]), 1)
        self.assertEqual(document["attachments"][0]["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual([item["gross_amount"] for item in document["allocations"]],
                         ["600.00", "900.00", "1500.00"])
        for project, amount in zip(projects, ("600.00", "900.00", "1500.00")):
            summary = self.server_client.get(f"/api/expense-objects/{project}/expenses", headers={
                "X-Sync-Key": "isolated-sync-test-key"}).get_json()
            self.assertEqual(summary["documents"][0]["object_gross"], amount)
            self.assertEqual(summary["documents"][0]["id"], document["id"])
            original = self.server_client.get(f"/api/expense-documents/{document['id']}/original", headers={
                "X-Sync-Key": "isolated-sync-test-key"})
            self.assertEqual(hashlib.sha256(original.data).hexdigest(), hashlib.sha256(content).hexdigest())
            original.close()
        with self.server_client.session_transaction() as session:
            session.update(user_id=1, role="admin", expense_csrf_token="isolated-csrf")
        # Match the web editor: it sends allocations and the expected version, not a new PDF.
        edited = {key: value for key, value in payload.items() if key != "usage"}
        edited.update(gross_amount="3300.00", expected_version=document["sync_version"], allocations=[
            {"object_id": project, "destination": "object", "gross_amount": amount}
            for project, amount in zip(projects, ("700.00", "1100.00", "1500.00"))])
        response = self.server_client.put(f"/api/expense-documents/{document['id']}", json=edited,
                                          headers={"X-CSRF-Token": "isolated-csrf"})
        self.assertEqual(response.status_code, 200, response.get_json())
        refreshed = self.desktop_snapshot()
        self.assertEqual(len(refreshed), 1)
        self.assertEqual(refreshed[0]["gross_amount"], "3300.00")
        self.assertEqual(sum(Decimal(row["gross_amount"]) for row in refreshed[0]["allocations"]), Decimal("3300.00"))
        self.assertEqual(refreshed[0]["id"], document["id"])
        self.assertEqual(refreshed[0]["attachments"], document["attachments"])
        self.assertEqual(refreshed[0]["usage"], "object_cost")
        self.assertEqual(len(self.documents()), 1)
        # Match the current gross web form: no per-allocation VAT controls are present.
        edited.update(expected_version=refreshed[0]["sync_version"], vat_amount="600.00")
        response = self.server_client.put(f"/api/expense-documents/{document['id']}", json=edited,
                                          headers={"X-CSRF-Token": "isolated-csrf"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["document"]["verification_status"], "requires_review")

    def test_current_gross_editor_allows_explicit_unallocated_remainder(self):
        projects = self.projects()
        payload = {
            "client_id": "partial-allocation-example", "supplier_name": "Waste test AB",
            "document_date": "2026-10-20", "currency_code": "SEK", "gross_amount": "3000.00",
            "vat_amount": None, "usage": "object_cost", "verification_status": "confirmed",
            "allocations": [{"object_id": projects[0], "gross_amount": "600.00"}],
        }
        response = self.server_client.post("/api/expense-documents", json=payload, headers={
            "X-Sync-Key": "isolated-sync-test-key"})
        self.assertEqual(response.status_code, 201, response.get_json())
        document = response.get_json()["document"]
        self.assertEqual(document["unallocated_gross"], "2400.00")
        # This existing behaviour is evidence of a required tightening, not changed in this audit.
        self.assertEqual(document["verification_status"], "confirmed")
