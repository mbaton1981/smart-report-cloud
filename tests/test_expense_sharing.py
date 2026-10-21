import hashlib
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import test_incoming_integration as integration
from test_mail_automation import pdf
from offline_review import ReviewError


class ExpenseSharingTests(unittest.TestCase):
    setUpClass = classmethod(integration.IncomingIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(integration.IncomingIntegrationTests.tearDownClass.__func__)
    _add_company = integration.IncomingIntegrationTests._add_company
    setUp = integration.IncomingIntegrationTests.setUp
    stage = integration.IncomingIntegrationTests.stage
    request = integration.IncomingIntegrationTests.request
    sync = integration.IncomingIntegrationTests.sync

    def configure_incoming(self):
        projects = self.projects()
        self.host._expense_objects = [{"id": project, "name": f"Test project {index}"}
                                      for index, project in enumerate(projects)]
        self.host.incoming_register_snapshot = self.documents
        self.host.incoming_delivery_pending = lambda client_id: (
            self.desktop.SmartReportApp.incoming_delivery_pending(self.host, client_id))
        self.host.refresh_incoming_register = lambda: None
        return projects

    def browser_edit(self, document, amounts, *, operation=None):
        payload = {key: document.get(key) for key in (
            "client_id", "supplier_name", "invoice_number", "document_date", "currency_code",
            "gross_amount", "vat_amount", "verification_status", "allocation_basis")}
        payload.update(expected_version=document["sync_version"],
                       allocations=[{"object_id": row["object_id"], "gross_amount": amount}
                                    for row, amount in zip(document["allocations"], amounts)])
        if operation:
            payload["cost_operation_id"] = operation
        with self.server_client.session_transaction() as session:
            session.update(role="admin", expense_csrf_token="isolated-csrf")
        response = self.server_client.put(f"/api/expense-documents/{document['id']}", json=payload,
                                          headers={"X-CSRF-Token": "isolated-csrf"})
        with self.server_client.session_transaction() as session:
            session.clear()
        return response

    def test_incoming_qt_three_parts_web_update_refresh_stale_guard_and_lost_ack(self):
        from offline_review_ui import OfflineReviewWindow
        projects = self.configure_incoming()
        original = self.store.original(1).read_bytes()
        window = OfflineReviewWindow(self.store, integrated=True)
        try:
            window.expense_button.click()
            self.assertEqual(window.inputs["gross_amount"].text(), "125.00")
            self.assertEqual(window.inputs["vat_amount"].text(), "25.00")
            window.add_allocation({"object_id": projects[0], "gross_amount": "20.00"})
            self.assertTrue(window.save_edits())  # Partial distribution is a draft, not an expense.
            self.assertIsNone(self.store.card(1))
            self.assertTrue(self.store.issues(1, window.payload()))
            window.add_allocation({"object_id": projects[1], "gross_amount": "45.00"})
            window.add_allocation({"object_id": projects[2], "gross_amount": "60.00"})
            window.checked.setChecked(True)
            with patch.object(self.desktop.QMessageBox, "question",
                              return_value=self.desktop.QMessageBox.StandardButton.Yes):
                window.confirm_transfer()
            self.assertIsNotNone(self.store.card(1))
            self.mode = "lost_ack"
            self.assertEqual(self.sync()["synced"], 0)
            self.assertEqual(self.sync()["synced"], 1, self.last_sync)
            window.select_document()
            self.assertEqual(window.status.text(), "Учтён на объекте")
            document = self.documents()[0]
            self.assertEqual(document["verification_status"], "confirmed")
            self.assertEqual(len(document["allocations"]), 3)
            self.assertEqual(len(document["attachments"]), 1)
            self.assertEqual(self.store.original(1).read_bytes(), original)
            stale_payload = window.payload()
            web = self.browser_edit(document, ("25.00", "40.00", "60.00"), operation="web-retry")
            self.assertEqual(web.status_code, 200, web.get_json())
            repeated = self.browser_edit(document, ("25.00", "40.00", "60.00"), operation="web-retry")
            self.assertEqual(repeated.status_code, 200, repeated.get_json())
            self.assertTrue(repeated.get_json()["duplicate_request"])
            with self.assertRaisesRegex(ReviewError, "актуальное"):
                self.store.save(1, stale_payload, window.revision)
            with self.assertRaisesRegex(ReviewError, "актуальное"):
                self.store.reassign(1, projects[1], window.revision)
            window.refresh_distribution()
            self.assertEqual([part["gross_amount"] for part in window.payload()["allocations"]],
                             ["25.00", "40.00", "60.00"])
            window.allocations.item(0, 1).setText("30.00")
            window.allocations.item(1, 1).setText("35.00")
            self.assertTrue(window.save_edits())
            self.mode = "lost_ack"
            self.assertEqual(self.sync()["synced"], 0)
            self.assertEqual(self.sync()["synced"], 1, self.last_sync)
            updated = self.documents()[0]
            self.assertEqual(updated["id"], document["id"])
            self.assertEqual(updated["attachments"], document["attachments"])
            self.assertEqual([part["gross_amount"] for part in updated["allocations"]],
                             ["30.00", "35.00", "60.00"])
            self.assertEqual(len(self.documents()), 1)
            self.assertGreater(len(self.store.history(1)), 2)
        finally:
            window.close()

    def test_server_change_after_local_queue_is_explicit_conflict_not_overwrite(self):
        projects = self.configure_incoming()
        revision, payload = self.store.latest(1)
        payload.update(object_id=None, original_checked=True, allocations=[
            {"object_id": project, "gross_amount": amount}
            for project, amount in zip(projects, ("20.00", "45.00", "60.00"))])
        revision = self.store.save(1, payload, revision)
        self.store.transfer(1, revision)
        self.assertEqual(self.sync()["synced"], 1, self.last_sync)
        document = self.documents()[0]
        revision, current = self.store.current_review(1)
        current["allocations"][0]["gross_amount"] = "25.00"
        current["allocations"][1]["gross_amount"] = "40.00"
        self.store.save(1, current, revision)
        web = self.browser_edit(document, ("15.00", "50.00", "60.00"))
        self.assertEqual(web.status_code, 200, web.get_json())
        result = self.sync()
        self.assertEqual(result["conflicts"], 1, result)
        self.assertEqual([row["gross_amount"] for row in self.documents()[0]["allocations"]],
                         ["15.00", "50.00", "60.00"])
        pending = self.host.incoming_delivery_pending(self.store.card(1)["client_id"])
        self.assertEqual(pending["state"], "conflict")
        self.assertIsNotNone(pending["last_error"])
        self.assertEqual(len(self.documents()), 1)

    def test_update_queue_commit_crash_replays_same_revision_and_document(self):
        projects = self.configure_incoming()
        revision, payload = self.store.latest(1)
        payload.update(object_id=None, original_checked=True, allocations=[
            {"object_id": project, "gross_amount": amount}
            for project, amount in zip(projects, ("20.00", "45.00", "60.00"))])
        revision = self.store.save(1, payload, revision)
        card = self.store.transfer(1, revision)
        self.assertEqual(self.sync()["synced"], 1, self.last_sync)
        revision, payload = self.store.current_review(1)
        payload["allocations"][0]["gross_amount"] = "30.00"
        payload["allocations"][1]["gross_amount"] = "35.00"
        queue = self.host.queue_expense_document_operation
        def crash_after_queue(*args, **kwargs):
            queue(*args, **kwargs)
            raise OSError("isolated update crash after durable queue commit")
        self.host.queue_expense_document_operation = crash_after_queue
        with self.assertRaises(OSError):
            self.store.save(1, payload, revision)
        self.host.queue_expense_document_operation = queue
        updated_revision = self.store.latest(1)[0]
        self.store.transfer(1, updated_revision)
        self.mode = "lost_ack"
        self.assertEqual(self.sync()["synced"], 0)
        self.assertEqual(self.sync()["synced"], 1, self.last_sync)
        documents = self.documents()
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["client_id"], card["client_id"])
        self.assertEqual(documents[0]["sync_version"], 2)
        self.assertEqual([row["gross_amount"] for row in documents[0]["allocations"]],
                         ["30.00", "35.00", "60.00"])

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
        self.assertEqual(response.get_json()["document"]["verification_status"], "confirmed")

    def test_object_cost_requires_full_allocation_but_allows_partial_draft(self):
        projects = self.projects()
        payload = {
            "client_id": "partial-allocation-example", "supplier_name": "Waste test AB",
            "document_date": "2026-10-20", "currency_code": "SEK", "gross_amount": "3000.00",
            "vat_amount": None, "usage": "object_cost", "verification_status": "confirmed",
            "allocations": [{"object_id": projects[0], "gross_amount": "600.00"}],
        }
        response = self.server_client.post("/api/expense-documents", json=payload, headers={
            "X-Sync-Key": "isolated-sync-test-key"})
        self.assertEqual(response.status_code, 400, response.get_json())
        payload["verification_status"] = "draft"
        response = self.server_client.post("/api/expense-documents", json=payload, headers={
            "X-Sync-Key": "isolated-sync-test-key"})
        self.assertEqual(response.status_code, 201, response.get_json())
        document = response.get_json()["document"]
        self.assertEqual(document["unallocated_gross"], "2400.00")
        self.assertEqual(document["verification_status"], "draft")
