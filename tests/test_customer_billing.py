import copy
import json
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_incoming_integration as integration
from customer_billing import allocation_fingerprint, expense_member, prepare_next_invoice, shift_member


class CustomerBillingTests(unittest.TestCase):
    setUpClass = classmethod(integration.IncomingIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(integration.IncomingIntegrationTests.tearDownClass.__func__)
    _add_company = integration.IncomingIntegrationTests._add_company
    stage = integration.IncomingIntegrationTests.stage
    approve = integration.IncomingIntegrationTests.approve
    request = integration.IncomingIntegrationTests.request
    sync = integration.IncomingIntegrationTests.sync

    def setUp(self):
        integration.IncomingIntegrationTests.setUp(self)
        self.approve()
        self.store.transfer(1, self.store.latest(1)[0])
        self.assertEqual(self.sync()["synced"], 1)
        self.headers = {"X-Sync-Key": "isolated-sync-test-key"}
        self.document = self.server_client.get("/api/expense-documents", headers=self.headers).get_json()["documents"][0]
        with closing(self.server.get_db_connection()) as connection:
            obj = connection.execute("SELECT * FROM meta_objects WHERE id=?", (self.object_id,)).fetchone()
            quote = {"version": 1, "company": obj["company"], "object_name": obj["name"],
                     "date_from": "2026-11-01", "date_to": "2026-11-01", "source": "Тестовые условия",
                     "base": 450, "travel": 450, "ob": {}}
            connection.execute("""INSERT INTO incoming_submissions
                (request_id,sync_id,submission_type,date,employee,company,object_name,hours,rate,
                 transport,created_at,updated_at,customer_pricing)
                VALUES ('billing-shift','billing-shift','work','2026-11-01','Test employee',?,?,8,450,1,?,?,?)""",
                               (obj["company"], obj["name"], "2026-11-01", "2026-11-01", json.dumps(quote)))
            connection.commit()
            self.shift = dict(connection.execute("SELECT * FROM incoming_submissions WHERE sync_id='billing-shift'").fetchone())
        self.shift["entry_type"] = "work"
        self.payload = {"object_id": self.object_id, "operation_id": "billing-operation",
                        "invoice_number": "CUSTOMER-1", "invoice_date": "2026-11-18",
                        "members": [shift_member(self.shift), expense_member(self.document, 0)]}

    def post(self, payload=None):
        return self.server_client.post("/api/customer-invoices", headers=self.headers,
                                       json=payload or self.payload)

    def test_customer_invoice_requires_the_current_dataset(self):
        with closing(self.server.get_db_connection()) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS sync_dataset (id INTEGER PRIMARY KEY,dataset_id TEXT NOT NULL)")
            connection.execute("INSERT OR REPLACE INTO sync_dataset VALUES (1,'isolated-billing-dataset')")
            connection.commit()
        self.assertEqual(self.post().status_code, 409)
        denied = self.server_client.get("/api/customer-invoices", headers=self.headers,
                                        query_string={"object_id": self.object_id})
        self.assertEqual(denied.status_code, 409)
        self.headers["X-SmartReport-Dataset"] = "isolated-billing-dataset"
        self.assertEqual(self.post().status_code, 200)

    def test_customer_invoice_is_atomic_and_retry_does_not_duplicate_members(self):
        first = self.post()
        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(self.post().status_code, 200)
        state = first.get_json()
        self.assertEqual(len(state["batches"]), 1)
        self.assertEqual(len(state["members"]), 2)
        with closing(self.server.get_db_connection()) as connection:
            row = connection.execute("SELECT invoiced,version FROM incoming_submissions WHERE sync_id='billing-shift'").fetchone()
            self.assertEqual(tuple(row), (1, 2))
        duplicate = {**self.payload, "operation_id": "another-computer", "invoice_number": "CUSTOMER-2"}
        self.assertEqual(self.post(duplicate).status_code, 409)
        self.assertEqual(self.post({**self.payload, "invoice_number": "changed-retry"}).status_code, 409)

    def test_stale_expense_rolls_back_the_entire_invoice_and_shift_status(self):
        stale = copy.deepcopy(self.payload)
        stale["members"][1]["version"] += 1
        self.assertEqual(self.post(stale).status_code, 409)
        with closing(self.server.get_db_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM customer_invoice_batches").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT invoiced FROM incoming_submissions WHERE sync_id='billing-shift'").fetchone()[0], 0)

    def test_mixed_currencies_are_rejected_without_changing_any_status(self):
        with closing(self.server.get_db_connection()) as connection:
            connection.execute("UPDATE expense_documents SET currency_code='USD' WHERE id=?", (self.document['id'],))
            connection.commit()
        document = self.server_client.get(f"/api/expense-documents/{self.document['id']}", headers=self.headers).get_json()['document']
        payload = {**self.payload, "members": [shift_member(self.shift), expense_member(document, 0)]}
        response = self.post(payload)
        self.assertEqual(response.status_code, 409)
        self.assertIn("одной валюте", response.get_json()['error'])
        with closing(self.server.get_db_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM customer_invoice_members").fetchone()[0], 0)

    def test_late_invoice_is_in_next_selection_and_billed_members_are_excluded(self):
        entries, warnings = prepare_next_invoice([self.shift], [self.document], self.object_id, "2026-11-18", {})
        self.assertEqual(len(entries), 2)
        self.assertEqual(warnings, [])
        state = self.post().get_json()
        entries, warnings = prepare_next_invoice([self.shift], [self.document], self.object_id, "2026-11-30", state["members"])
        self.assertEqual(entries, [])
        changed = copy.deepcopy(self.document)
        changed["allocations"][0]["net_amount"] = "101.00"
        entries, warnings = prepare_next_invoice([], [changed], self.object_id, "2026-11-30", state["members"])
        self.assertEqual(entries, [])
        self.assertTrue(warnings)
        unchanged = copy.deepcopy(self.document)
        unchanged["allocations"][0]["expense_category"] = "Материалы и закупки"
        self.assertEqual(allocation_fingerprint(unchanged, 0), allocation_fingerprint(self.document, 0))

    def test_desktop_preview_exports_only_selected_rows_and_records_invoice(self):
        from types import SimpleNamespace
        from next_customer_invoice import open_next_invoice
        from pypdf import PdfReader

        with closing(self.desktop.get_db_connection()) as connection:
            local = dict(connection.execute("SELECT * FROM objects WHERE is_active=1 LIMIT 1").fetchone())
            connection.execute("UPDATE objects SET server_id=? WHERE id=?", (self.object_id, local["id"]))
            connection.commit()
        report = {"journal_rows": [self.shift], "journal_segments": {}, "company": local["company"],
                  "object": local["name"], "object_id": local["id"], "date_from": "1900-01-01", "date_to": "2026-11-18"}
        window = self.desktop.QMainWindow()
        self.addCleanup(window.close)
        window.inv_obj_cb = SimpleNamespace(currentData=lambda: local["id"])
        window.inv_date_to = self.desktop.create_date_field("2026-11-18")
        window.collect_invoice_report = lambda **kwargs: report
        output = Path(self._case.name) / "next-customer-invoice.pdf"

        def api(method, endpoint, **kwargs):
            response = self.server_client.open(endpoint, method=method, headers=self.headers,
                                               query_string=kwargs.get("params"), json=kwargs.get("json"))
            self.assertEqual(response.status_code, 200, response.get_json())
            return response.get_json()

        def inspect(dialog):
            from PyQt6.QtWidgets import QTableWidget, QPushButton, QLineEdit, QCheckBox
            table = dialog.findChild(QTableWidget)
            self.assertIsNotNone(table, str([(type(widget).__name__, widget.objectName()) for widget in dialog.children()]))
            self.assertEqual(table.rowCount(), 2)
            buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
            self.assertFalse(buttons["Сохранить как выставленное"].isEnabled())
            with patch.object(self.desktop.QFileDialog, "getSaveFileName", return_value=(str(output), "PDF")), \
                    patch.object(self.desktop.QMessageBox, "information"), \
                    patch.object(self.desktop.QMessageBox, "warning", side_effect=lambda *args: self.fail(args[2])):
                buttons["Предварительный PDF"].click()
            text = " ".join(page.extract_text() for page in PdfReader(output).pages)
            self.assertIn("4 150,00 SEK", text)
            for field in dialog.findChildren(QLineEdit):
                if "Номер фактуры" in field.placeholderText():
                    field.setText("UI-INVOICE-1")
            dialog.findChild(QCheckBox).setChecked(True)
            self.assertTrue(buttons["Сохранить как выставленное"].isEnabled())
            with patch.object(self.desktop.QMessageBox, "question", return_value=self.desktop.QMessageBox.StandardButton.Yes), \
                    patch.object(self.desktop.QMessageBox, "information"):
                buttons["Сохранить как выставленное"].click()
            return dialog.result()

        with patch.object(self.desktop, "request_expense_api", side_effect=api), \
                patch.object(self.desktop, "fetch_all_expense_documents", return_value=[self.document]), \
                patch.object(self.desktop.QDialog, "exec", new=inspect):
            open_next_invoice(window)
        state = api("GET", "/api/customer-invoices", params={"object_id": self.object_id})
        self.assertEqual(len(state["members"]), 2)
