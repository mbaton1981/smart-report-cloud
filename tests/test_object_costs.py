import json
import unittest
from unittest.mock import patch

import test_incoming_integration as integration
from offline_review import ReviewError


class ObjectCostTests(unittest.TestCase):
    setUpClass = classmethod(integration.IncomingIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(integration.IncomingIntegrationTests.tearDownClass.__func__)
    _add_company = integration.IncomingIntegrationTests._add_company
    setUp = integration.IncomingIntegrationTests.setUp
    stage = integration.IncomingIntegrationTests.stage
    request = integration.IncomingIntegrationTests.request
    sync = integration.IncomingIntegrationTests.sync
    approve = integration.IncomingIntegrationTests.approve

    def documents(self):
        response = self.server_client.get("/api/expense-documents",
                                         headers={"X-Sync-Key": "isolated-sync-test-key"})
        self.assertEqual(response.status_code, 200)
        return response.get_json()["documents"]

    def test_receipt_without_number_payment_or_new_schema_lost_ack(self):
        conn = self.server.get_db_connection()
        schema = [tuple(row) for row in conn.execute("SELECT sql FROM sqlite_master ORDER BY name")]
        conn.close()
        revision = self.approve(2)
        card = self.store.transfer(2, revision)
        self.mode = "lost_ack"
        self.assertEqual(self.sync()["synced"], 0)
        self.assertEqual(self.sync()["synced"], 1, self.last_sync)
        self.assertEqual(self.sync()["synced"], 0)
        self.store.transfer(2, revision)
        documents = self.documents()
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["client_id"], card["client_id"])
        self.assertEqual(documents[0]["invoice_number"], "")
        self.assertIsNone(documents[0]["amount_due"])
        self.assertEqual(documents[0]["gross_amount"], "125.00")
        self.assertTrue(documents[0]["has_original"])
        conn = self.server.get_db_connection()
        self.assertEqual(schema, [tuple(row) for row in conn.execute("SELECT sql FROM sqlite_master ORDER BY name")])
        conn.close()
        conn = self.desktop.get_db_connection()
        for table in ("supplier_documents", "supplier_document_payments", "balance"):
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        conn.close()

    def test_unknown_vat_and_manually_confirmed_unknown_type(self):
        base = self.store.base(2)
        base["document_type"] = "unclassified"
        base["raw_text"] = ""
        from offline_review import packed
        with self.store.conn:
            self.store.conn.execute("UPDATE documents SET base_json=? WHERE id=2", (packed(base),))
        revision, payload = self.store.latest(2)
        payload.update(confirmed_type="receipt", vat_amount=None, net_amount=None,
                       object_id=self.object_id, original_checked=True)
        revision = self.store.save(2, payload, revision)
        self.store.transfer(2, revision)
        self.assertEqual(self.sync()["synced"], 1, self.last_sync)
        document = self.documents()[0]
        self.assertEqual(document["gross_amount"], "125.00")
        self.assertIsNone(document["vat_amount"])

    def test_reassign_updates_same_card_and_object_totals(self):
        card = self.store.transfer(2, self.approve(2))
        self.assertEqual(self.sync()["synced"], 1, self.last_sync)
        conn = self.server.get_db_connection()
        try:
            other = conn.execute("""INSERT INTO meta_objects(company,name,markning,rate,transport_rate,is_active)
                VALUES ('Renatur','Other isolated object','test-other',0,0,1)""").lastrowid
            conn.commit()
        finally:
            conn.close()
        self.host._expense_objects.append({"id": other, "name": "Other isolated object"})
        self.host.incoming_register_snapshot = self.documents
        with self.assertRaisesRegex(ReviewError, "актуальное распределение"):
            self.store.reassign(2, other, self.store.latest(2)[0])
        revision, current = self.store.current_review(2)
        current["allocations"] = [{"object_id": other, "gross_amount": current["gross_amount"]}]
        self.store.save(2, current, revision)
        self.mode = "lost_ack"
        self.assertEqual(self.sync()["synced"], 0)
        self.assertEqual(self.sync()["synced"], 1, self.last_sync)
        documents = self.documents()
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["client_id"], card["client_id"])
        self.assertEqual(documents[0]["allocations"][0]["object_id"], other)
        self.assertTrue(self.store.accounting_status(2)[1])
        for object_id, expected in ((self.object_id, 0), (other, 1)):
            result = self.server_client.get(f"/api/expense-objects/{object_id}/expenses",
                                            headers={"X-Sync-Key": "isolated-sync-test-key"}).get_json()
            self.assertEqual(len(result["documents"]), expected)

    def test_different_pdf_same_purchase_requires_review(self):
        self.store.transfer(2, self.approve(2))
        self.assertEqual(self.sync()["synced"], 1)
        document = self.documents()[0]
        payload = {**self.fields, "client_id": "other-pdf", "usage": "object_cost",
                   "allocation_basis": "gross", "verification_status": "confirmed",
                   "invoice_number": "", "recognized_text": "Kvitto", "allocations": [
                       {"object_id": self.object_id, "gross_amount": "125.00", "vat_amount": "25.00"}]}
        result = self.server_client.post("/api/expense-documents", json=payload,
                                        headers={"X-Sync-Key": "isolated-sync-test-key"})
        self.assertEqual(result.status_code, 409, result.get_json())
        self.assertEqual(self.documents(), [document])

    def test_no_green_for_failed_or_unacknowledged_delivery(self):
        self.store.transfer(2, self.approve(2))
        self.host.incoming_delivery_pending = lambda cid: {"last_error": "test connection interrupted"}
        self.assertFalse(self.store.accounting_status(2)[1])
        self.host.incoming_delivery_pending = lambda cid: None
        self.assertFalse(self.store.accounting_status(2)[1])

    def test_nordea_remains_blocked_with_old_invoice_label(self):
        with self.assertRaises(ReviewError):
            self.store.transfer(3, self.approve(3))
        self.assertEqual(self.documents(), [])

    def test_integrated_ui_edit_save_choose_object_add_and_green(self):
        from PyQt6.QtWidgets import QMessageBox
        from offline_review_ui import OfflineReviewWindow
        window = OfflineReviewWindow(self.store, integrated=True)
        try:
            window.show()
            self.qt_app.processEvents()
            window.selector.setCurrentIndex(1)
            window.expense_button.click()
            self.assertFalse(window.receipt_paid.isVisible())
            self.assertNotIn("amount_due", window.inputs)
            window.inputs["supplier_name"].setText("Manually reviewed seller")
            window.kind.setCurrentIndex(window.kind.findData("receipt"))
            self.assertIsNone(window.object.currentData())
            self.assertTrue(any("объект" in issue for issue in self.store.issues(2, window.payload())))
            window.object.setCurrentIndex(window.object.findData(self.object_id))
            window.checked.setChecked(True)
            self.assertTrue(window.save_edits())
            self.qt_app.processEvents()
            self.assertTrue(window.gallery.isVisible())
            self.assertFalse(window.gallery.preview._image.pixmap().isNull())
            self.assertEqual(self.store.latest(2)[1]["supplier_name"], "Manually reviewed seller")
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                window.confirm_transfer()
            self.assertIsNotNone(self.store.card(2))
            self.assertEqual(self.sync()["synced"], 1, self.last_sync)
            self.host.incoming_register_snapshot = self.documents
            window.select_document()
            self.assertEqual(window.status.text(), "Учтён на объекте")
            self.assertIn("#b9efc6", window.status.styleSheet())
            self.assertFalse(window.transfer_button.isEnabled())
            self.assertTrue(window.reassign_button.isEnabled())
        finally:
            window.close()
