import json
import sqlite3
import unittest
import uuid
from unittest.mock import patch

import test_customer_report as reports
import test_ob_transport as transport
from customer_pricing import agreed_prices_for_date, ensure_pricing_schema, parse_pricing, row_prices, store_pricing


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
                 "date_from": "2026-10-20", "date_to": "2026-10-20", "source": "Synthetic contract",
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
