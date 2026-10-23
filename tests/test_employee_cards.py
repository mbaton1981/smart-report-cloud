from contextlib import closing
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import unittest
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
from pypdf import PdfReader

import employee_cards as cards
import test_catalog_sync as catalog


class EmployeeCardTests(unittest.TestCase):
    setUpClass = classmethod(catalog.CatalogSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog.CatalogSyncTests.tearDownClass.__func__)
    _add_company = catalog.CatalogSyncTests._add_company

    def setUp(self):
        catalog.CatalogSyncTests.setUp(self)
        self.addCleanup(self._case.cleanup)
        self.key_patch = patch.dict(os.environ, {"SMARTREPORT_EMPLOYEE_CARD_KEY": Fernet.generate_key().decode()})
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        from api_auth import issue_token
        from werkzeug.security import generate_password_hash
        conn = self.server.get_db_connection()
        self.addCleanup(conn.close)
        self.server_id = conn.execute("INSERT INTO meta_employees(name,salary_rate) VALUES('Synthetic Employee',245.8)").lastrowid
        self.admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
        accountant_id = conn.execute("""INSERT INTO users(username,password_hash,role,is_active,created_at)
            VALUES('synthetic-accountant',?,'accountant',1,datetime('now'))""", (generate_password_hash("synthetic-password-only"),)).lastrowid
        ordinary_id = conn.execute("""INSERT INTO users(username,role,is_active,employee_id,created_at)
            VALUES('synthetic-worker','user',1,?,datetime('now'))""", (self.server_id,)).lastrowid
        conn.commit()
        self.tokens = {role: issue_token(conn, user_id)[0] for role, user_id in (
            ("admin", self.admin_id), ("accountant", accountant_id), ("user", ordinary_id))}
        conn.close()
        conn = self.desktop.get_db_connection()
        self.addCleanup(conn.close)
        self.local_id = conn.execute("INSERT INTO employees(name,salary_rate) VALUES('Synthetic Employee',245.8)").lastrowid
        conn.execute("INSERT INTO balance(employee,change_amount,description,date) VALUES('Synthetic Employee',100,'test','2026-10-01')")
        conn.commit()
        conn.close()
        self.path = f"/api/employee-cards/{self.server_id}"

    def headers(self, role="admin"):
        return {"Authorization": "Bearer " + self.tokens[role]}

    def get(self, role="admin"):
        response = self.server_client.get(self.path, headers=self.headers(role))
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["employee"]

    def payload(self, record, **changes):
        return dict(card=dict(record["card"], **changes), revision=record["revision"],
                    employee_revision=record["employee_revision"], is_active=record["is_active"],
                    operation_id=str(uuid.uuid4()))

    def put(self, payload, role="admin"):
        return self.server_client.put(self.path, json=payload, headers=self.headers(role))

    def synthetic(self):
        return cards.empty_card() | dict(
            start_date="2026-10-01", identifier_type="samordningsnummer", identifier="0000000000",
            address="Synthetic street 0", postal_code="00100", city="Test city", country="Test country",
            phone="+000 000000", email="synthetic@example.invalid", bank_name="Synthetic bank",
            account_holder="Synthetic Employee", clearing="0001", account_number="0000123456")

    def test_all_fields_roundtrip_encryption_leading_zeroes_and_audit(self):
        record = self.get()
        payload = self.payload(record, **self.synthetic())
        payload["card"]["verified_date"] = "2026-10-01"
        response = self.put(payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        result = response.get_json()["employee"]
        self.assertEqual(result["card"]["clearing"], "0001")
        self.assertEqual(result["card"]["account_number"], "0000123456")
        self.assertEqual(result["card"]["postal_code"], "00100")
        self.assertEqual(result["card"]["identifier"], "0000000000")
        self.assertEqual(result["card"]["verified_date"], "")
        self.assertTrue(result["bank_confirmation_reset"])
        confirmation = self.put(self.payload(result, verified_date="2026-10-02")).get_json()["employee"]
        self.assertEqual(confirmation["card"]["verified_date"], "2026-10-02")
        self.assertEqual(self.get()["card"], confirmation["card"])
        with closing(self.server.get_db_connection()) as conn:
            dump = "\n".join(conn.iterdump())
            for value in ("0000123456", "0000000000", "Synthetic street", "synthetic@example.invalid"):
                self.assertNotIn(value, dump)
            self.assertEqual(conn.execute("SELECT id,salary_rate FROM meta_employees WHERE id=?", (self.server_id,)).fetchone()[1], 245.8)
            audit = conn.execute("SELECT * FROM employee_card_audit ORDER BY id").fetchall()
            self.assertEqual(audit[0]["actor_id"], self.admin_id)
            self.assertIn("account_number", json.loads(audit[0]["fields_json"]))
            self.assertEqual(json.loads(audit[1]["fields_json"]), ["verified_date"])
        audit = self.server_client.get(self.path + "/audit", headers=self.headers()).get_json()
        self.assertIn("Номер счёта", audit["changes"][1]["fields"])
        self.assertNotIn("0000123456", json.dumps(audit))

    def test_bank_change_resets_confirmation_contacts_do_not(self):
        record = self.put(self.payload(self.get(), **self.synthetic())).get_json()["employee"]
        record = self.put(self.payload(record, verified_date="2026-10-02")).get_json()["employee"]
        record = self.put(self.payload(record, phone="+000 111111")).get_json()["employee"]
        self.assertEqual(record["card"]["verified_date"], "2026-10-02")
        record = self.put(self.payload(record, account_number="0000999999", verified_date="2026-10-03")).get_json()["employee"]
        self.assertEqual(record["card"]["verified_date"], "")

    def test_partial_foreign_no_identifier_and_format_warnings(self):
        card = self.synthetic()
        card.update(account_type="foreign", identifier="", identifier_type="", clearing="", account_number="",
                    iban="ZZ00TEST000000000000000", bic="TESTZZ00", email="not-an-email")
        response = self.put(self.payload(self.get(), **card))
        self.assertEqual(response.status_code, 200)
        result = response.get_json()["employee"]
        self.assertNotIn("Clearingnummer", result["missing"])
        self.assertNotIn("Номер счёта", result["missing"])
        self.assertTrue(result["warnings"])
        self.assertEqual(result["card"]["identifier"], "")
        for key in ("clearing", "account_number", "postal_code", "identifier"):
            with self.subTest(key=key):
                invalid = self.payload(result)
                invalid["card"][key] = 123
                self.assertEqual(self.put(invalid).status_code, 400)

    def test_access_enforced_get_put_list_audit_sync_key_not_enough(self):
        self.assertEqual(self.server_client.get(self.path, base_url="http://external.example.invalid",
                                               headers=self.headers()).status_code, 403)
        self.assertEqual(self.server_client.get(self.path, base_url="https://external.example.invalid",
                                               headers=self.headers()).status_code, 200)
        for headers in ({}, {"X-Sync-Key": "isolated-sync-test-key"}, self.headers("user")):
            for path in (self.path, self.path + "/audit", "/api/employee-cards"):
                response = self.server_client.get(path, headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("card_cipher", response.get_data(as_text=True))
            self.assertEqual(self.server_client.put(self.path, json=self.payload(self.get()), headers=headers).status_code, 403)
        for role in ("admin", "accountant"):
            response = self.server_client.get(self.path, headers=self.headers(role))
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response.headers["Cache-Control"])
        for route in ("/api/admin/backup/database", "/api/sync/employee-catalog", "/sync/payroll"):
            self.assertIn(self.server_client.get(route, headers=self.headers("accountant")).status_code, (401, 403))
        with closing(self.server.get_db_connection()) as conn:
            conn.execute("UPDATE users SET is_active=0 WHERE role='accountant'")
            conn.commit()
        self.assertEqual(self.server_client.get(self.path, headers=self.headers("accountant")).status_code, 403)

    def test_conflict_retry_and_noop_do_not_overwrite(self):
        first = self.get()
        request = self.payload(first, phone="+000 123")
        saved = self.put(request)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(self.put(request).status_code, 200)
        conflict = self.put(self.payload(first, phone="+000 456"))
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(self.get()["card"]["phone"], "+000 123")
        current = self.get()
        self.assertEqual(self.put(self.payload(current)).get_json()["employee"]["revision"], current["revision"])
        with closing(self.server.get_db_connection()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM employee_card_audit").fetchone()[0], 1)
        other = self.payload(current, phone="+000 999")
        self.assertEqual(self.put(other).status_code, 200)
        self.assertEqual(self.put(request).status_code, 409)

    def test_missing_or_wrong_key_fail_closed_and_recovery(self):
        result = self.put(self.payload(self.get(), **self.synthetic()))
        self.assertEqual(result.status_code, 200)
        correct_key = os.environ["SMARTREPORT_EMPLOYEE_CARD_KEY"]
        with patch.dict(os.environ, {"SMARTREPORT_EMPLOYEE_CARD_KEY": ""}):
            self.assertEqual(self.server_client.get(self.path, headers=self.headers()).status_code, 503)
            self.assertEqual(self.put(self.payload(result.get_json()["employee"])).status_code, 503)
        with patch.dict(os.environ, {"SMARTREPORT_EMPLOYEE_CARD_KEY": Fernet.generate_key().decode()}):
            self.assertEqual(self.server_client.get(self.path, headers=self.headers()).status_code, 503)
        self.assertEqual(os.environ["SMARTREPORT_EMPLOYEE_CARD_KEY"], correct_key)
        self.assertEqual(self.get()["card"]["account_number"], "0000123456")

    def test_employment_status_preserves_id_salary_and_history(self):
        before = self.get()
        request = self.payload(before, end_date="2026-10-31")
        request["is_active"] = False
        response = self.put(request)
        self.assertEqual(response.status_code, 200)
        employee = response.get_json()["employee"]
        self.assertEqual((employee["id"], employee["is_active"], employee["salary_rate"]),
                         (self.server_id, False, 245.8))
        request = self.payload(employee)
        request["is_active"] = True
        self.assertEqual(self.put(request).status_code, 200)
        self.assertEqual(self.get()["id"], self.server_id)

    def test_additive_migration_idempotent_and_encrypted_restore(self):
        with closing(self.desktop.get_db_connection()) as conn:
            old = list(conn.execute("SELECT id,name,salary_rate FROM employees"))
            cards.ensure_schema(conn.cursor(), "employees")
            cards.ensure_schema(conn.cursor(), "employees")
            self.assertEqual(list(conn.execute("SELECT id,name,salary_rate FROM employees")), old)
        self.put(self.payload(self.get(), **self.synthetic()))
        with closing(self.server.get_db_connection()) as source, closing(sqlite3.connect(":memory:")) as restored:
            source.backup(restored)
            restored.row_factory = sqlite3.Row
            cards.ensure_schema(restored.cursor(), "meta_employees")
            self.assertEqual(cards.card_record(restored.cursor(), self.server_id)["card"]["account_number"], "0000123456")
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def transport(self, method, url, **kwargs):
        import requests
        path = url.split("/api/employee-cards", 1)[1]
        response = self.server_client.open("/api/employee-cards" + path, method=method,
                                           headers=kwargs.get("headers"), json=kwargs.get("json"))
        result = requests.Response()
        result.status_code = response.status_code
        result._content = response.data
        result.headers.update(response.headers)
        return result

    def test_client_binding_encrypted_cache_and_server_conflict(self):
        from employee_cards_client import CardClient
        client = CardClient(self.desktop.get_db_connection)
        with patch("desktop_auth.current_token", return_value=self.tokens["admin"]), \
                patch("desktop_auth.server_url", return_value="http://127.0.0.1:5999"), \
                patch("requests.request", side_effect=self.transport):
            record = client.resolve(self.local_id)
            result = client.save(record, self.synthetic(), True, self.local_id)
            self.assertEqual(result["card"]["clearing"], "0001")
            fetched = client.resolve(self.local_id)
            self.assertEqual(fetched["card"], result["card"])
            with self.assertRaises(cards.CardConflict):
                client.save(record, dict(self.synthetic(), account_number="0000009999"), True, self.local_id)
        with closing(self.desktop.get_db_connection()) as conn:
            row = conn.execute("SELECT * FROM employees WHERE id=?", (self.local_id,)).fetchone()
            self.assertEqual((row["id"], row["card_server_id"], row["salary_rate"]), (self.local_id, self.server_id, 245.8))
            self.assertNotIn("0000123456", "\n".join(conn.iterdump()))

    def test_pdf_vector_private_preview_save_and_print_authorized(self):
        from employee_cards_ui import accountant_pdf, ConfidentialPreview
        from PyQt6.QtPrintSupport import QPrinter
        record = self.put(self.payload(self.get(), **self.synthetic())).get_json()["employee"]
        pdf = accountant_pdf([record], self.desktop.DEFAULT_FONT, self.desktop.DEFAULT_FONT_BOLD)
        reader = PdfReader(BytesIO(pdf))
        text = " ".join(page.extract_text() for page in reader.pages)
        self.assertIn("0000123456", text)
        self.assertIn("Clearingnummer", text)
        for forbidden in ("OB1", "Смены", "245.8", "клиентская"):
            self.assertNotIn(forbidden, text)
        for page in reader.pages:
            self.assertEqual(len(page.images), 0)
            for definition in page["/Resources"]["/Font"].values():
                font = definition.get_object()
                if "/ToUnicode" in font:
                    self.assertIn("/FontFile2", font["/FontDescriptor"].get_object())
        from unittest.mock import Mock
        client = Mock()
        preview = ConfidentialPreview(pdf, client, None)
        self.addCleanup(preview.release_resources)
        self.assertEqual(preview.document.pageCount(), len(reader.pages))
        preview.set_zoom(2)
        self.assertEqual(preview.preview.zoomFactor(), 2)
        target = Path(self._case.name) / "private.pdf"
        with patch("employee_cards_ui.QFileDialog.getSaveFileName", return_value=(str(target), "PDF")), \
                patch("employee_cards_ui.QMessageBox.question", return_value=self.desktop.QMessageBox.StandardButton.Yes), \
                patch("employee_cards_ui.QMessageBox.information"):
            preview.save_as()
        self.assertEqual(target.read_bytes(), pdf)
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(Path(self._case.name) / "printed.pdf"))
        printer.setResolution(150)
        preview.print_document(printer)
        self.assertEqual(len(PdfReader(printer.outputFileName()).pages), len(reader.pages))
        client.authorize.side_effect = cards.CardError("Доступ запрещён.")
        target.unlink()
        with patch("employee_cards_ui.QFileDialog.getSaveFileName") as save_dialog, \
                patch("employee_cards_ui.QMessageBox.warning"):
            preview.save_as()
        save_dialog.assert_not_called()
        self.assertFalse(target.exists())

    def test_card_dialog_partial_save_unsaved_cancel_and_bank_verification(self):
        from employee_cards_ui import EmployeeCardDialog
        from unittest.mock import Mock
        record = self.get()
        client = Mock()
        client.save.return_value = self.put(self.payload(record, **self.synthetic())).get_json()["employee"]
        dialog = EmployeeCardDialog(client, record)
        dialog.fields["clearing"].setText("0001")
        dialog.fields["account_number"].setText("0000123456")
        self.assertFalse(dialog.confirm_button.isEnabled())
        self.assertTrue(dialog.dirty())
        with patch("employee_cards_ui.QMessageBox.question", return_value=self.desktop.QMessageBox.StandardButton.No):
            self.assertFalse(dialog.can_close())
        with patch("employee_cards_ui.QMessageBox.information"):
            dialog.save()
        client.save.assert_called_once()
        self.assertFalse(dialog.dirty())
        self.assertTrue(dialog.confirm_button.isEnabled())
        dialog.close()

    def test_no_leak_into_catalog_public_routes_or_backup_to_accountant(self):
        self.put(self.payload(self.get(), **self.synthetic()))
        for route, headers in (("/get-active-employees", {}), ("/api/sync/employee-catalog", {"X-Sync-Key": "isolated-sync-test-key"})):
            response = self.server_client.get(route, headers=headers)
            self.assertEqual(response.status_code, 200)
            for forbidden in ("0000123456", "card_cipher", "identifier", "bank_name"):
                self.assertNotIn(forbidden, response.get_data(as_text=True))

    def test_accountant_provision_login_and_revoke_admin_only(self):
        path = "/api/employee-cards/accountants"
        account = {"username": "second-synthetic-accountant", "password": "synthetic-password-only"}
        self.assertEqual(self.server_client.post(path, headers=self.headers("accountant"), json=account).status_code, 403)
        self.assertEqual(self.server_client.post(path, headers=self.headers(), json=account).status_code, 201)
        self.assertEqual(self.server_client.post(path, headers=self.headers(), json=account).status_code, 409)
        response = self.server_client.post("/api/auth/token", json=account)
        self.assertEqual(response.status_code, 200)
        token = response.get_json()["token"]
        headers = {"Authorization": "Bearer " + token}
        self.assertEqual(self.server_client.get("/api/auth/me", headers=headers).get_json()["user"]["role"], "accountant")
        self.assertEqual(self.server_client.get("/api/employee-cards", headers=headers).status_code, 200)
        accounts = self.server_client.get(path, headers=self.headers()).get_json()["accountants"]
        identifier = next(row["id"] for row in accounts if row["username"] == account["username"])
        self.assertEqual(self.server_client.delete(f"{path}/{identifier}", headers=self.headers()).status_code, 200)
        self.assertEqual(self.server_client.get(self.path, headers=headers).status_code, 403)
        self.assertEqual(self.server_client.delete(f"{path}/{self.admin_id}", headers=self.headers()).status_code, 404)

    def test_client_old_server_http_and_unauthorized_export_fail_closed(self):
        from employee_cards_client import CardClient
        import requests
        client = CardClient()
        response = requests.Response()
        response.status_code = 404
        with patch("desktop_auth.current_token", return_value=self.tokens["admin"]), \
                patch("desktop_auth.server_url", return_value="http://127.0.0.1:5999"), \
                patch("requests.request", return_value=response):
            with self.assertRaisesRegex(cards.CardError, "обновление сервера"):
                client.employees()
        with patch("desktop_auth.server_url", return_value="http://remote.example.invalid"):
            with self.assertRaisesRegex(cards.CardError, "HTTPS"):
                client.employees()
        with patch("desktop_auth.current_token", return_value=""), \
                patch("desktop_auth.server_url", return_value="https://example.invalid"), \
                patch("requests.request") as network:
            with self.assertRaises(cards.CardError):
                client.authorize()
            network.assert_not_called()

    def test_accountant_offline_cannot_fall_through_to_financial_window(self):
        import desktop_login
        with patch.dict(os.environ, {"SYNC_API_KEY": "", "SMARTREPORT_TRIAL_MODE": "0"}), \
                patch("desktop_auth.load_saved_token", return_value="synthetic-token"), \
                patch("desktop_auth.check_token", return_value=None), \
                patch("desktop_auth.saved_account_role", return_value="accountant"), \
                patch("desktop_login.QMessageBox.warning") as warning:
            self.assertFalse(desktop_login.ensure_login())
            warning.assert_called_once()

    def test_server_status_change_conflicts_with_open_card(self):
        record = self.get()
        conn = self.server.get_db_connection()
        try:
            self.server.apply_employee_sync_event(conn.cursor(), {
                "name": record["name"], "employee_key": record["name"].casefold(),
                "operation_id": str(uuid.uuid4()), "revision": record["employee_revision"] + 1
            }, "deactivate")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.put(self.payload(record, phone="+000 123")).status_code, 409)

    def test_cipher_bound_to_employee_and_no_silent_rebinding(self):
        record = self.put(self.payload(self.get(), **self.synthetic())).get_json()["employee"]
        with self.assertRaises(cards.CardError):
            cards.decrypt_card(record["cipher"], self.server_id + 1)
        from employee_cards_client import CardClient
        client = CardClient(self.desktop.get_db_connection)
        with patch("desktop_auth.current_token", return_value=self.tokens["admin"]), \
                patch("desktop_auth.server_url", return_value="http://127.0.0.1:5999"), \
                patch("requests.request", side_effect=self.transport):
            client.resolve(self.local_id)
            with closing(self.desktop.get_db_connection()) as conn:
                conn.execute("UPDATE employees SET name='Synthetic changed identity' WHERE id=?", (self.local_id,))
                conn.commit()
            with self.assertRaisesRegex(cards.CardError, "перепривязка"):
                client.resolve(self.local_id)

    def test_real_local_http_existing_directory_card_save_reopen(self):
        import threading
        from werkzeug.serving import make_server
        from PyQt6.QtCore import QTimer
        from PyQt6.QtTest import QTest
        from employee_cards_ui import EmployeeCardDialog
        from employee_cards_client import CardClient
        service = make_server("127.0.0.1", 0, self.server.app)
        worker = threading.Thread(target=service.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(service.server_close)
        self.addCleanup(worker.join, 5)
        self.addCleanup(service.shutdown)
        base = f"http://127.0.0.1:{service.server_port}"
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_sprav = self.desktop.QWidget()
        window.setCentralWidget(window.tab_sprav)
        window.setup_sprav_tab()
        errors = []
        with patch("desktop_auth.current_token", return_value=self.tokens["admin"]), \
                patch("desktop_auth.server_url", return_value=base), patch.object(self.desktop, "CLOUD_URL", base):
            window.load_employees_table()
            for index in range(window.table_employees.rowCount()):
                if window.table_employees.item(index, 0).data(self.desktop.Qt.ItemDataRole.UserRole) == self.local_id:
                    window.table_employees.selectRow(index)
                    break

            def fill_and_save():
                dialog = self.desktop.QApplication.activeModalWidget()
                try:
                    self.assertIsInstance(dialog, EmployeeCardDialog)
                    self.assertEqual(dialog.record["id"], self.server_id)
                    dialog.fields["postal_code"].setText("00100")
                    dialog.fields["clearing"].setText("0001")
                    dialog.fields["account_number"].setText("0000123456")
                    dialog.save()
                    self.assertFalse(dialog.dirty())
                    dialog.accept()
                except Exception as error:
                    errors.append(str(error))
                    if dialog:
                        dialog.accept()

            QTimer.singleShot(100, fill_and_save)
            with patch("employee_cards_ui.QMessageBox.information"), \
                    patch("employee_cards_ui.QMessageBox.warning") as warning:
                QTest.mouseClick(window.findChild(self.desktop.QPushButton, "open_employee_card"),
                                 self.desktop.Qt.MouseButton.LeftButton)
            self.assertEqual(errors, [])
            warning.assert_not_called()
            record = CardClient(self.desktop.get_db_connection).resolve(self.local_id)
            self.assertEqual(record["card"]["account_number"], "0000123456")
            self.assertEqual(record["card"]["postal_code"], "00100")
        window.close()

    def test_export_selection_specific_selected_and_active(self):
        from employee_cards_ui import accountant_export
        from employee_cards_client import CardClient
        window = self.desktop.QWidget()
        window.table_employees = self.desktop.QTableWidget(1, 1)
        item = self.desktop.QTableWidgetItem("Synthetic Employee")
        item.setData(self.desktop.Qt.ItemDataRole.UserRole, self.local_id)
        window.table_employees.setItem(0, 0, item)
        window.table_employees.selectRow(0)
        inactive = dict(id=self.server_id + 1, name="Synthetic inactive", is_active=False)
        active = dict(id=self.server_id, name="Synthetic Employee", is_active=True)
        for mode in ("Конкретный сотрудник", "Выбранные сотрудники", "Все действующие сотрудники"):
            with self.subTest(mode=mode), \
                    patch.object(CardClient, "employees", return_value=[active, inactive]), \
                    patch.object(CardClient, "resolve", return_value={"id": self.server_id}), \
                    patch("employee_cards_ui.QInputDialog.getItem", side_effect=[
                        (mode, True), (f"Synthetic Employee · ID {self.server_id}", True)]), \
                    patch("employee_cards_ui.export_records") as export:
                accountant_export(window)
                self.assertEqual(export.call_args.args[1], [self.server_id])
        window.close()

    def test_card_changes_leave_every_financial_table_unchanged(self):
        def protected_snapshot():
            with closing(self.server.get_db_connection()) as conn:
                names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                return {name: list(map(tuple, conn.execute('SELECT * FROM "' + name + '"')))
                        for name in names if name not in {
                            "meta_employees", "api_tokens", "employee_card_audit", "employee_card_operations", "sqlite_sequence"
                        }}
        before = protected_snapshot()
        self.assertEqual(self.put(self.payload(self.get(), **self.synthetic())).status_code, 200)
        self.assertEqual(protected_snapshot(), before)

    def test_dataset_boundary_applies_to_cards_and_exports(self):
        from sync_dataset import HEADER
        dataset = str(uuid.uuid4())
        with closing(self.server.get_db_connection()) as conn:
            conn.execute("CREATE TABLE sync_dataset(id INTEGER PRIMARY KEY,dataset_id TEXT)")
            conn.execute("INSERT INTO sync_dataset VALUES(1,?)", (dataset,))
            conn.commit()
        for path in (self.path, self.path + "/audit", "/api/employee-cards"):
            self.assertEqual(self.server_client.get(path, headers=self.headers()).status_code, 409)
            self.assertEqual(self.server_client.get(path, headers=self.headers() | {HEADER: dataset}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
