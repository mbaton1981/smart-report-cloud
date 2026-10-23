import json
import unittest
from unittest.mock import patch

from customer_pricing import agreed_prices_for_date, parse_pricing, row_prices, store_pricing
from fixed_pricing import create_fixed, fixed_quote
import test_customer_pricing as historical


class FixedPriceTests(unittest.TestCase):
    setUpClass = classmethod(historical.HistoricalPricesTests.setUpClass.__func__)
    setUp = historical.HistoricalPricesTests.setUp
    tearDown = historical.HistoricalPricesTests.tearDown
    shift = historical.HistoricalPricesTests.shift
    ob = historical.HistoricalPricesTests.ob

    def fast(self, amount="3000", extras=None):
        self.window.fast_pris_check.setChecked(True)
        self.window.fast_pris_input.setText(amount)
        report = self.window.collect_invoice_report()
        if extras is not None:
            self.window._fast_pris_draft = {
                "scope": self.window._fixed_scope(report), "extras": extras,
            }
        return self.window.collect_invoice_report()

    def test_unknown_included_rates_close_persist_repeat_reopen_and_keep_payroll_balance(self):
        identifier = self.ob()
        self.conn.execute("UPDATE shifts SET customer_pricing=NULL")
        self.conn.commit()
        salary_before = list(map(tuple, self.conn.execute("SELECT * FROM ob_segments")))
        balance_before = list(map(tuple, self.conn.execute("SELECT * FROM balance")))
        self.assertEqual(self.window._set_period_invoiced(self.fast(), True), (1, 4))
        self.window.fast_pris_check.setChecked(False)
        self.window.fast_pris_input.clear()
        report = self.window.generate_invoice()
        self.assertEqual(report["totals"]["invoice_total"], 3000)
        self.assertEqual(report["totals"]["hours"], 4)
        self.assertEqual(report["totals"]["ob_categories"]["weekday_early_evening"], 2)
        self.assertEqual(len(report["cost_lines"]), 1)
        self.assertFalse(report["warnings"])
        quote = row_prices(dict(self.conn.execute("SELECT * FROM shifts").fetchone()))
        self.assertIsNone(quote["base"])
        self.assertIsNone(quote["travel"])
        self.assertEqual(set(quote["fixed"]["members"]), {identifier})
        audits = self.conn.execute("SELECT COUNT(*) FROM customer_price_audit").fetchone()[0]
        queued = self.conn.execute("SELECT COUNT(*) FROM shift_sync_outbox").fetchone()[0]
        self.assertEqual(self.window._set_period_invoiced(self.window.collect_invoice_report(), True), (0, 0))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM customer_price_audit").fetchone()[0], audits)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shift_sync_outbox").fetchone()[0], queued)
        self.assertEqual(self.window._set_period_invoiced(self.window.collect_invoice_report(), False), (1, 4))
        self.assertNotIn("fixed", row_prices(dict(self.conn.execute("SELECT * FROM shifts").fetchone())))
        self.assertEqual(list(map(tuple, self.conn.execute("SELECT * FROM ob_segments"))), salary_before)
        self.assertEqual(list(map(tuple, self.conn.execute("SELECT * FROM balance"))), balance_before)

    def test_explicit_ob_and_transport_extras_no_base_double_charge_and_keep_prices(self):
        identifier = self.ob()
        before = dict(self.conn.execute("SELECT * FROM shifts").fetchone())
        quote = json.loads(before["customer_pricing"])
        quote["ob"] = {"weekday_early_evening": 125}
        store_pricing(self.conn.cursor(), "shifts", identifier, quote)
        self.conn.commit()
        self.window._set_period_invoiced(self.fast(extras={identifier: {"ob": True, "travel": True}}), True)
        self.window.fast_pris_check.setChecked(False)
        report = self.window.generate_invoice()
        self.assertEqual(report["totals"]["invoice_total"], 3700)
        self.assertEqual(report["totals"]["work_amount"], 3000)
        self.assertEqual(report["totals"]["ob_amount"], 250)
        self.assertEqual(report["totals"]["transport_amount"], 450)
        after_quote = json.loads(self.conn.execute("SELECT customer_pricing FROM shifts").fetchone()[0])
        after_quote.pop("fixed")
        self.assertEqual(after_quote, quote)
        from test_customer_report import CustomerReportTests
        pdf = CustomerReportTests.pdf(self, report)
        first = "\n".join(page.extract_text() for page in pdf.pages[:-1])
        self.assertIn("3 700,00", first)
        self.assertIn("utöver fast pris", first)
        self.assertIn("Ingår", first)
        self.assertIn("Extra", first)
        self.assertNotIn("245", first)
        self.assertNotIn("SEK", pdf.pages[-1].extract_text())

    def test_unknown_extra_blocks_closure_but_preview_available(self):
        identifier = self.ob()
        self.conn.execute("UPDATE shifts SET customer_pricing=NULL")
        self.conn.commit()
        report = self.fast(extras={identifier: {"ob": True}})
        prepared = self.window._prepare_invoice_view(report)
        self.assertIsNone(prepared["totals"]["invoice_total"])
        self.assertEqual(prepared["totals"]["known_subtotal"], 3000)
        with self.assertRaisesRegex(ValueError, "OB1"):
            self.window._set_period_invoiced(report, True)
        self.assertEqual(self.conn.execute("SELECT invoiced FROM shifts").fetchone()[0], 0)

    def test_partial_selection_new_records_and_closed_records_cannot_reprice_package(self):
        self.shift(day="2026-10-20")
        self.shift(day="2026-10-21")
        self.window._set_period_invoiced(self.fast(), True)
        self.window.fast_pris_check.setChecked(False)
        self.window.inv_date_from.setDate(self.desktop.QDate(2026, 10, 21))
        partial = self.window.collect_invoice_report()
        self.assertIsNone(self.window._prepare_invoice_view(partial)["totals"]["invoice_total"])
        with self.assertRaisesRegex(ValueError, "весь"):
            self.window._set_period_invoiced(partial, False)
        self.window.inv_date_from.setDate(self.desktop.QDate(2026, 10, 1))
        self.shift(day="2026-10-22")
        full = self.window.generate_invoice()
        self.assertEqual(full["totals"]["hours"], 24)
        self.assertIsNone(full["totals"]["invoice_total"])
        self.assertEqual(full["totals"]["known_subtotal"], 3000)
        with self.assertRaisesRegex(ValueError, "Nya poster"):
            self.window._set_period_invoiced(self.window.collect_invoice_report(), True)

    def test_fixed_contract_not_reused_for_new_rows_and_invalid_members_rejected(self):
        identifier = self.shift()
        self.window._set_period_invoiced(self.fast(), True)
        row = dict(self.conn.execute("SELECT * FROM shifts").fetchone())
        self.assertIsNone(agreed_prices_for_date(self.conn.cursor(), "shifts", row))
        quote = json.loads(row["customer_pricing"])
        for change in ({"hours": 9}, {"sync_id": "other"}, {"employee": "Other"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_pricing(quote, dict(row, **change))
        with self.assertRaisesRegex(ValueError, "закрытым"):
            create_fixed(dict(self.window.collect_invoice_report(), is_fast_pris=True, fast_pris_amount=4000))

    def test_real_allocation_dialog_defaults_include_and_explicit_selection(self):
        identifier = self.ob()
        self.fast()
        def accept(dialog):
            boxes = dialog.findChildren(self.desktop.QCheckBox)
            self.assertEqual(len(boxes), 3)
            self.assertTrue(all(not box.isChecked() for box in boxes))
            dialog.findChild(self.desktop.QCheckBox, f"fixed_extra_{identifier}_travel").setChecked(True)
            return self.desktop.QDialog.DialogCode.Accepted
        with patch.object(self.desktop.QDialog, "exec", accept), \
                patch.object(self.desktop.QMessageBox, "warning") as warning:
            self.window.configure_fast_pris()
        warning.assert_not_called()
        report = self.window.generate_invoice()
        self.assertEqual(report["totals"]["invoice_total"], 3450)
        self.assertEqual(len(report["cost_lines"]), 2)

    def test_different_extra_rates_zero_and_scope_change(self):
        first = self.shift(rate=600)
        second = self.shift(day="2026-10-22", rate=700)
        report = self.fast(extras={first: {"base": True}, second: {"base": True}})
        self.window._set_period_invoiced(report, True)
        self.window.fast_pris_check.setChecked(False)
        prepared = self.window.generate_invoice()
        self.assertEqual(prepared["totals"]["invoice_total"], 3000 + 8 * (600 + 700))
        self.assertEqual(len(prepared["cost_lines"]), 3)
        self.window._set_period_invoiced(self.window.collect_invoice_report(), False)
        for record in self.conn.execute("SELECT * FROM shifts").fetchall():
            quote = json.loads(record["customer_pricing"])
            quote["base"] = 0
            store_pricing(self.conn.cursor(), "shifts", record["sync_id"], quote)
        self.conn.commit()
        report = self.fast(extras={first: {"base": True}, second: {"base": True}})
        self.window._set_period_invoiced(report, True)
        self.assertEqual(self.window.generate_invoice()["totals"]["invoice_total"], 3000)
        self.window.inv_obj_cb.setCurrentIndex(self.window.inv_obj_cb.findData(84))
        self.shift(object_name="HOTEL Djurgården")
        report = self.window.collect_invoice_report()
        self.assertEqual(report["fast_pris_extras"], {})

    def test_week_ten_employees_full_hours_without_postrow_prices(self):
        for day in range(5, 12):
            for employee in range(10):
                self.shift(name=f"Worker {employee}", day=f"2026-10-{day:02}", confirmed=False)
        self.window._set_period_invoiced(self.fast("50000"), True)
        self.window.fast_pris_check.setChecked(False)
        report = self.window.generate_invoice()
        self.assertEqual(len(report["prepared_rows"]), 10)
        self.assertEqual(len(report["journal_rows"]), 70)
        self.assertEqual(report["totals"]["hours"], 560)
        self.assertEqual(report["totals"]["invoice_total"], 50000)
        self.assertEqual(len(report["cost_lines"]), 1)


class FixedPriceSyncTests(unittest.TestCase):
    setUpClass = classmethod(historical.PricingSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(historical.PricingSyncTests.tearDownClass.__func__)
    setUp = historical.PricingSyncTests.setUp
    _add_company = historical.PricingSyncTests._add_company
    submit = historical.PricingSyncTests.submit

    def test_fixed_unknown_rates_server_roundtrip_retry_and_reopen(self):
        self.submit("fixed-sync", "ob", "Travel B", 1)
        conn = self.server.get_db_connection()
        row = dict(conn.execute("SELECT * FROM incoming_submissions WHERE sync_id='fixed-sync'").fetchone())
        segments = list(map(tuple, conn.execute("SELECT * FROM ob_segments")))
        conn.close()
        row["entry_type"] = "ob"
        report = {"journal_rows": [row], "company": row["company"], "sync_id": "object-test",
                  "date_from": row["date"], "date_to": row["date"], "fast_pris_amount": 3000}
        fixed = create_fixed(report)
        quote = fixed_quote(row, fixed)
        payload = {key: row[key] for key in ("date", "employee", "company", "object_name", "hours",
                   "rate", "transport", "comment", "start_time", "end_time")}
        payload.update(record_type="ob", invoiced=1, customer_pricing=quote)
        change = {"sync_id": row["sync_id"], "operation_id": "fixed-close", "operation": "upsert",
                  "base_version": row["version"], "shift": payload}
        for _ in range(2):
            response = self.server_client.post("/sync/push", json={"changes": [change]},
                                               headers={"X-Sync-Key": "isolated-sync-test-key"})
            self.assertEqual(response.status_code, 200, response.get_json())
        conn = self.server.get_db_connection()
        saved = dict(conn.execute("SELECT * FROM incoming_submissions WHERE sync_id='fixed-sync'").fetchone())
        self.assertEqual(json.loads(saved["customer_pricing"])["fixed"], fixed)
        self.assertEqual(saved["version"], row["version"] + 1)
        self.assertEqual(list(map(tuple, conn.execute("SELECT * FROM ob_segments"))), segments)
        conn.close()
        payload["invoiced"] = 0
        payload["customer_pricing"] = dict(quote)
        payload["customer_pricing"].pop("fixed")
        change.update(operation_id="fixed-reopen", base_version=saved["version"])
        response = self.server_client.post("/sync/push", json={"changes": [change]},
                                           headers={"X-Sync-Key": "isolated-sync-test-key"})
        self.assertEqual(response.status_code, 200, response.get_json())

    def test_incomplete_fixed_membership_rolls_back_whole_request(self):
        self.submit("fixed-first", "ob", "Travel B", 1)
        self.submit("fixed-second", "work", "Travel B", 0)
        conn = self.server.get_db_connection()
        rows = [dict(row) for row in conn.execute("SELECT * FROM incoming_submissions ORDER BY id")]
        conn.close()
        for row in rows:
            row["entry_type"] = row["submission_type"]
        fixed = create_fixed({"journal_rows": rows, "company": "Renatur", "sync_id": "object-test",
                              "date_from": rows[0]["date"], "date_to": rows[0]["date"], "fast_pris_amount": 3000})
        row = rows[0]
        payload = {key: row[key] for key in ("date", "employee", "company", "object_name", "hours",
                   "rate", "transport", "comment", "start_time", "end_time")}
        payload.update(record_type=row["entry_type"], invoiced=1, customer_pricing=fixed_quote(row, fixed))
        response = self.server_client.post("/sync/push", json={"changes": [{
            "sync_id": row["sync_id"], "operation_id": "partial-fixed", "operation": "upsert",
            "base_version": row["version"], "shift": payload,
        }]}, headers={"X-Sync-Key": "isolated-sync-test-key"})
        self.assertEqual(response.status_code, 400, response.get_json())
        conn = self.server.get_db_connection()
        self.assertEqual(conn.execute("SELECT SUM(invoiced) FROM incoming_submissions").fetchone()[0], 0)
        conn.close()

    def test_whole_package_sync_and_partial_reopen_rollback(self):
        self.submit("package-ob", "ob", "Travel B", 1)
        self.submit("package-work", "work", "Travel B", 0)
        conn = self.server.get_db_connection()
        rows = [dict(row) for row in conn.execute("SELECT * FROM incoming_submissions ORDER BY id")]
        conn.close()
        for row in rows:
            row["entry_type"] = row["submission_type"]
        fixed = create_fixed({"journal_rows": rows, "company": "Renatur", "sync_id": "object-test",
                              "date_from": rows[0]["date"], "date_to": rows[0]["date"], "fast_pris_amount": 4000})
        changes = []
        for row in rows:
            payload = {key: row[key] for key in ("date", "employee", "company", "object_name", "hours",
                       "rate", "transport", "comment", "start_time", "end_time")}
            payload.update(record_type=row["entry_type"], invoiced=1, customer_pricing=fixed_quote(row, fixed))
            changes.append({"sync_id": row["sync_id"], "operation_id": "close-" + row["sync_id"],
                            "operation": "upsert", "base_version": row["version"], "shift": payload})
        headers = {"X-Sync-Key": "isolated-sync-test-key"}
        response = self.server_client.post("/sync/push", json={"changes": changes}, headers=headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        for change in changes:
            change["base_version"] += 1
            change["operation_id"] = "reopen-" + change["sync_id"]
            change["shift"]["invoiced"] = 0
            change["shift"]["customer_pricing"].pop("fixed")
        response = self.server_client.post("/sync/push", json={"changes": changes[:1]}, headers=headers)
        self.assertEqual(response.status_code, 400, response.get_json())
        conn = self.server.get_db_connection()
        self.assertEqual(conn.execute("SELECT SUM(invoiced) FROM incoming_submissions").fetchone()[0], 2)
        conn.close()
        response = self.server_client.post("/sync/push", json={"changes": changes}, headers=headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        conn = self.server.get_db_connection()
        self.assertEqual(conn.execute("SELECT SUM(invoiced) FROM incoming_submissions").fetchone()[0], 0)
        conn.close()
