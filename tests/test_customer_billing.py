import copy
import json
import unittest
from contextlib import closing
from datetime import date
from decimal import Decimal
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

    def test_accountant_handoff_accepts_unknown_ob_price_and_net_without_repricing(self):
        with closing(self.desktop.get_db_connection()) as local_connection:
            segment_schema = local_connection.execute("SELECT sql FROM sqlite_master WHERE name='ob_segments'").fetchone()[0]
        with closing(self.server.get_db_connection()) as connection:
            connection.execute(segment_schema)
            quote = json.loads(self.shift["customer_pricing"])
            quote["travel"] = None
            connection.execute("UPDATE incoming_submissions SET submission_type='ob',customer_pricing=? WHERE sync_id='billing-shift'",
                               (json.dumps(quote),))
            connection.execute("UPDATE expense_allocations SET allocated_net=NULL,allocated_vat=NULL WHERE document_id=?", (self.document["id"],))
            connection.commit()
            shift = dict(connection.execute("SELECT * FROM incoming_submissions WHERE sync_id='billing-shift'").fetchone())
        shift["entry_type"] = "ob"
        document = self.server_client.get(f"/api/expense-documents/{self.document['id']}", headers=self.headers).get_json()["document"]
        payload = {**self.payload, "purpose": "accountant", "members": [shift_member(shift), expense_member(document, 0)]}
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.post(payload).status_code, 200)
        with closing(self.server.get_db_connection()) as connection:
            actual = dict(connection.execute("SELECT * FROM incoming_submissions WHERE sync_id='billing-shift'").fetchone())
            self.assertEqual(actual["customer_pricing"], shift["customer_pricing"])
            self.assertEqual(actual["invoiced"], 1)
            self.assertEqual(actual["hours"], shift["hours"])
            self.assertEqual(actual["transport"], shift["transport"])
        duplicate = {**payload, "operation_id": "duplicate-accountant", "invoice_number": "OTHER"}
        self.assertEqual(self.post(duplicate).status_code, 409)

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

    def test_invoice_expense_correction_replaces_only_membership_and_rejects_stale_edits(self):
        from api_auth import issue_token

        path = "/api/admin/analytics/billing-correction"
        payload = {**self.payload, "purpose": "accountant"}
        self.assertEqual(self.post(payload).status_code, 200)
        self.assertEqual(self.server_client.get(path).status_code, 403)
        with closing(self.server.get_db_connection()) as connection:
            new_doc = connection.execute("""
                INSERT INTO expense_documents (client_id, supplier_name, invoice_number, document_date,
                    net_amount, vat_amount, gross_amount, verification_status, created_at, updated_at)
                VALUES ('correct-expense','Alternate supplier','NEW-42','2026-11-12',
                    '200','50','250','confirmed','2026-11-12','2026-11-12')
            """).lastrowid
            connection.execute("""
                INSERT INTO expense_allocations (document_id,object_id,allocated_gross,allocated_net,allocated_vat,destination)
                VALUES (?,?,'250','200','50','object')
            """, (new_doc, self.object_id))
            shift_before = tuple(connection.execute("SELECT * FROM incoming_submissions WHERE sync_id='billing-shift'").fetchone())
            connection.commit()
            admin_id = connection.execute("SELECT id FROM users WHERE role='admin' AND is_active=1").fetchone()[0]
            token, _ = issue_token(connection, admin_id)
        params = {"object_id": self.object_id, "invoice_number": payload["invoice_number"]}
        auth = {"Authorization": f"Bearer {token}"}

        def correct(data):
            return self.server_client.post(path, json=data, headers=auth)

        model = self.server_client.get(path, query_string=params, headers=auth).get_json()["correction"]
        self.assertEqual(model["revision"], 0)
        before, available = model["expenses"]
        self.assertTrue(before["included"])
        self.assertFalse(available["included"])
        self.assertEqual(available["snapshot"]["document_id"], new_doc)
        correction = {**params, "correction_id": "swap", "revision": 0,
                      "remove": [before["snapshot"]["key"]], "add": [available["snapshot"]]}
        stale_doc = {**available["snapshot"], "version": available["snapshot"]["version"] + 1}
        self.assertEqual(correct({**correction, "add": [stale_doc]}).status_code, 409)
        self.assertEqual(correct({**correction, "remove": [self.payload["members"][0]["key"]]}).status_code, 409)
        first = correct(correction)
        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(correct(correction).get_json(), first.get_json())
        self.assertEqual(correct({**correction, "correction_id": "stale"}).status_code, 409)
        refreshed = self.server_client.get(path, query_string=params, headers=auth).get_json()["correction"]
        self.assertEqual(refreshed["revision"], 1)
        self.assertEqual([(item["snapshot"]["document_id"], item["included"]) for item in refreshed["expenses"]],
                         [(new_doc, True), (self.document["id"], False)])
        state = self.server_client.get("/api/customer-invoices", headers=self.headers, query_string=params).get_json()
        self.assertEqual(set(state["members"]), {self.payload["members"][0]["key"], available["snapshot"]["key"]})
        entries, _ = prepare_next_invoice([], [self.document], self.object_id, "2026-11-30", state["members"])
        self.assertEqual([entry["member"]["key"] for entry in entries], [before["snapshot"]["key"]])
        with closing(self.server.get_db_connection()) as connection:
            self.assertEqual(tuple(connection.execute("SELECT * FROM incoming_submissions WHERE sync_id='billing-shift'").fetchone()),
                             shift_before)
            log = connection.execute("SELECT actor,removed_json,added_json FROM customer_invoice_corrections").fetchall()
            self.assertEqual(len(log), 1)
            self.assertEqual(log[0]["actor"], str(admin_id))
            self.assertEqual(json.loads(log[0]["removed_json"]), [before["snapshot"]])
            self.assertEqual(json.loads(log[0]["added_json"]), [available["snapshot"]])

    def test_desktop_preview_exports_only_selected_rows_and_records_invoice(self):
        from types import SimpleNamespace
        from next_customer_invoice import open_next_invoice
        from object_package_report import expense_document_key
        from pypdf import PdfReader
        from reportlab.pdfgen.canvas import Canvas
        import swedish_translation

        swedish_translation.clear_cache()
        self.addCleanup(swedish_translation.clear_cache)
        self.shift["comment"] = "Демонтаж стін у підвалі"

        with closing(self.desktop.get_db_connection()) as connection:
            local = dict(connection.execute("SELECT * FROM objects WHERE is_active=1 LIMIT 1").fetchone())
            connection.execute("UPDATE objects SET server_id=? WHERE id=?", (self.object_id, local["id"]))
            connection.commit()
            swedish_translation.save_confirmed_translations(
                connection, [self.shift], {self.shift["sync_id"]: "Demontering av väggar i källaren"},
            )
        incomplete_shift = copy.deepcopy(self.shift)
        incomplete_shift.update(sync_id="incomplete-shift", id=self.shift["id"] + 1000, date="2026-11-02",
                                entry_type="ob", company="Test builder", comment="Демонтаж стен и сортировка отходов")
        quote = json.loads(incomplete_shift["customer_pricing"])
        quote.update(travel=None, date_from="2026-11-02", date_to="2026-11-02", company=incomplete_shift["company"])
        incomplete_shift["customer_pricing"] = json.dumps(quote)
        report = {"journal_rows": [self.shift, incomplete_shift],
                  "journal_segments": {"incomplete-shift": [{"category": "weekday_early_evening", "hours": 8,
                                                              "start_time": "16:00", "end_time": "00:00"}]}, "company": local["company"],
                  "object": local["name"], "object_id": local["id"], "date_from": "1900-01-01", "date_to": "2026-11-18"}
        window = self.desktop.QMainWindow()
        self.addCleanup(window.close)
        window.inv_obj_cb = SimpleNamespace(currentData=lambda: local["id"])
        window.inv_date_to = self.desktop.create_date_field("2026-10-31")
        window.collect_invoice_report = lambda **kwargs: report
        window._collect_journal_translations = self.desktop.SmartReportApp._collect_journal_translations.__get__(window)
        output = Path(self._case.name) / "next-customer-invoice.pdf"
        original = Path(self._case.name) / "vendor-original.pdf"
        canvas = Canvas(str(original))
        canvas.drawString(60, 750, "TEST SUPPLIER ORIGINAL")
        canvas.save()
        window._download_object_report_originals = lambda documents, *_: (
            {expense_document_key(document): [original] for document in documents
             if document["id"] == self.document["id"]}, {},
        )
        self.document["allocations"][0].update(expense_category="Материалы и закупки", expense_category_source="user")
        unknown = copy.deepcopy(self.document)
        unknown["id"] += 1000
        unknown["client_id"] = "test-unknown-supplier-copy"
        unknown["document_date"] = "2026-11-17"
        unknown["allocations"][0]["net_amount"] = None
        rental = copy.deepcopy(self.document)
        rental.update(id=rental["id"] + 1001, client_id="test-rental", document_date="2026-10-20",
                      invoice_number="RENTAL-1")
        rental["allocations"][0]["expense_category"] = "Аренда оборудования"

        def api(method, endpoint, **kwargs):
            response = self.server_client.open(endpoint, method=method, headers=self.headers,
                                               query_string=kwargs.get("params"), json=kwargs.get("json"))
            self.assertEqual(response.status_code, 200, response.get_json())
            return response.get_json()

        def translate(items, _database):
            self.assertEqual([text for _, text in items], [incomplete_shift["comment"]])
            return {identifier: "Rivning av väggar och sortering av avfall" for identifier, _ in items}

        reviewed_translations = []

        def inspect(dialog):
            from PyQt6.QtWidgets import QAbstractItemView, QCheckBox, QLineEdit, QPushButton, QTableWidget, QTextEdit
            if isinstance(dialog, self.desktop.JournalTranslationDialog):
                self.assertEqual(dialog.table.rowCount(), 1)
                self.assertIn("Документы бухгалтеру", dialog.parentWidget().windowTitle())
                editor = dialog.findChild(QTextEdit, f"journal_translation_{incomplete_shift['id']}")
                reviewed_translations.append(editor.toPlainText())
                if len(reviewed_translations) == 1:
                    dialog.reject()
                else:
                    editor.setPlainText("Demontering av väggar och sortering av rivningsavfall")
                    dialog.accept()
                return dialog.result()
            table = dialog.findChild(QTableWidget)
            self.assertIsNotNone(table, str([(type(widget).__name__, widget.objectName()) for widget in dialog.children()]))
            self.assertEqual(table.rowCount(), 8)
            self.assertEqual(table.verticalScrollMode(), QAbstractItemView.ScrollMode.ScrollPerPixel)
            self.assertEqual(table.horizontalHeaderItem(2).text(), "Категория")
            headings = [(i, table.item(i, 0).text()) for i in range(table.rowCount()) if table.columnSpan(i, 0) > 1]
            self.assertEqual(headings, [
                (0, "Материалы и закупки · 2"),
                (3, "Аренда оборудования · 1"),
                (5, "Наши часы и поездки · 2"),
            ])
            self.assertEqual([table.item(i, 1).text() for i in (1, 2, 4, 6, 7)],
                             ["2026-10-21", "2026-11-17", "2026-10-20", "2026-11-01", "2026-11-02"])
            self.assertFalse(table.item(0, 0).flags() & self.desktop.Qt.ItemFlag.ItemIsUserCheckable)
            jump = dialog.findChild(QPushButton, "accountantInvoiceGroupNavigation")
            sections = jump.menu().actions()
            self.assertEqual([action.text() for action in sections],
                             ["Материалы и закупки", "Аренда оборудования", "Наши часы и поездки"])
            with patch.object(table, "scrollToItem") as scroll:
                sections[1].trigger()
                scroll.assert_called_once_with(table.item(3, 0), QAbstractItemView.ScrollHint.PositionAtTop)
            self.assertEqual(table.item(1, 2).text(), "Материалы и закупки")
            self.assertEqual(table.item(6, 2).text(), "Наши часы и поездки")
            self.assertEqual(table.item(7, 4).text(), "—")
            self.assertNotIn("SEK", table.item(7, 3).text())
            self.assertIn("OB 2", table.item(7, 3).text())
            self.assertIn("Поездки · 1", table.item(7, 3).text())
            self.assertEqual(table.item(7, 5).text(), "К передаче бухгалтеру")
            self.assertEqual(table.item(7, 0).checkState(), self.desktop.Qt.CheckState.Checked)
            self.assertIn("125.00 SEK с НДС", table.item(2, 4).text())
            self.assertEqual(table.item(2, 0).checkState(), self.desktop.Qt.CheckState.Checked)
            buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
            self.assertFalse(buttons["Сохранить как выставленное"].isEnabled())
            self.assertTrue(buttons["За весь проект"].isChecked())
            rows = (1, 2, 4, 6, 7)
            self.assertEqual([row for row in rows if not table.isRowHidden(row)], list(rows))
            reviewed = dialog.findChild(QCheckBox, "accountantInvoiceReviewed")
            reviewed.setChecked(True)
            buttons["Прошлый месяц"].click()
            self.assertFalse(reviewed.isChecked())
            self.assertEqual([row for row in rows if not table.isRowHidden(row)], [1, 4])
            table.item(1, 0).setCheckState(self.desktop.Qt.CheckState.Unchecked)
            buttons["По дату отчёта"].click()
            self.assertEqual([row for row in rows if not table.isRowHidden(row)], [1, 4])
            buttons["Этот месяц"].click()
            self.assertEqual([row for row in rows if not table.isRowHidden(row)], [2, 6, 7])
            self.assertTrue(table.isRowHidden(3))
            self.assertFalse(sections[1].isVisible())
            self.assertEqual(table.item(0, 0).text(), "Материалы и закупки · 1")
            buttons["За весь проект"].click()
            self.assertEqual(table.item(1, 0).checkState(), self.desktop.Qt.CheckState.Unchecked)
            table.item(1, 0).setCheckState(self.desktop.Qt.CheckState.Checked)
            checks = {field.text(): field for field in dialog.findChildren(QCheckBox)}
            checks["Включить поездки"].setChecked(False)
            table.item(1, 6).setCheckState(self.desktop.Qt.CheckState.Checked)
            table.item(2, 6).setCheckState(self.desktop.Qt.CheckState.Unchecked)
            table.item(4, 0).setCheckState(self.desktop.Qt.CheckState.Unchecked)
            table.item(4, 6).setCheckState(self.desktop.Qt.CheckState.Checked)
            with patch("next_customer_invoice.edit_accountant_rates",
                       return_value=({"ob2": Decimal(720), "trip": Decimal(300)}, False)):
                buttons["Тарифы пакета…"].click()
            def preview(source, suggested, parent, **kwargs):
                self.assertTrue(Path(source).is_file())
                self.assertEqual(kwargs['title'], 'Предпросмотр документа бухгалтеру')
                output.write_bytes(Path(source).read_bytes())
            with patch('payroll_preview.open_preview', side_effect=preview) as viewer, \
                    patch.object(self.desktop.QFileDialog, "getSaveFileName", side_effect=AssertionError('Preview must not ask to save')), \
                    patch.object(self.desktop.QMessageBox, "information"), \
                    patch.object(self.desktop.QMessageBox, "warning", side_effect=lambda *args: self.fail(args[2])):
                buttons["Предварительный PDF"].click()
                viewer.assert_not_called()
                buttons["Предварительный PDF"].click()
                viewer.assert_called_once()
            text = " ".join(page.extract_text() for page in PdfReader(output).pages)
            self.assertIn("Till och med 2026-11-24", text)
            self.assertIn("100,00 SEK", text)
            self.assertIn("125,00 SEK", text)
            self.assertIn("4 500,00 SEK", text)
            self.assertIn("2 880,00 SEK", text)
            self.assertIn("OB 2", text)
            self.assertIn("Dagbok", text)
            self.assertIn("Demontering av väggar i källaren", text)
            self.assertIn("Demontering av väggar och sortering av rivningsavfall", text)
            self.assertNotIn("Демонтаж", text)
            self.assertNotIn("RENTAL-1", text)
            self.assertIn("Totalt exkl. moms: att kontrollera", text)
            self.assertNotIn("Resor", text)
            self.assertEqual(text.count("TEST SUPPLIER ORIGINAL"), 1)
            checks["Включить поездки"].setChecked(True)
            checks["Включить Dagbok"].setChecked(False)
            table.item(1, 6).setCheckState(self.desktop.Qt.CheckState.Unchecked)
            with patch('payroll_preview.open_preview', side_effect=preview):
                buttons["Предварительный PDF"].click()
            without_attachments = " ".join(page.extract_text() for page in PdfReader(output).pages)
            self.assertNotIn("Dagbok", without_attachments)
            self.assertNotIn("TEST SUPPLIER ORIGINAL", without_attachments)
            self.assertIn("Resor: 2", without_attachments)
            self.assertIn("600,00 SEK", without_attachments)
            buttons["Этот месяц"].click()
            with patch('payroll_preview.open_preview', side_effect=preview):
                buttons["Предварительный PDF"].click()
            month_pdf = " ".join(page.extract_text() for page in PdfReader(output).pages)
            self.assertIn("2026-11-01 - 2026-11-30", month_pdf)
            self.assertNotIn("2026-10-21", month_pdf)
            buttons["За весь проект"].click()
            for field in dialog.findChildren(QLineEdit):
                if "Номер фактуры" in field.placeholderText():
                    field.setText("UI-INVOICE-1")
            dialog.findChild(QCheckBox, "accountantInvoiceReviewed").setChecked(True)
            self.assertTrue(buttons["Сохранить как выставленное"].isEnabled())
            table.item(7, 0).setCheckState(self.desktop.Qt.CheckState.Unchecked)
            table.item(2, 0).setCheckState(self.desktop.Qt.CheckState.Unchecked)
            self.assertTrue(buttons["Сохранить как выставленное"].isEnabled())
            with patch.object(self.desktop.QMessageBox, "question", return_value=self.desktop.QMessageBox.StandardButton.Yes), \
                    patch.object(self.desktop.QMessageBox, "information"):
                buttons["Сохранить как выставленное"].click()
            return dialog.result()

        with patch.object(self.desktop, "request_expense_api", side_effect=api), \
                patch.object(self.desktop, "stockholm_today", return_value=date(2026, 11, 24)), \
                patch.object(self.desktop, "fetch_all_expense_documents", return_value=[self.document, unknown, rental]), \
                patch.object(swedish_translation, "_request", side_effect=translate) as translator, \
                patch.object(self.desktop.QDialog, "exec", new=inspect):
            open_next_invoice(window)
        translator.assert_called_once()
        self.assertEqual(reviewed_translations, ["Rivning av väggar och sortering av avfall"] * 2)
        with closing(self.desktop.get_db_connection()) as connection:
            self.assertEqual(swedish_translation.load_confirmed_translations(connection, [incomplete_shift]),
                             {incomplete_shift["sync_id"]: "Demontering av väggar och sortering av rivningsavfall"})
        state = api("GET", "/api/customer-invoices", params={"object_id": self.object_id})
        self.assertEqual(len(state["members"]), 2)
