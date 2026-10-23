import json
import sqlite3
import unittest
import uuid
from datetime import datetime
from unittest.mock import patch

import test_customer_report as reports
import test_ob_transport as transport
from customer_pricing import agreed_prices_for_date, ensure_pricing_schema, fill_missing_prices, parse_pricing, row_prices, store_pricing


class HistoricalPricesTests(unittest.TestCase):
    setUpClass = classmethod(reports.CustomerReportTests.setUpClass.__func__)
    tearDown = reports.CustomerReportTests.tearDown
    shift = reports.CustomerReportTests.shift
    ob = reports.CustomerReportTests.ob

    def setUp(self):
        reports.CustomerReportTests.setUp(self)
        self.uri = f"file:pricing-{uuid.uuid4()}?mode=memory&cache=shared"
        master = sqlite3.connect(self.uri, uri=True)
        self.conn.backup(master)
        self.conn.close()
        self.conn = master
        master.row_factory = sqlite3.Row
        master.executescript("""
            ALTER TABLE shifts ADD COLUMN sync_version INTEGER DEFAULT 0;
            CREATE TABLE shift_sync_outbox(id INTEGER PRIMARY KEY,operation_id TEXT,sync_id TEXT,
                operation TEXT,base_version INTEGER,payload_json TEXT,state TEXT,created_at TEXT);
            CREATE TABLE balance(id INTEGER,change_amount REAL);
            INSERT INTO balance VALUES(1,100),(2,-20),(3,35);
            ALTER TABLE employees ADD COLUMN is_active INTEGER DEFAULT 1;
            ALTER TABLE objects ADD COLUMN is_active INTEGER DEFAULT 1;
            ALTER TABLE shifts ADD COLUMN work_transport_cache REAL DEFAULT 0;
            ALTER TABLE shifts ADD COLUMN ob_base_rate REAL;
        """)
        ensure_pricing_schema(master.cursor(), "shifts")
        master.commit()
        self.patcher.stop()
        def connect():
            conn = sqlite3.connect(self.uri, uri=True)
            conn.row_factory = sqlite3.Row
            return conn
        self.patcher = patch.object(self.desktop, "get_db_connection", connect)
        self.patcher.start()

    def confirmed_ob(self):
        identifier = self.ob()
        row = self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (identifier,)).fetchone()
        quote = json.loads(row["customer_pricing"])
        quote["ob"] = {"weekday_early_evening": 125}
        store_pricing(self.conn.cursor(), "shifts", identifier, quote)
        self.conn.commit()
        return identifier

    def load_history(self):
        if not hasattr(self.window, "table_shifts"):
            self.window.table_shifts = self.desktop.QTableWidget(0, 15)
        with patch.object(self.window, "load_dropdowns"), \
                patch.object(self.window, "apply_shifts_filters"), \
                patch.object(self.desktop.QMessageBox, "critical") as error:
            self.window.load_shifts_history()
        error.assert_not_called()

    def confirm_period(self, base="520", ob1="125", ob2="175"):
        def accept(dialog):
            fields = dialog.findChildren(self.desktop.QLineEdit)
            for field, value in zip(fields, (base, "450", ob1, ob2)):
                if field.objectName() != "customer_price_comment":
                    field.setText(value)
            return self.desktop.QDialog.DialogCode.Accepted
        with patch.object(self.desktop.QDialog, "exec", accept), \
                patch.object(self.desktop.QMessageBox, "information") as info, \
                patch.object(self.desktop.QMessageBox, "warning") as warning, \
                patch.object(self.window, "background_sync_with_cloud"), \
                patch.object(self.window, "load_shifts_history"):
            self.window.confirm_customer_prices()
        warning.assert_not_called()
        return info.call_args.args[2]

    def test_period_prices_feed_history_editor_and_report_without_duplicate_entry(self):
        identifier = self.shift(confirmed=False)
        self.confirm_period()
        self.load_history()
        self.assertEqual(self.window.table_shifts.item(0, 8).text(), "520.00")
        self.assertEqual(self.window.generate_invoice()["prepared_rows"][0]["rate"], 520)
        self.assertEqual(self.conn.execute("SELECT rate FROM shifts").fetchone()[0], 0)
        before = self.conn.execute("SELECT customer_pricing FROM shifts").fetchone()[0]
        self.window.table_shifts.selectRow(0)
        def accept(dialog):
            self.assertEqual(dialog.findChild(self.desktop.QDoubleSpinBox, "edit_shift_rate").value(), 520)
            return self.desktop.QDialog.DialogCode.Accepted
        with patch.object(self.desktop.QDialog, "exec", accept), \
                patch.object(self.desktop.QMessageBox, "information"), \
                patch.object(self.desktop.QMessageBox, "warning") as warning, \
                patch.object(self.window, "background_sync_with_cloud"), \
                patch.object(self.window, "load_shifts_history"):
            self.window.edit_shift()
        warning.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT customer_pricing FROM shifts").fetchone()[0], before)
        self.assertEqual(row_prices(dict(self.conn.execute("SELECT * FROM shifts").fetchone()))["base"], 520)
        self.assertEqual(self.window.generate_invoice()["totals"]["invoice_total"], 4160)

    def test_bulk_prices_keep_individual_quotes_legacy_zero_and_noop_repeat(self):
        legacy_id = self.shift(rate=610, confirmed=False)
        explicit_id = self.shift(rate=600)
        zero_id = self.shift()
        row = dict(self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (zero_id,)).fetchone())
        quote = json.loads(row["customer_pricing"])
        quote["base"] = 0
        store_pricing(self.conn.cursor(), "shifts", zero_id, quote)
        self.conn.commit()
        message = self.confirm_period()
        self.assertIn("Индивидуальные цены сохранены без замены", message)
        for identifier, expected in ((legacy_id, 610), (explicit_id, 600), (zero_id, 0)):
            row = dict(self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (identifier,)).fetchone())
            self.assertEqual(row_prices(row)["base"], expected)
        before = list(map(tuple, self.conn.execute("SELECT * FROM customer_price_audit")))
        queued = self.conn.execute("SELECT COUNT(*) FROM shift_sync_outbox").fetchone()[0]
        self.confirm_period()
        self.assertEqual(list(map(tuple, self.conn.execute("SELECT * FROM customer_price_audit"))), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shift_sync_outbox").fetchone()[0], queued)
        self.assertEqual(self.window.generate_invoice()["totals"]["work_amount"], (610 + 600) * 8)

    def test_ob_history_shows_client_base_and_premiums_not_payroll(self):
        identifier = self.ob()
        self.conn.execute("UPDATE shifts SET customer_pricing=NULL,start_time='16:00',end_time='20:00'")
        self.conn.commit()
        segments = list(map(tuple, self.conn.execute("SELECT * FROM ob_segments")))
        self.load_history()
        self.assertEqual(self.window.table_shifts.item(0, 8).text(), "Не задана")
        self.assertEqual(self.window.table_shifts.item(0, 13).text(), "Не задана")
        self.confirm_period()
        self.load_history()
        self.assertEqual(self.window.table_shifts.item(0, 8).text(), "520.00")
        self.assertEqual(self.window.table_shifts.item(0, 13).text(), "125.00")
        self.assertEqual(self.window.table_shifts.item(0, 14).text(), "Не задана")
        self.assertEqual(list(map(tuple, self.conn.execute("SELECT * FROM ob_segments"))), segments)
        self.assertEqual(self.window.generate_invoice()["totals"]["ob_amount"], 250)
        self.window.table_shifts.selectRow(0)
        def accept(dialog):
            base = dialog.findChild(self.desktop.QDoubleSpinBox, "edit_shift_rate")
            ob1 = dialog.findChild(self.desktop.QDoubleSpinBox, "edit_shift_client_ob1")
            self.assertEqual(base.value(), 520)
            self.assertFalse(base.isHidden())
            self.assertEqual(ob1.value(), 125)
            base.setValue(550)
            ob1.setValue(150)
            return self.desktop.QDialog.DialogCode.Accepted
        with patch.object(self.desktop.QDialog, "exec", accept), \
                patch.object(self.desktop.QMessageBox, "information"), \
                patch.object(self.desktop.QMessageBox, "warning") as warning, \
                patch.object(self.window, "background_sync_with_cloud"), \
                patch.object(self.window, "load_shifts_history"):
            self.window.edit_shift()
        warning.assert_not_called()
        report = self.window.generate_invoice()
        self.assertEqual(report["totals"]["work_amount"], 2200)
        self.assertEqual(report["totals"]["ob_amount"], 300)
        self.assertEqual(list(map(tuple, self.conn.execute("SELECT * FROM ob_segments"))), segments)
        self.load_history()
        self.assertEqual(self.window.table_shifts.item(0, 8).text(), "550.00")
        self.assertEqual(self.window.table_shifts.item(0, 13).text(), "150.00")

    def test_merging_missing_prices_does_not_extend_individual_validity(self):
        identifier = self.shift()
        row = dict(self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (identifier,)).fetchone())
        proposed = dict(json.loads(row["customer_pricing"]), base=600, date_from="2026-10-01", date_to="2026-10-31",
                        ob={"weekday_early_evening": 100})
        merged, retained = fill_missing_prices(row, proposed)
        self.assertEqual(merged["base"], 520)
        self.assertIn("base", retained)
        self.assertEqual(merged["ob"]["weekday_early_evening"], 100)
        self.assertEqual(merged["date_from"], row["date"])
        self.assertEqual(merged["date_to"], row["date"])

    def test_close_and_reopen_work_and_ob_noop_repeat_and_balance_unchanged(self):
        self.confirmed_ob()
        self.shift(hours=2, day="2026-10-22")
        before = list(self.conn.execute("SELECT * FROM balance"))
        segments = list(self.conn.execute("SELECT * FROM ob_segments"))
        report = self.window.collect_invoice_report()
        self.assertEqual(self.window._set_period_invoiced(report, True), (2, 6))
        self.assertEqual(self.conn.execute("SELECT SUM(invoiced) FROM shifts").fetchone()[0], 2)
        queue = list(self.conn.execute("SELECT * FROM shift_sync_outbox"))
        self.assertEqual(len(queue), 2)
        self.assertEqual(self.window._set_period_invoiced(self.window.collect_invoice_report(), True), (0, 0))
        self.assertEqual(list(self.conn.execute("SELECT * FROM shift_sync_outbox")), queue)
        self.assertEqual(self.window._set_period_invoiced(self.window.collect_invoice_report(), False), (2, 6))
        queue = list(self.conn.execute("SELECT * FROM shift_sync_outbox"))
        self.assertEqual(self.window._set_period_invoiced(self.window.collect_invoice_report(), False), (0, 0))
        self.assertEqual(list(self.conn.execute("SELECT * FROM shift_sync_outbox")), queue)
        self.assertEqual(list(self.conn.execute("SELECT * FROM balance")), before)
        self.assertEqual(list(self.conn.execute("SELECT * FROM ob_segments")), segments)

    def test_unknown_ob_blocks_close_but_preserves_preview_and_all_hours(self):
        self.ob()
        report = self.window.generate_invoice()
        self.assertEqual(report["totals"]["hours"], 4)
        self.assertIsNone(report["totals"]["invoice_total"])
        self.assertIn("№1 · 2026-10-21", self.window.invoice_status_note.toPlainText())
        self.assertIn("OB1", self.window.invoice_status_note.toPlainText())
        self.assertGreater(self.window.invoice_status_note.minimumHeight(), 32)
        with self.assertRaisesRegex(ValueError, "OB1"):
            self.window._set_period_invoiced(self.window.collect_invoice_report(), True)
        self.assertEqual(self.conn.execute("SELECT SUM(invoiced) FROM shifts").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shift_sync_outbox").fetchone()[0], 0)

    def test_current_catalogue_changes_cannot_reprice_historical_work_travel_or_ob(self):
        self.confirmed_ob()
        first = self.window.generate_invoice()
        self.assertEqual(first["totals"]["invoice_total"], 2780)
        self.assertEqual(first["totals"]["ob_amount"], 250)
        self.conn.execute("UPDATE objects SET rate=999,transport_rate=888")
        self.conn.commit()
        again = self.window.generate_invoice()
        self.assertEqual(first["cost_lines"], again["cost_lines"])
        self.assertEqual(again["totals"]["hours"], 4)
        self.assertEqual(again["totals"]["without_ob_hours"], 2)

    def test_legacy_zero_unknown_and_explicit_zero_are_distinct(self):
        identifier = self.shift(hours=2, transport=1, confirmed=False)
        first = self.window.generate_invoice()
        self.assertIsNone(first["totals"]["invoice_total"])
        self.assertIsNone(first["totals"]["work_amount"])
        row = dict(self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (identifier,)).fetchone())
        self.assertIsNone(row_prices(row)["base"])
        quote = {"version": 1, "company": row["company"], "object_name": row["object_name"],
                 "date_from": row["date"], "date_to": row["date"], "source": "Confirmed free test",
                 "base": 0, "travel": 0, "ob": {}}
        store_pricing(self.conn.cursor(), "shifts", identifier, quote)
        self.conn.commit()
        self.assertEqual(self.window.generate_invoice()["totals"]["invoice_total"], 0)

    def test_closed_price_protected_and_reopen_keeps_audit(self):
        identifier = self.confirmed_ob()
        self.window._set_period_invoiced(self.window.collect_invoice_report(), True)
        row = self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (identifier,)).fetchone()
        quote = json.loads(row["customer_pricing"])
        quote["base"] = 600
        with self.assertRaisesRegex(ValueError, "закрытая"):
            store_pricing(self.conn.cursor(), "shifts", identifier, quote)
        self.window._set_period_invoiced(self.window.collect_invoice_report(), False)
        store_pricing(self.conn.cursor(), "shifts", identifier, quote)
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM customer_price_audit").fetchone()[0], 2)

    def test_stale_report_rejected_atomically(self):
        self.shift(hours=2)
        report = self.window.collect_invoice_report()
        self.conn.execute("UPDATE shifts SET hours=3")
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "изменились"):
            self.window._set_period_invoiced(report, True)
        self.assertEqual(self.conn.execute("SELECT SUM(invoiced) FROM shifts").fetchone()[0], 0)

    def test_pricing_date_and_customer_validation(self):
        identifier = self.shift()
        row = dict(self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (identifier,)).fetchone())
        quote = json.loads(row["customer_pricing"])
        for key, value in (("company", "Other"), ("date_to", "2026-01-01"), ("base", float("nan")), ("source", "")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_pricing(dict(quote, **{key: value}), row)

    def test_confirm_prices_without_comment_records_confirmation_and_date(self):
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication, QDialog, QLineEdit, QLabel
        identifier = self.shift(confirmed=False)
        failures = []

        def accept_prices():
            try:
                dialog = QApplication.activeModalWidget()
                self.assertIsInstance(dialog, QDialog)
                labels = [label.text() for label in dialog.findChildren(QLabel)]
                self.assertIn("Комментарий к ценам:", labels)
                self.assertNotIn("Основание цен:", labels)
                comment = dialog.findChild(QLineEdit, "customer_price_comment")
                self.assertEqual(comment.text(), "")
                for field in dialog.findChildren(QLineEdit):
                    if field is not comment:
                        field.setText("520")
                dialog.accept()
            except Exception as error:
                failures.append(error)
                if QApplication.activeModalWidget():
                    QApplication.activeModalWidget().reject()

        QTimer.singleShot(0, accept_prices)
        with patch.object(self.desktop.QMessageBox, "information"), \
                patch.object(self.desktop.QMessageBox, "warning") as warning, \
                patch.object(self.window, "background_sync_with_cloud"):
            self.window.confirm_customer_prices()
        self.assertEqual(failures, [])
        warning.assert_not_called()
        row = dict(self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (identifier,)).fetchone())
        quote = json.loads(row["customer_pricing"])
        self.assertEqual(quote["source"], "")
        self.assertIs(quote["confirmed"], True)
        self.assertIsNotNone(datetime.fromisoformat(quote["confirmed_at"]).tzinfo)
        self.assertEqual(parse_pricing(quote, row), quote)
        self.assertEqual(self.window.generate_invoice()["totals"]["invoice_total"], 4160)
        queued = json.loads(self.conn.execute("SELECT payload_json FROM shift_sync_outbox").fetchone()[0])
        self.assertEqual(json.loads(queued["customer_pricing"]), quote)

    def test_confirmation_metadata_roundtrip_and_legacy_comments(self):
        identifier = self.shift()
        row = dict(self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (identifier,)).fetchone())
        legacy = json.loads(row["customer_pricing"])
        self.assertEqual(parse_pricing(legacy, row)["source"], legacy["source"])
        quote = dict(legacy, source="", confirmed=True, confirmed_at="2026-10-23T07:42:00+02:00")
        self.assertEqual(parse_pricing(quote, row), quote)
        for fields in ({"confirmed": False}, {"confirmed_at": "bad"},
                       {"confirmed_at": "2026-10-23T07:42:00"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                parse_pricing(dict(quote, **fields), row)

    def test_contract_reuse_only_in_valid_period_and_no_guess_on_conflict(self):
        first = self.shift(day="2026-10-21")
        row = self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (first,)).fetchone()
        quote = json.loads(row["customer_pricing"])
        quote["date_to"] = "2026-10-31"
        store_pricing(self.conn.cursor(), "shifts", first, quote)
        self.conn.commit()
        identity = {"date": "2026-10-25", "company": "Renatur", "object_name": "Tomteboda ÄTA"}
        self.assertEqual(json.loads(agreed_prices_for_date(self.conn.cursor(), "shifts", identity)), quote)
        self.assertIsNone(agreed_prices_for_date(self.conn.cursor(), "shifts", dict(identity, date="2026-11-01")))
        self.shift(day="2026-10-25", rate=700)
        self.assertIsNone(agreed_prices_for_date(self.conn.cursor(), "shifts", identity))

    def test_different_historical_ob_and_travel_prices_separate_lines(self):
        first = self.confirmed_ob()
        second = self.ob(day="2026-10-22", transport=1)
        row = self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (second,)).fetchone()
        quote = json.loads(row["customer_pricing"])
        quote.update(base=600, travel=500, ob={"weekday_early_evening": 150})
        store_pricing(self.conn.cursor(), "shifts", second, quote)
        self.conn.commit()
        report = self.window.generate_invoice()
        self.assertEqual(report["totals"]["hours"], 8)
        self.assertEqual(report["totals"]["ob_categories"], {"weekday_early_evening": 4})
        self.assertEqual(report["totals"]["work_amount"], 4480)
        self.assertEqual(report["totals"]["transport_amount"], 950)
        self.assertEqual(report["totals"]["ob_amount"], 550)
        self.assertEqual(report["totals"]["invoice_total"], 5980)
        self.assertEqual(len([line for line in report["cost_lines"] if line["label"] == "Resa"]), 2)


class PricingSyncTests(unittest.TestCase):
    setUpClass = classmethod(transport.OBTransportTests.setUpClass.__func__)
    tearDownClass = classmethod(transport.OBTransportTests.tearDownClass.__func__)
    setUp = transport.OBTransportTests.setUp
    _add_company = transport.OBTransportTests._add_company
    submit = transport.OBTransportTests.submit

    def test_ob_status_prices_segments_roundtrip_and_retry(self):
        self.submit("pricing-sync", "ob", "Travel B", 1)
        conn = self.server.get_db_connection()
        before = dict(conn.execute("SELECT * FROM incoming_submissions WHERE sync_id='pricing-sync'").fetchone())
        segments = list(map(tuple, conn.execute("SELECT * FROM ob_segments")))
        conn.close()
        quote = {"version": 1, "company": "Renatur", "object_name": "Travel B",
                 "date_from": "2026-10-20", "date_to": "2026-10-20", "source": "",
                 "confirmed": True, "confirmed_at": "2026-10-23T07:42:00+02:00",
                 "base": 300, "travel": 450, "ob": {"weekday_early_evening": 50, "weekday_late_evening_night": 70}}
        payload = {key: before[key] for key in ("date", "employee", "company", "object_name", "hours", "rate",
                   "transport", "comment", "start_time", "end_time")}
        payload.update(record_type="ob", invoiced=1, customer_pricing=quote)
        request = {"changes": [{"sync_id": "pricing-sync", "operation_id": "close-pricing-sync",
                               "operation": "upsert", "base_version": before["version"], "shift": payload}]}
        for _ in range(2):
            response = self.server_client.post("/sync/push", json=request,
                                               headers={"X-Sync-Key": "isolated-sync-test-key"})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertTrue(response.get_json()["results"][0]["ok"])
        conn = self.server.get_db_connection()
        stored = conn.execute("SELECT * FROM incoming_submissions WHERE sync_id='pricing-sync'").fetchone()
        self.assertEqual(stored["invoiced"], 1)
        self.assertEqual(stored["version"], before["version"] + 1)
        self.assertEqual(list(map(tuple, conn.execute("SELECT * FROM ob_segments"))), segments)
        remote = self.server.serialize_submission(stored, cursor=conn.cursor())
        conn.close()
        from types import SimpleNamespace
        local = self.desktop.get_db_connection()
        self.desktop.ensure_ob_schema(local.cursor())
        self.desktop.SmartReportApp.apply_remote_shift(SimpleNamespace(), local.cursor(), remote)
        row = local.execute("SELECT * FROM shifts WHERE sync_id='pricing-sync'").fetchone()
        self.assertEqual(row["invoiced"], 1)
        self.assertEqual(json.loads(row["customer_pricing"]), quote)
        local.close()
