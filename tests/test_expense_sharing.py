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
    approve = integration.IncomingIntegrationTests.approve
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
        window = OfflineReviewWindow(self.store, integrated=True, document_id=1)
        try:
            self.assertEqual(window.inputs["gross_amount"].text(), "125.00")
            self.assertEqual(window.inputs["vat_amount"].text(), "25.00")
            first = window.allocations.cellWidget(0, 0)
            first.setCurrentIndex(first.findData(projects[0]))
            window.allocations.item(0, 1).setText("20.00")
            self.assertTrue(window.save_edits())  # Partial distribution is a draft, not an expense.
            self.assertIsNone(self.store.card(1))
            self.assertTrue(self.store.issues(1, window.payload()))
            window.add_allocation({"object_id": projects[1], "gross_amount": "45.00"})
            window.add_allocation({"object_id": projects[2], "gross_amount": "60.00"})
            window.checked.setChecked(True)
            window.expense_button.click()
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

    def test_category_edit_and_support_link_are_explicit_net_neutral_and_versioned(self):
        from expense_categories import CATEGORY_SUBCONTRACTOR_WORK
        from object_package_report import collect_object_expenses

        projects = self.configure_incoming()
        capabilities = self.server_client.get(
            "/api/expense-documents/capabilities",
            headers={"X-Sync-Key": "isolated-sync-test-key"},
        ).get_json()
        self.assertIn("allocation_categories_v1", capabilities["features"])
        self.assertIn("expense_support_links_v1", capabilities["features"])
        revision, supporting_payload = self.store.latest(2)
        supporting_payload["document_date"] = "2026-10-22"
        self.store.save(2, supporting_payload, revision)
        for document_id in (1, 2):
            revision, payload = self.store.latest(document_id)
            payload.update(
                confirmed_type="receipt" if document_id == 2 else "invoice",
                object_id=projects[0],
                original_checked=True,
                allocations=[{
                    "destination": "object",
                    "object_id": projects[0],
                    "gross_amount": payload["gross_amount"],
                }],
            )
            revision = self.store.save(document_id, payload, revision)
            self.store.transfer(document_id, revision)
        self.assertEqual(self.sync()["synced"], 2, self.last_sync)

        documents = self.documents()
        parent = next(document for document in documents
                      if document["client_id"] == self.store.card(1)["client_id"])
        supporting = next(document for document in documents
                          if document["client_id"] == self.store.card(2)["client_id"])
        original_version = parent["sync_version"]
        payload = {
            "client_id": parent["client_id"],
            "supplier_name": parent["supplier_name"],
            "invoice_number": parent["invoice_number"],
            "document_date": parent["document_date"],
            "currency_code": parent["currency_code"],
            "gross_amount": parent["gross_amount"],
            "vat_amount": parent["vat_amount"],
            "verification_status": "confirmed",
            "allocation_basis": "gross",
            "expected_version": original_version,
            "allocations": [{
                "object_id": projects[0],
                "gross_amount": "125.00",
                "net_amount": "100.00",
                "vat_amount": "25.00",
                "expense_category": CATEGORY_SUBCONTRACTOR_WORK,
                "expense_category_source": "user",
                "expense_category_evidence": "Selected from invoice line 1",
            }],
            "supporting_document_ids": [supporting["id"]],
        }
        with self.server_client.session_transaction() as session:
            session.update(role="admin", expense_csrf_token="isolated-csrf")
        response = self.server_client.put(
            f"/api/expense-documents/{parent['id']}", json=payload,
            headers={"X-CSRF-Token": "isolated-csrf"},
        )
        self.assertEqual(response.status_code, 200, response.get_json())

        stale = self.server_client.put(
            f"/api/expense-documents/{parent['id']}", json=payload,
            headers={"X-CSRF-Token": "isolated-csrf"},
        )
        self.assertEqual(stale.status_code, 409, stale.get_json())
        with self.server_client.session_transaction() as session:
            session.clear()

        updated = next(document for document in self.documents()
                       if document["id"] == parent["id"])
        legacy_payload = {
            "client_id": updated["client_id"],
            "supplier_name": updated["supplier_name"],
            "invoice_number": updated["invoice_number"],
            "document_date": updated["document_date"],
            "currency_code": updated["currency_code"],
            "gross_amount": updated["gross_amount"],
            "vat_amount": updated["vat_amount"],
            "verification_status": updated["verification_status"],
            "expected_version": updated["sync_version"],
            "allocations": [{
                "object_id": allocation["object_id"],
                "gross_amount": allocation["gross_amount"],
                "net_amount": allocation["net_amount"],
                "vat_amount": allocation["vat_amount"],
            } for allocation in updated["allocations"]],
        }
        with self.server_client.session_transaction() as session:
            session.update(role="admin", expense_csrf_token="isolated-csrf")
        legacy_response = self.server_client.put(
            f"/api/expense-documents/{parent['id']}", json=legacy_payload,
            headers={"X-CSRF-Token": "isolated-csrf"},
        )
        self.assertEqual(legacy_response.status_code, 200, legacy_response.get_json())
        with self.server_client.session_transaction() as session:
            session.clear()

        updated_documents = self.documents()
        updated = next(document for document in updated_documents
                       if document["id"] == parent["id"])
        child = next(document for document in updated_documents
                     if document["id"] == supporting["id"])
        allocation = updated["allocations"][0]
        self.assertEqual(updated["supplier_name"], parent["supplier_name"])
        self.assertEqual(updated["invoice_number"], parent["invoice_number"])
        self.assertEqual(updated["document_type"], parent["document_type"])
        self.assertEqual(updated["verification_status"], parent["verification_status"])
        self.assertEqual(updated["rounding_amount"], parent["rounding_amount"])
        self.assertEqual(updated["amount_due"], parent["amount_due"])
        self.assertEqual(updated["attachments"], parent["attachments"])
        self.assertEqual(allocation["object_id"], projects[0])
        self.assertEqual(allocation["gross_amount"], "125.00")
        self.assertEqual(allocation["net_amount"], "100.00")
        self.assertEqual(allocation["vat_amount"], "25.00")
        self.assertEqual(allocation["expense_category"], CATEGORY_SUBCONTRACTOR_WORK)
        self.assertEqual(allocation["expense_category_source"], "user")
        self.assertEqual(updated["supporting_document_ids"], [supporting["id"]])
        self.assertEqual(child["supporting_to_document_id"], parent["id"])

        summary = self.server_client.get(
            f"/api/expense-objects/{projects[0]}/expenses",
            headers={"X-Sync-Key": "isolated-sync-test-key"},
        ).get_json()
        self.assertEqual(len(summary["documents"]), 1)
        self.assertEqual(summary["summary"]["SEK"]["net"], "100.00")
        self.assertEqual(
            summary["summary"]["SEK"]["categories"][CATEGORY_SUBCONTRACTOR_WORK]["known_net"],
            "100.00",
        )
        included, excluded = collect_object_expenses(updated_documents, projects[0])
        self.assertEqual(len(included), 1)
        self.assertEqual(included[0]["share"]["net_amount"], Decimal("100.00"))
        self.assertEqual(excluded, [])

        connection = self.server.get_db_connection()
        try:
            history = connection.execute(
                "SELECT new_allocations_json FROM expense_allocation_history "
                "WHERE document_id=? ORDER BY id DESC LIMIT 1", (parent["id"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(history)
        self.assertIn(CATEGORY_SUBCONTRACTOR_WORK, history[0])

    def test_company_and_mixed_allocations_sync_once_and_analytics_keep_company_share_general(self):
        projects = self.configure_incoming()
        revision, company_payload = self.store.latest(1)
        company_payload.update(original_checked=True, allocations=[{
            "destination": "company", "object_id": None, "gross_amount": "125.00",
        }])
        revision = self.store.save(1, company_payload, revision)
        self.store.transfer(1, revision)
        self.assertEqual(self.sync()["synced"], 1, self.last_sync)

        revision, mixed_payload = self.store.latest(2)
        mixed_payload.update(
            supplier_name="Second isolated supplier",
            document_date="2026-10-22",
            original_checked=True,
            allocations=[
                {"destination": "company", "object_id": None, "gross_amount": "25.00"},
                {"destination": "object", "object_id": projects[0], "gross_amount": "100.00"},
            ],
        )
        revision = self.store.save(2, mixed_payload, revision)
        self.store.transfer(2, revision)
        self.assertEqual(self.sync()["synced"], 1, self.last_sync)

        documents = self.documents()
        self.assertEqual(len(documents), 2)
        company_document = next(
            document for document in documents
            if document["client_id"] == self.store.card(1)["client_id"])
        mixed_document = next(
            document for document in documents
            if document["client_id"] == self.store.card(2)["client_id"])
        self.assertEqual(
            {(part["destination"], part["object_id"]) for part in company_document["allocations"]},
            {("company", None)},
        )
        self.assertEqual(
            {(part["destination"], part["object_id"]) for part in mixed_document["allocations"]},
            {("company", None), ("object", projects[0])},
        )
        self.assertEqual(company_document["unallocated_gross"], "0.00")
        self.assertEqual(mixed_document["unallocated_gross"], "0.00")

        import analytics
        conn = self.server.get_db_connection()
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            report = analytics.build(conn.cursor(), "2026-10-01", "2026-10-31", documents)
        finally:
            conn.close()
        rows = {row["key"]: row for row in report["rows"]}
        self.assertEqual(report["totals"]["expenses"], 200)
        self.assertEqual(rows["general"]["expenses"], 100)
        self.assertEqual(rows["unresolved_net"]["expenses"], 100)
        object_row = rows[f"object:{projects[0]}"]
        self.assertEqual(object_row["expenses"], 0)
        self.assertTrue(object_row["partial"])
        self.assertIsNone(report["billing"][0]["net"])

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

    def test_web_editor_renders_allocation_categories_and_explicit_evidence_controls(self):
        import json
        import re
        from expense_categories import EXPENSE_CATEGORIES

        with self.server_client.session_transaction() as session:
            session.update(role="admin")
        response = self.server_client.get("/expenses")
        with self.server_client.session_transaction() as session:
            session.clear()

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        category_match = re.search(r"const expenseCategories = (.*?);", html)
        self.assertIsNotNone(category_match)
        self.assertEqual(json.loads(category_match.group(1)), list(EXPENSE_CATEGORIES))
        self.assertIn("Категория относится только к этой части расхода", html)
        self.assertIn("Пояснение, строка или страница", html)
        self.assertIn("supporting-documents", html)
        self.assertIn("Открыть оригинал", html)

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
        reopened = self.documents()[0]
        self.assertEqual(
            [row["gross_amount"] for row in reopened["allocations"]],
            ["700.00", "1100.00", "1500.00"],
        )
        self.assertEqual(
            sum(Decimal(row["gross_amount"]) for row in reopened["allocations"]),
            Decimal(reopened["gross_amount"]),
        )
        for project, amount in zip(projects, ("700.00", "1100.00", "1500.00")):
            summary = self.server_client.get(f"/api/expense-objects/{project}/expenses", headers={
                "X-Sync-Key": "isolated-sync-test-key"}).get_json()
            self.assertEqual(summary["documents"][0]["object_gross"], amount)
        reopened_desktop = self.desktop_snapshot()
        self.assertEqual(
            [row["gross_amount"] for row in reopened_desktop[0]["allocations"]],
            ["700.00", "1100.00", "1500.00"],
        )
        # Match the current gross web form: no per-allocation VAT controls are present.
        edited.update(expected_version=reopened_desktop[0]["sync_version"], vat_amount="600.00")
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

    def test_expense_document_list_paginates_without_losing_total_supplier_list(self):
        conn = self.server.get_db_connection()
        try:
            now = "2026-11-01T00:00:00+00:00"
            conn.executemany(
                "INSERT INTO expense_documents(client_id,supplier_name,created_at,updated_at) "
                "VALUES(?,?,?,?)",
                [
                    (f"page-{index}", f"Page supplier {index}", now, now)
                    for index in range(3)
                ],
            )
            conn.commit()
        finally:
            conn.close()
        headers = {"X-Sync-Key": "isolated-sync-test-key"}
        first = self.server_client.get(
            "/api/expense-documents?limit=2&offset=0", headers=headers,
        )
        self.assertEqual(first.status_code, 200, first.get_json())
        first_page = first.get_json()
        self.assertEqual(first_page["total"], 3)
        self.assertTrue(first_page["has_more"])
        self.assertEqual(len(first_page["documents"]), 2)
        self.assertEqual(first_page["suppliers"], [
            "Page supplier 0", "Page supplier 1", "Page supplier 2",
        ])

        second = self.server_client.get(
            "/api/expense-documents?limit=2&offset=2", headers=headers,
        )
        self.assertEqual(second.status_code, 200, second.get_json())
        second_page = second.get_json()
        self.assertEqual(second_page["total"], 3)
        self.assertFalse(second_page["has_more"])
        self.assertEqual(len(second_page["documents"]), 1)
        self.assertFalse({
            row["id"] for row in first_page["documents"]
        } & {
            row["id"] for row in second_page["documents"]
        })

        invalid = self.server_client.get(
            "/api/expense-documents?limit=1001", headers=headers,
        )
        self.assertEqual(invalid.status_code, 400)
