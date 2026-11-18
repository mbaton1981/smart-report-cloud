import copy
import unittest
import uuid
from contextlib import closing
from unittest.mock import patch

import test_catalog_sync as catalog_tests
from test_allocation_proposal import fixture, OBJECTS
from document_costs import propose_object_allocations
from expense_categories import CATEGORY_MATERIALS, CATEGORY_SUBCONTRACTOR_WORK, CATEGORY_UNCLASSIFIED


class CategoryAutomationTests(unittest.TestCase):
    setUpClass = classmethod(catalog_tests.CatalogSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog_tests.CatalogSyncTests.tearDownClass.__func__)
    setUp = catalog_tests.CatalogSyncTests.setUp
    tearDown = catalog_tests.CatalogSyncTests.tearDown
    _add_company = catalog_tests.CatalogSyncTests._add_company

    def test_semantic_fallback_keeps_main_window_responsive_and_only_suggests(self):
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        self.addCleanup(window.close)
        proposals = {99: {0: dict(category=CATEGORY_UNCLASSIFIED, content="MASKERINGSTEJP UV STANDARD", evidence="", gross_amount="125.00")}}
        def request(method, path, **kwargs):
            if path.endswith("capabilities"):
                return {"features": ["expense_category_ai_v1"]}
            self.assertEqual(kwargs["json"]["groups"], [{"id": "1", "lines": ["MASKERINGSTEJP UV STANDARD"]}])
            return {"suggestions": [dict(id="1", category=CATEGORY_MATERIALS, confidence=.99, evidence_lines=[1])]}
        with patch.object(self.desktop, "request_expense_api", side_effect=request):
            window.incoming_category_suggestions(proposals)
        self.assertEqual(proposals[99][0]["category"], CATEGORY_MATERIALS)
        self.assertEqual(proposals[99][0]["gross_amount"], "125.00")

    def test_global_action_recognizes_two_objects_and_saves_categories_in_one_review(self):
        from expense_documents import migrate_invoice_net_schema, ensure_expense_allocation_category_schema
        headers = {"X-Sync-Key": "isolated-sync-test-key"}
        with closing(self.server.get_db_connection()) as connection:
            migrate_invoice_net_schema(connection)
            ensure_expense_allocation_category_schema(connection)
            connection.execute("INSERT INTO meta_objects(company,name,markning,rate,transport_rate,is_active) VALUES('Renatur','Global A','AUTO1',450,450,1)")
            connection.execute("INSERT INTO meta_objects(company,name,markning,rate,transport_rate,is_active) VALUES('Renatur','Global B','AUTO2',450,450,1)")
            connection.commit()
            objects = [dict(row) for row in connection.execute("SELECT * FROM meta_objects WHERE markning IN ('AUTO1','AUTO2') ORDER BY id")]
        documents = []
        for index, (obj, text) in enumerate(zip((objects[0], objects[1], objects[0]),
                                               ("GIPSSKIVA 13X900X2500", "Byggtjänster 8 tim", "Unknown item"))):
            payload = {"client_id": uuid.uuid4().hex, "supplier_name": "Same supplier", "invoice_number": f"AUTO-{index}",
                       "document_date": "2026-11-01", "currency_code": "SEK", "verification_status": "confirmed",
                       "net_amount": "100.00", "vat_amount": "25.00", "gross_amount": "125.00",
                       "rounding_amount": "0.00", "amount_due": "125.00", "recognized_text": text,
                       "allocations": [{"object_id": obj["id"], "destination": "object", "net_amount": "100.00",
                                        "vat_amount": "25.00", "gross_amount": "125.00", "rounding_amount": "0.00"}]}
            response = self.server_client.post("/api/expense-documents", headers=headers, json=payload)
            self.assertEqual(response.status_code, 201, response.get_json())
            documents.append(response.get_json()["document"])
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        self.addCleanup(window.close)
        window._object_expense_recovery_cache = {}
        window._async_expense_reads = False

        def api(method, path, **kwargs):
            response = self.server_client.open(path, method=method, headers=headers, json=kwargs.get("json"))
            self.assertEqual(response.status_code, 200, response.get_json())
            return response.get_json()

        def inspect(dialog):
            self.qt_app.processEvents()
            table = dialog.findChild(self.desktop.QTableWidget)
            self.assertEqual(table.rowCount(), 3)
            for index, category in enumerate((CATEGORY_MATERIALS, CATEGORY_SUBCONTRACTOR_WORK, CATEGORY_UNCLASSIFIED)):
                row = next(row for row in range(table.rowCount()) if f"AUTO-{index}" in table.item(row, 1).text())
                self.assertEqual(table.cellWidget(row, 4).currentData(), category)
                self.assertEqual(table.item(row, 0).checkState(), self.desktop.Qt.CheckState.Unchecked
                                 if category == CATEGORY_UNCLASSIFIED else self.desktop.Qt.CheckState.Checked)
            save = dialog.findChild(self.desktop.QPushButton, "saveCategoryReview")
            self.assertTrue(save.isEnabled())
            with patch.object(self.desktop.QMessageBox, "question", return_value=self.desktop.QMessageBox.StandardButton.Yes), \
                    patch.object(self.desktop.QMessageBox, "information"):
                save.click()
            self.assertFalse(save.isEnabled())
            dialog.reject()
            return dialog.result()

        with patch.object(self.desktop, "fetch_all_expense_documents", return_value=documents), \
                patch.object(window, "_expense_document_snapshot", return_value=(documents, True)), \
                patch.object(window, "_expense_server_objects", return_value=(objects, True)), \
                patch.object(window, "_invalidate_expense_reads"), patch.object(window, "load_object_expense_overview"), \
                patch.object(self.desktop, "request_expense_api", side_effect=api), \
                patch.object(self.desktop.QDialog, "exec", new=inspect):
            window.open_expense_category_review(all_objects=True, automatic=True)
        for document, category in zip(documents, (CATEGORY_MATERIALS, CATEGORY_SUBCONTRACTOR_WORK, CATEGORY_UNCLASSIFIED)):
            current = api("GET", f"/api/expense-documents/{document['id']}")["document"]
            self.assertEqual(current["allocations"][0]["expense_category"], category)
            for key in ("net_amount", "vat_amount", "gross_amount", "rounding_amount", "object_id", "allocation_id"):
                self.assertEqual(current["allocations"][0].get(key), document["allocations"][0].get(key))

    def test_order_content_stays_with_its_object_without_changing_financial_totals(self):
        fields = fixture()
        for index, order in enumerate(fields["order_proposals"]):
            for item in order["vat_items"]:
                item["description"] = "Byggtjänster 8 tim" if index in (1, 2) else "GIPSSKIVA 13X900"
        before = copy.deepcopy(fields)
        parts = propose_object_allocations(fields, OBJECTS)["parts"]
        self.assertEqual([p["expense_category"] for p in parts],
                         [CATEGORY_MATERIALS, CATEGORY_SUBCONTRACTOR_WORK, CATEGORY_MATERIALS])
        self.assertEqual([p["gross_amount"] for p in parts], ["592.32", "2411.02", "2282.66"])
        self.assertEqual(fields, before)
