"""Synthetic acceptance through real loopback HTTP and actual desktop handlers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import random
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests
from pypdf import PdfReader
from werkzeug.serving import make_server
from werkzeug.security import generate_password_hash

import test_catalog_sync as catalog
import test_employee_cards as card_fixture
import test_new_shift_prices as price_fixture
from test_payroll_history import PARAMS

ROOT = Path(os.environ["SMARTREPORT_QA_ROOT"]) if os.environ.get("SMARTREPORT_QA_ROOT") else None


def hashes(connection):
    return {name: hashlib.sha256(repr(sorted(map(tuple, connection.execute(
        'SELECT * FROM "' + name.replace('"', '""') + '"')), key=repr)).encode()).hexdigest()
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class LiveHTTP:
    def start_http(self):
        self.http = make_server("127.0.0.1", 0, self.server.app, threaded=True, ssl_context="adhoc")
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_http)
        self.url = f"https://127.0.0.1:{self.http.server_port}"
        original_request = requests.Session.request
        base = self.url
        def local_request(session, method, url, **kwargs):
            if url.startswith(base + "/") or url == base:
                kwargs["verify"] = False  # Only this ephemeral synthetic TLS server.
            return original_request(session, method, url, **kwargs)
        certificate = patch.object(requests.Session, "request", local_request)
        certificate.start()
        self.addCleanup(certificate.stop)
        self.headers = {"X-Sync-Key": "isolated-sync-test-key"}
        self.assertEqual(requests.get(self.url, timeout=10).status_code, 200)

    def stop_http(self):
        self.http.shutdown()
        self.thread.join(timeout=10)
        self.http.server_close()
        if self.thread.is_alive():
            raise RuntimeError("Local QA server did not stop")

    def metric(self, check, expected, actual):
        entry = dict(test=self.id(), check=check, expected=expected, actual=actual,
                     passed=expected == actual, run=os.environ.get("SMARTREPORT_QA_RUN", "final"))
        with (ROOT / "results" / "scenario-metrics.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.assertEqual(actual, expected, check)


@unittest.skipIf(ROOT is None, "Requires isolated Run-QA.ps1 environment")
class EndToEndTests(LiveHTTP, unittest.TestCase):
    setUpClass = classmethod(catalog.CatalogSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog.CatalogSyncTests.tearDownClass.__func__)
    _add_company = catalog.CatalogSyncTests._add_company
    payload = price_fixture.NewShiftPricesTests.payload
    desktop_save = price_fixture.NewShiftPricesTests.desktop_save

    def setUp(self):
        price_fixture.NewShiftPricesTests.setUp(self)
        self.start_http()
        self.url_patch = patch.object(self.desktop, "CLOUD_URL", self.url)
        self.url_patch.start()
        self.addCleanup(self.url_patch.stop)
        self.env_patch = patch.dict(os.environ, {"SMARTREPORT_CLOUD_URL": self.url})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.web = requests.Session()
        self.addCleanup(self.web.close)
        with closing(self.server.get_db_connection()) as conn:
            conn.execute("INSERT INTO users(username,password_hash,role,is_active,employee_id,created_at) "
                         "VALUES('Price Test',?,'user',1,?,datetime('now'))",
                         (generate_password_hash("qa-worker-password"), self.employee_id))
            conn.commit()
        response = self.web.post(self.url + "/login",
                                 json={"username": "Price Test", "password": "qa-worker-password"}, timeout=10)
        self.assertEqual(response.status_code, 200, response.text)
        self.metric("real worker HTTP login", True, response.json()["ok"])

    def sync(self):
        d = self.desktop
        host = SimpleNamespace(_manual_sync_requested=False)
        host.apply_remote_shift = lambda cursor, row: d.SmartReportApp.apply_remote_shift(host, cursor, row)
        host._sync_expense_document_outbox = lambda: d.SmartReportApp._sync_expense_document_outbox(host)
        return d.SmartReportApp.perform_sync_logic(host)

    def create(self, **changes):
        body = self.payload(**changes)
        response = self.web.post(self.url + "/submit-shift", json=body, timeout=10)
        self.assertEqual(response.status_code, 201, response.text)
        return body, response

    def test_catalog_web_desktop_prices_salary_invoice_close_and_restore(self):
        """8h web + 5h desktop -> invoice7660, payroll2600, Balance unchanged, verified restore."""
        from customer_report import write_customer_pdf
        import payroll_management
        import payroll_ui
        import server_backup
        from portable_recovery import decrypt, publish, restore

        self.create(hours=8)
        self.desktop_save(hours=5)
        self.sync()
        self.sync()
        d = self.desktop
        with closing(d.get_db_connection()) as conn:
            rows = conn.execute("SELECT * FROM shifts WHERE employee='Price Test'").fetchall()
            self.metric("both creation paths, no duplicates", 2, len(rows))
            self.metric("work total", 13, sum(row["hours"] for row in rows))
            self.metric("snapshotted base and travel", [[520, 450], [520, 450]], [
                [json.loads(r["customer_pricing"])["base"], json.loads(r["customer_pricing"])["travel"]]
                for r in rows])
            balance = list(map(tuple, conn.execute("SELECT * FROM balance")))
            reports = payroll_management.reports(
                conn.cursor(), "2026-10-01", "2026-10-31", d.swedish_public_holidays, d.swedish_workday_norm)
            selected = next(r for r in reports if r["employee"] == "Price Test")
            self.metric("current salary rate, not customer rate", 200, selected["rate"])
            self.metric("13 hours without times: base only, not zero", [13, 2600, 0, 2600],
                        [selected["categories"]["unknown"], selected["base"], selected["premium"], selected["total"]])
            self.metric("Swedish October workday norm", 176, selected["standard_norm"])
        output = ROOT / "results" / "e2e"
        output.mkdir(exist_ok=True)
        salary = output / "salary.pdf"
        payroll_ui.write_pdf(str(salary), reports, d.DEFAULT_FONT, d.DEFAULT_FONT_BOLD)
        self.metric("salary PDF generated with selectable synthetic name", True,
                    "Price Test" in "\n".join(p.extract_text() for p in PdfReader(salary).pages))
        window = d.SmartReportApp.__new__(d.SmartReportApp)
        d.QMainWindow.__init__(window)
        self.addCleanup(window.close)
        window.tab_invoice = d.QWidget()
        window.tab_dagbok = d.QWidget()
        window.setup_invoice_tab()
        window.setup_dagbok_tab()
        window.inv_comp_cb.setCurrentText("Renatur")
        with closing(d.get_db_connection()) as conn:
            oid = conn.execute("SELECT id FROM objects WHERE company='Renatur' AND name='Hotel'").fetchone()[0]
        window.inv_obj_cb.setCurrentIndex(window.inv_obj_cb.findData(oid))
        window.inv_date_from.setDate(d.QDate(2026, 10, 23))
        window.inv_date_to.setDate(d.QDate(2026, 10, 23))
        invoice = window.generate_invoice()
        self.metric("customer invoice base/travel/total", [6760, 900, 7660],
                    [invoice["totals"][k] for k in ("work_amount", "transport_amount", "invoice_total")])
        write_customer_pdf(invoice, str(output / "invoice.pdf"), d.DEFAULT_FONT, d.DEFAULT_FONT_BOLD, lambda t: t)
        self.metric("invoice text total", True, "7 660,00" in PdfReader(output / "invoice.pdf").pages[0].extract_text())
        self.metric("close two records", 2, window._set_period_invoiced(window.collect_invoice_report(), True)[0])
        self.metric("repeat close is no-op", 0, window._set_period_invoiced(window.collect_invoice_report(), True)[0])
        self.sync()
        with closing(d.get_db_connection()) as conn:
            self.metric("closing invoice does not affect Balance", balance, list(map(tuple, conn.execute("SELECT * FROM balance"))))
        # Retrieve the real backup API over local HTTP with a synthetic admin token.
        login = requests.post(self.url + "/api/auth/token",
                              json={"username": "admin", "password": "isolated-test-only"}, timeout=10)
        self.assertEqual(login.status_code, 200, login.text)
        target, manifest = server_backup.pull_backup(self.url, login.json()["token"], output / "backup")
        self.metric("backup complete", "complete", manifest["status"])
        source = target / "server_snapshot.db"
        bundle = publish(output / "portable", {"backup/server.db": source.read_bytes(),
                         "configuration/test.json": b'{"environment":"synthetic-local-only"}'},
                         "synthetic-recovery-password-only")
        destination = output / ("restored-" + uuid.uuid4().hex)
        restore(decrypt(bundle.read_bytes(), "synthetic-recovery-password-only"), destination)
        with closing(sqlite3.connect(source)) as before, \
                closing(sqlite3.connect(destination / "backup/server.db")) as after:
            self.metric("restored SQLite integrity", "ok", after.execute("PRAGMA integrity_check").fetchone()[0])
            self.metric("every restored table matches snapshot", hashes(before), hashes(after))
        # Keep synthetic end-to-end databases for reproduction, not the temporary fixture.
        with closing(d.get_db_connection()) as src, closing(sqlite3.connect(output / "desktop.db")) as dst:
            src.backup(dst)

    def test_exact_hotel_five_hours_worker_admin_and_desktop_survive_reopen(self):
        """Renatur/HOTEL: every creation path keeps520*5=2600;0/1trip ->0/450 after sync and process reopen."""
        from customer_pricing import row_prices
        hotel = "HOTEL Hazeliusbacken 20, på Djurgården"
        for connect, table in ((self.server.get_db_connection, "meta_objects"),
                               (self.desktop.get_db_connection, "objects")):
            with closing(connect()) as conn:
                conn.execute(f"UPDATE {table} SET name=? WHERE company='Renatur' AND name='Hotel'", (hotel,))
                conn.commit()
        admin = requests.Session()
        self.addCleanup(admin.close)
        login = admin.post(self.url + "/login",
                           json={"username": "admin", "password": "isolated-test-only"}, timeout=10)
        self.assertEqual(login.status_code, 200, login.text)
        identifiers = []
        for index, (actor, trips) in enumerate(
                ((actor, trips) for actor in ("worker", "web-admin", "desktop-admin") for trips in (0, 1))):
            day = f"2026-10-{19 + index:02}"
            if actor == "desktop-admin":
                row, _ = self.desktop_save(hours=5, object_name=hotel, transport=trips, day=day)
                identifier = row["sync_id"]
            else:
                body = self.payload(hours=5, transport=trips, object_name=hotel, date=day)
                response = (self.web if actor == "worker" else admin).post(
                    self.url + "/submit-shift", json=body, timeout=10)
                self.assertEqual(response.status_code, 201, response.text)
                if actor == "worker":
                    self.assertNotIn("customer_pricing", response.json())
                    self.assertNotIn("520", response.text)
                identifier = body["request_id"]
            identifiers.append((actor, trips, day, identifier))
        self.sync()
        self.sync()
        expected = {}
        for actor, trips, day, identifier in identifiers:
            for connect, table in ((self.server.get_db_connection, "incoming_submissions"),
                                   (self.desktop.get_db_connection, "shifts")):
                with closing(connect()) as conn:
                    row = dict(conn.execute(f"SELECT * FROM {table} WHERE sync_id=?", (identifier,)).fetchone())
                    if table == "incoming_submissions":
                        row["entry_type"] = row["submission_type"]
                    quote = row_prices(row)
                    actual = [row["hours"], quote["base"], row["hours"] * quote["base"],
                              row["transport"], quote["travel"], row["transport"] * quote["travel"]]
                    self.metric(f"HOTEL {actor} {trips}trip {table}", [5, 520, 2600, trips, 450, trips * 450], actual)
            expected[identifier] = [5, 520, 2600, trips, 450, trips * 450]
        # New interpreter, new SQLite connections: no in-memory price cache can satisfy this assertion.
        script = """
import json,sqlite3,sys
out={}
with sqlite3.connect(sys.argv[1]) as conn:
 conn.row_factory=sqlite3.Row
 for row in conn.execute('SELECT * FROM shifts WHERE employee=?',('Price Test',)):
  q=json.loads(row['customer_pricing'])
  out[row['sync_id']]=[row['hours'],q['base'],row['hours']*q['base'],row['transport'],q['travel'],row['transport']*q['travel']]
print(json.dumps(out))
"""
        reopened = json.loads(subprocess.check_output(
            [sys.executable, "-c", script, self.desktop.DB_FILE], text=True, encoding="utf-8"))
        self.metric("HOTEL all six records after independent process reopen", expected, reopened)
        d = self.desktop
        window = d.SmartReportApp.__new__(d.SmartReportApp)
        d.QMainWindow.__init__(window)
        self.addCleanup(window.close)
        window.tab_invoice = d.QWidget()
        window.tab_dagbok = d.QWidget()
        window.setup_invoice_tab()
        window.setup_dagbok_tab()
        window.inv_comp_cb.setCurrentText("Renatur")
        with closing(d.get_db_connection()) as conn:
            oid = conn.execute("SELECT id FROM objects WHERE company='Renatur' AND name=?", (hotel,)).fetchone()[0]
        window.inv_obj_cb.setCurrentIndex(window.inv_obj_cb.findData(oid))
        for actor, trips, day, identifier in identifiers:
            window.inv_date_from.setDate(d.QDate.fromString(day, "yyyy-MM-dd"))
            window.inv_date_to.setDate(d.QDate.fromString(day, "yyyy-MM-dd"))
            totals = window.generate_invoice()["totals"]
            self.metric(f"HOTEL invoice {actor} {trips}trip", [5, 2600, trips * 450, 2600 + trips * 450],
                        [totals[k] for k in ("hours", "work_amount", "transport_amount", "invoice_total")])

    def test_concurrent_duplicate_requests_create_one_record(self):
        """Eight simultaneous retries of one web request must create exactly one shift."""
        body = self.payload()
        cookies = self.web.cookies.get_dict()
        def post(_):
            return requests.post(self.url + "/submit-shift", json=body, cookies=cookies, timeout=20).status_code
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = sorted(pool.map(post, range(8)))
        self.metric("concurrent duplicate HTTP outcomes", [200] * 7 + [201], statuses)
        with closing(self.server.get_db_connection()) as conn:
            self.metric("one logical web record", 1, conn.execute(
                "SELECT count(*) FROM incoming_submissions WHERE sync_id=?", (body["request_id"],)).fetchone()[0])

    def test_concurrent_edits_have_one_winner_and_explicit_conflict(self):
        """Two editors sharing version1: only one update succeeds; no lost update."""
        body, _ = self.create()
        def change(hours):
            payload = dict(body, hours=hours, record_type="work", rate=0)
            response = requests.post(self.url + "/sync/push", headers=self.headers,
                json={"changes": [dict(sync_id=body["request_id"], operation_id=str(uuid.uuid4()),
                      operation="upsert", base_version=1, shift=payload)]}, timeout=20)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()["results"][0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, (6, 7)))
        self.metric("exactly one winning concurrent edit", 1, sum(bool(r["ok"]) for r in results))
        self.metric("explicit conflict on stale writer", True,
                    any(not r["ok"] and r.get("conflict") for r in results))

    def test_lost_ack_retries_real_http_without_duplicate_or_repricing(self):
        """Lose server ACK after commit, change catalogue, retry exact operation and keep original snapshot."""
        row, _ = self.desktop_save(hours=5)
        real_post = requests.post
        lost = []
        def post(url, **kwargs):
            response = real_post(url, **kwargs)
            if url.endswith("/sync/push") and not lost:
                lost.append(True)
                with closing(self.server.get_db_connection()) as conn:
                    conn.execute("UPDATE meta_objects SET rate=630,transport_rate=550 WHERE company='Renatur'")
                    conn.commit()
                raise requests.ConnectionError("QA simulated lost ACK after server commit")
            return response
        with patch.object(self.desktop.requests, "post", side_effect=post):
            with self.assertRaises(requests.ConnectionError):
                self.sync()
        self.sync()
        self.sync()
        self.metric("fault reached commit-before-ACK", [True], lost)
        for connect, table in ((self.server.get_db_connection, "incoming_submissions"),
                               (self.desktop.get_db_connection, "shifts")):
            with closing(connect()) as conn:
                rows = conn.execute(f"SELECT customer_pricing FROM {table} WHERE sync_id=?", (row["sync_id"],)).fetchall()
                self.metric(table + " retry count and snapshot", [1, 520, 450], [
                    len(rows), json.loads(rows[0][0])["base"], json.loads(rows[0][0])["travel"]])

    def test_closed_payroll_adjustment_and_payment_are_separate_over_http(self):
        """Close400, add2h -> frozen400, adjustment+200 once, no payment or Balance change."""
        import payroll_history as history
        self.create(hours=4)
        headers = self.headers
        def action(kind, **values):
            events = requests.get(self.url + "/sync/payroll", headers=headers, timeout=10).json()["events"]
            body = dict(kind=kind, employee="Price Test", operation_id=str(uuid.uuid4()),
                        expected_seq=len(events), **values)
            response = requests.post(self.url + "/sync/payroll", json=body, headers=headers, timeout=10)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json(), body
        with patch.object(history, "WRITES_ENABLED", True):
            action("rate", effective_from="2026-10-01", rate=100)
            current, _ = action("preview", date_from="2026-10-01", date_to="2026-10-31", parameters=PARAMS)
            closed, _ = action("close", date_from="2026-10-01", date_to="2026-10-31",
                               parameters=PARAMS, preview_hash=current["preview"]["preview_hash"])
            self.create(hours=2)
            preview, _ = action("preview", date_from="2026-10-01", date_to="2026-10-31", parameters=PARAMS)
            self.metric("closed snapshot not silently recalculated", [400, False, True], [
                preview["preview"]["previous"]["amounts"]["base_salary"],
                preview["preview"]["previous"]["paid"], preview["preview"]["source_changed"]])
            adjusted, body = action("adjust", date_from="2026-10-01", date_to="2026-10-31", parameters=PARAMS,
                preview_hash=preview["preview"]["preview_hash"], reason="Synthetic approved missing two hours")
            event = json.loads(adjusted["events"][-1]["event_json"])
            self.metric("explicit adjustment difference", 200, event["difference"]["base_salary"])
            retry = requests.post(self.url + "/sync/payroll", json=body, headers=headers, timeout=10)
            self.metric("adjustment retry no additional ledger event", len(adjusted["events"]), len(retry.json()["events"]))
            self.metric("adjustment does not mark paid", False, event["snapshot"]["paid"])


@unittest.skipIf(ROOT is None, "Requires isolated Run-QA.ps1 environment")
class CardRecoveryHTTPTests(LiveHTTP, unittest.TestCase):
    setUpClass = classmethod(catalog.CatalogSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog.CatalogSyncTests.tearDownClass.__func__)
    _add_company = catalog.CatalogSyncTests._add_company
    payload = card_fixture.EmployeeCardTests.payload
    synthetic = card_fixture.EmployeeCardTests.synthetic

    def setUp(self):
        card_fixture.EmployeeCardTests.setUp(self)
        self.start_http()

    def test_real_http_roles_conflict_and_key_restoration(self):
        """Admin/accountant access, worker denied, card conflict, encrypted DB+key restored."""
        from cryptography.fernet import Fernet
        from portable_recovery import decrypt, publish, restore
        header = lambda role: {"Authorization": "Bearer " + self.tokens[role]}
        for role, expected in (("admin", 200), ("accountant", 200), ("user", 403)):
            response = requests.get(self.url + self.path, headers=header(role), timeout=10)
            self.metric(role + " card access", expected, response.status_code)
        original = requests.get(self.url + self.path, headers=header("admin"), timeout=10).json()["employee"]
        body = self.payload(original, **self.synthetic())
        saved = requests.put(self.url + self.path, headers=header("admin"), json=body, timeout=10)
        self.assertEqual(saved.status_code, 200, saved.text)
        stale = self.payload(original, phone="+000 111111")
        self.metric("stale bank card edit rejected", 409, requests.put(
            self.url + self.path, headers=header("accountant"), json=stale, timeout=10).status_code)
        output = ROOT / "results" / "card-recovery"
        output.mkdir(exist_ok=True)
        with closing(self.server.get_db_connection()) as src, \
                closing(sqlite3.connect(output / "snapshot.db")) as dst:
            src.backup(dst)
        key = os.environ["SMARTREPORT_EMPLOYEE_CARD_KEY"].encode()
        bundle = publish(output, {"backup/server.db": (output / "snapshot.db").read_bytes(),
                                 "recovery/card-key": key}, "synthetic-recovery-password-only")
        recovered = decrypt(bundle.read_bytes(), "synthetic-recovery-password-only")
        destination = output / ("independent-" + uuid.uuid4().hex)
        restore(recovered, destination)
        with closing(sqlite3.connect(destination / "backup/server.db")) as conn:
            conn.row_factory = sqlite3.Row
            ciphers = conn.execute("SELECT card_cipher FROM meta_employees WHERE card_cipher IS NOT NULL").fetchall()
            restored_key = recovered["recovery/card-key"]
            cards = [json.loads(Fernet(restored_key).decrypt(r[0].encode()))["card"] for r in ciphers]
            self.metric("restored card retains leading zeros", "0000123456", cards[0]["account_number"])


@unittest.skipIf(ROOT is None, "Requires isolated Run-QA.ps1 environment")
class IsolationGuardTests(unittest.TestCase):
    def test_production_database_and_external_network_are_denied(self):
        """Negative guard tests use an unconnected socket and deny DB before opening it."""
        with self.assertRaisesRegex(PermissionError, "SQLite outside"):
            sqlite3.connect(str(ROOT.parent / "forbidden.db"))
        self.assertFalse((ROOT.parent / "forbidden.db").exists())
        with socket.socket() as client, self.assertRaisesRegex(PermissionError, "loopback"):
            client.connect(("192.0.2.1", 443))


class BalanceAndPreviewTests(unittest.TestCase):
    setUpClass = classmethod(catalog.CatalogSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog.CatalogSyncTests.tearDownClass.__func__)
    _add_company = catalog.CatalogSyncTests._add_company

    def setUp(self):
        catalog.CatalogSyncTests.setUp(self)
        self.addCleanup(self._case.cleanup)
        d = self.desktop
        self.window = d.SmartReportApp.__new__(d.SmartReportApp)
        d.QMainWindow.__init__(self.window)
        self.addCleanup(self.window.close)
        self.window.tab_balance = d.QWidget()
        self.window.setup_balance_tab()
        self.window.bal_emp_cb.setCurrentText("Synthetic Contact")

    def test_balance_actual_add_edit_cancel_delete_with_decimal_comma(self):
        """Actual Balance handlers: +100.25,-40 ->60.25, edit ->50.25; delete cancellation is read-only."""
        d, window = self.desktop, self.window
        window.bal_amount.setText("100,25")
        self.assertTrue(window.add_balance_entry())
        window.bal_amount.setText("-40")
        self.assertTrue(window.add_balance_entry())
        self.assertEqual(window.total_sum_label.text(), "60.25 kr")
        identifier = window.table_balance.item(0, 0).data(d.Qt.ItemDataRole.UserRole)
        with patch.object(d.QMessageBox, "information"):
            self.assertTrue(window.update_balance_entry(identifier, amount="-50"))
        self.assertEqual(window.total_sum_label.text(), "50.25 kr")
        window.table_balance.selectRow(0)
        with patch.object(d.QMessageBox, "question", return_value=d.QMessageBox.StandardButton.No):
            self.assertFalse(window.delete_balance_entry())
        self.assertEqual(window.table_balance.rowCount(), 2)
        with patch.object(d.QMessageBox, "question", return_value=d.QMessageBox.StandardButton.Yes):
            self.assertTrue(window.delete_balance_entry())
        self.assertEqual(window.total_sum_label.text(), "100.25 kr")

    def test_balance_rejects_nonfinite_add_without_database_changes(self):
        """NaN/infinity are not money; reject before insert and before rendering totals."""
        for text in ("nan", "inf", "-inf", "1e999"):
            with self.subTest(amount=text), patch.object(self.desktop.QMessageBox, "warning") as warning:
                self.window.bal_amount.setText(text)
                with closing(self.desktop.get_db_connection()) as conn:
                    before = list(conn.iterdump())
                self.assertFalse(self.window.add_balance_entry())
                warning.assert_called_once()
                with closing(self.desktop.get_db_connection()) as conn:
                    self.assertEqual(list(conn.iterdump()), before)

    def test_balance_rejects_nonfinite_update_without_database_changes(self):
        """An existing financial operation must not be replaced with NaN/infinity."""
        window = self.window
        window.bal_amount.setText("100")
        self.assertTrue(window.add_balance_entry())
        identifier = window.table_balance.item(0, 0).data(self.desktop.Qt.ItemDataRole.UserRole)
        for text in ("nan", "inf", "-inf", "1e999"):
            with self.subTest(amount=text), patch.object(self.desktop.QMessageBox, "warning") as warning, \
                    patch.object(self.desktop.QMessageBox, "information"):
                with closing(self.desktop.get_db_connection()) as conn:
                    before = list(conn.iterdump())
                self.assertFalse(window.update_balance_entry(identifier, amount=text))
                warning.assert_called_once()
                with closing(self.desktop.get_db_connection()) as conn:
                    self.assertEqual(list(conn.iterdump()), before)

    def pdf(self):
        from reportlab.pdfgen import canvas
        result = BytesIO()
        document = canvas.Canvas(result)
        document.drawString(30, 700, "Synthetic accountant data")
        document.save()
        return result.getvalue()

    def test_preview_save_as_from_memory_writes_exact_pdf_bytes(self):
        """Card/accountant preview is memory-only: Save As must not look for employee-data.pdf."""
        import payroll_preview
        data = self.pdf()
        target = Path(self._case.name) / "selected.pdf"
        dialog = payroll_preview.PayrollPdfPreviewDialog(data, target)
        try:
            with patch.object(payroll_preview.QFileDialog, "getSaveFileName", return_value=(str(target), "PDF")), \
                    patch.object(payroll_preview.QMessageBox, "critical") as critical, \
                    patch.object(payroll_preview.QMessageBox, "information"):
                dialog.save_as()
                critical.assert_not_called()
            self.assertEqual(target.read_bytes(), data)
        finally:
            dialog.release_resources()
            dialog.close()

    def test_preview_save_as_current_file_is_safe(self):
        """Choosing the already suggested existing PDF must succeed without SameFileError."""
        import payroll_preview
        data = self.pdf()
        target = Path(self._case.name) / "selected.pdf"
        target.write_bytes(data)
        dialog = payroll_preview.PayrollPdfPreviewDialog(target, target)
        try:
            with patch.object(payroll_preview.QFileDialog, "getSaveFileName", return_value=(str(target), "PDF")), \
                    patch.object(payroll_preview.QMessageBox, "critical") as critical, \
                    patch.object(payroll_preview.QMessageBox, "information"):
                dialog.save_as()
                critical.assert_not_called()
            self.assertEqual(target.read_bytes(), data)
        finally:
            dialog.release_resources()
            dialog.close()

    def test_preview_atomic_write_failure_is_visible_and_preserves_existing_pdf(self):
        """A failed save must not truncate an existing user-selected PDF."""
        import payroll_preview
        data = self.pdf()
        target = Path(self._case.name) / "existing.pdf"
        target.write_bytes(b"previous export")
        dialog = payroll_preview.PayrollPdfPreviewDialog(data, target)
        try:
            with patch.object(payroll_preview.QFileDialog, "getSaveFileName", return_value=(str(target), "PDF")), \
                    patch.object(payroll_preview, "QSaveFile") as save_file, \
                    patch.object(payroll_preview.QMessageBox, "critical") as critical, \
                    patch.object(payroll_preview.QMessageBox, "information") as success:
                save_file.return_value.open.return_value = False
                save_file.return_value.errorString.return_value = "QA simulated disk permission error"
                dialog.save_as()
                critical.assert_called_once()
                success.assert_not_called()
            self.assertEqual(target.read_bytes(), b"previous export")
        finally:
            dialog.release_resources()
            dialog.close()

    def test_twelve_employee_vector_pdf_one_page_and_high_resolution_preview(self):
        """12 synthetic Cyrillic employees fit one vector page, embedded fonts, 1800px render."""
        import payroll_management
        import payroll_ui
        import payroll_preview
        from PyQt6.QtCore import QSize
        from PyQt6.QtGui import QImage, QPainter
        d = self.desktop
        names = [f"Тестовый Сотрудник {index:02}" for index in range(1, 13)]
        with closing(d.get_db_connection()) as conn:
            for index, name in enumerate(names, 1):
                conn.execute("INSERT INTO employees(name,salary_rate,is_active) VALUES(?,?,1)", (name, 100 + index))
                conn.execute("INSERT INTO shifts(sync_id,date,employee,company,object_name,hours,rate,transport,entry_type) "
                             "VALUES(?, '2026-10-21',?,'Renatur','Synthetic Site',8,0,0,'work')",
                             (str(uuid.uuid4()), name))
            conn.commit()
            before = hashes(conn)
            reports = payroll_management.reports(
                conn.cursor(), "2026-10-01", "2026-10-31", d.swedish_public_holidays, d.swedish_workday_norm)
        output = ROOT / "results" / "e2e" if ROOT is not None else Path(self._case.name)
        output.mkdir(exist_ok=True)
        target = output / "salary-12-employees.pdf"
        payroll_ui.write_pdf(str(target), reports, d.DEFAULT_FONT, d.DEFAULT_FONT_BOLD)
        reader = PdfReader(target)
        self.assertEqual(len(reader.pages), 1)
        text = reader.pages[0].extract_text()
        self.assertTrue(all(name in text for name in names))
        fonts = reader.pages[0]["/Resources"]["/Font"].get_object()
        descriptors = [f.get_object().get("/FontDescriptor") for f in fonts.values()]
        self.assertTrue(any(value and "/FontFile2" in value.get_object() for value in descriptors))
        preview = payroll_preview.PayrollPdfPreviewDialog(target, target)
        try:
            preview.set_zoom(3)
            self.assertEqual(preview.preview.zoomFactor(), 3)
            image = preview.document.render(0, QSize(1800, 1273))
            self.assertFalse(image.isNull())
            white = QImage(image.size(), QImage.Format.Format_ARGB32)
            white.fill(d.Qt.GlobalColor.white)
            painter = QPainter(white)
            painter.drawImage(0, 0, image)
            painter.end()
            self.assertTrue(white.save(str(output / "salary-12-preview-1800.png")))
        finally:
            preview.release_resources()
            preview.close()
        with closing(d.get_db_connection()) as conn:
            self.assertEqual(hashes(conn), before)

    def test_closing_during_sync_waits_for_worker_without_destroying_thread(self):
        """A real running SyncWorker must finish before closing the parent window."""
        from PyQt6.QtCore import QTimer
        d, window = self.desktop, self.window
        started, release = threading.Event(), threading.Event()
        def blocked_sync():
            started.set()
            if not release.wait(10):
                raise RuntimeError("QA worker release timed out")
            return dict(conflicts=0, linked_legacy=0, unresolved_legacy=0, imported_employees=0,
                        unresolved_employees=0, conflict_details="", unresolved_legacy_details="")
        window.perform_sync_logic = blocked_sync
        window._sync_worker = None
        window._on_sync_completed = lambda *args: None
        window.auto_sync_timer = QTimer(window)
        window.auto_sync_timer.start(2000)
        window.show()
        window.background_sync_with_cloud()
        worker = window._sync_worker
        try:
            self.assertTrue(started.wait(3))
            window._sync_worker = None  # Completion signal may clear the reference before QThread.finished.
            self.assertFalse(window.close(), "Close must wait while synchronization is still running")
            self.assertFalse(window.auto_sync_timer.isActive())
            self.assertTrue(worker.isRunning())
            window.background_sync_with_cloud()
            self.assertEqual(window.findChildren(d.SyncWorker), [worker])
        finally:
            release.set()
            self.assertTrue(worker.wait(10000))
            self.qt_app.processEvents()
        self.assertFalse(window.isVisible())


class CalendarInvariantTests(unittest.TestCase):
    setUpClass = classmethod(catalog.CatalogSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog.CatalogSyncTests.tearDownClass.__func__)

    def test_800_intervals_against_independent_utc_minute_oracle(self):
        """Random and DST intervals: no hours lost/duplicated; OB belongs to worked time."""
        import app
        import payroll_management as management
        rng = random.Random(20261023)
        zone = ZoneInfo("Europe/Stockholm")
        cases = [("2026-03-28", 22, 6), ("2026-10-24", 22, 6),
                 ("2026-10-21", 16, 4), ("2026-12-24", 22, 6)]
        safe_hours = [0, 4, 6, 16, 17, 18, 22]
        for _ in range(796):
            day = date(2026, 1, 1) + timedelta(days=rng.randrange(365))
            left = rng.choice(safe_hours)
            right = rng.choice([h for h in safe_hours if h != left])
            duration = (right - left) % 24
            cases.append((day.isoformat(), left, duration))
        for day, start, duration in cases:
            with self.subTest(day=day, start=start, duration=duration):
                first = datetime.combine(date.fromisoformat(day), time(start), zone)
                last_local = first.replace(tzinfo=None) + timedelta(hours=duration)
                last = last_local.replace(tzinfo=zone)
                cursor = first.astimezone(timezone.utc)
                end = last.astimezone(timezone.utc)
                expected = {key: 0 for key in management.CATEGORIES}
                while cursor < end:
                    local = cursor.astimezone(zone)
                    minute = local.hour * 60 + local.minute
                    if local.weekday() >= 5 or local.date() in app.swedish_public_holidays(local.year):
                        key = "helg"
                    elif 360 <= minute < 1020:
                        key = "day"
                    elif 300 <= minute < 360 or 1020 <= minute < 1080:
                        key = "ob1"
                    elif 1080 <= minute < 1320:
                        key = "ob2"
                    else:
                        key = "ob3"
                    expected[key] += 1
                    cursor += timedelta(minutes=1)
                segments = management.split_interval(day, f"{start:02d}:00", last.strftime("%H:%M"),
                    dict(timezone="Europe/Stockholm", ambiguous_policy="earlier"), app.swedish_public_holidays)
                actual = {key: round(sum(s["hours"] for s in segments if s["category"] == key) * 60)
                          for key in management.CATEGORIES}
                self.assertEqual(actual, expected)
                self.assertAlmostEqual(sum(s["hours"] for s in segments), (end-first.astimezone(timezone.utc)).total_seconds()/3600)
