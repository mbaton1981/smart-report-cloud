import json
import unittest
from unittest.mock import patch

import test_customer_pricing as pricing
from customer_pricing import catalog_price_preview, row_prices


class CatalogPricesTests(unittest.TestCase):
    setUpClass = classmethod(pricing.HistoricalPricesTests.setUpClass.__func__)
    setUp = pricing.HistoricalPricesTests.setUp
    tearDown = pricing.HistoricalPricesTests.tearDown
    shift = pricing.HistoricalPricesTests.shift
    ob = pricing.HistoricalPricesTests.ob

    def row(self, identifier):
        return dict(self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (identifier,)).fetchone())

    def old_prices(self, identifier, *, source="Ручное изменение клиентской цены записи"):
        quote = json.loads(self.row(identifier)["customer_pricing"])
        quote.update(base=300, travel=388, source=source, ob={"weekday_early_evening": 123})
        self.conn.execute("UPDATE shifts SET customer_pricing=? WHERE sync_id=?", (json.dumps(quote), identifier))
        self.conn.commit()

    def preview_dialog(self, *, accepted=True, replace=True, select=None):
        def act(dialog):
            table = dialog.findChild(self.desktop.QTableWidget, "catalog_prices_table")
            self.assertIsNotNone(table)
            self.assertIn("ОБА поля", dialog.findChild(self.desktop.QLabel).text())
            if select is not None:
                for index in range(table.rowCount()):
                    table.item(index, 0).setCheckState(
                        self.desktop.Qt.CheckState.Checked if index in select else self.desktop.Qt.CheckState.Unchecked)
            return (self.desktop.QDialog.DialogCode.Accepted if accepted else
                    self.desktop.QDialog.DialogCode.Rejected)
        with patch.object(self.desktop.QDialog, "exec", act), \
                patch.object(self.desktop.QMessageBox, "question", return_value=(
                    self.desktop.QMessageBox.StandardButton.Yes if replace else
                    self.desktop.QMessageBox.StandardButton.No)) as question, \
                patch.object(self.desktop.QMessageBox, "information"), \
                patch.object(self.desktop.QMessageBox, "warning") as warning, \
                patch.object(self.window, "load_shifts_history"), \
                patch.object(self.window, "background_sync_with_cloud") as sync:
            self.window.preview_catalog_prices()
        warning.assert_not_called()
        return question, sync

    def test_both_fields_selected_only_audit_ob_payroll_and_balance_unchanged(self):
        selected = self.ob(transport=1)
        untouched = self.shift(day="2026-10-22", transport=1)
        for identifier in (selected, untouched):
            self.old_prices(identifier)
        before = self.row(selected)
        segments = list(map(tuple, self.conn.execute("SELECT * FROM ob_segments")))
        balance = list(map(tuple, self.conn.execute("SELECT * FROM balance")))
        report = self.window.collect_invoice_report()
        with self.assertRaisesRegex(ValueError, "подтверждения"):
            self.window._apply_catalog_prices(report, {selected})
        self.assertEqual(self.row(selected), before)
        question, sync = self.preview_dialog(select={0})
        question.assert_called_once()
        sync.assert_called_once()
        after = self.row(selected)
        self.assertEqual({k: v for k, v in after.items() if k != "customer_pricing"},
                         {k: v for k, v in before.items() if k != "customer_pricing"})
        prices = row_prices(after)
        self.assertEqual((prices["base"], prices["travel"]), (520, 450))
        self.assertEqual(prices["ob"], {"weekday_early_evening": 123})
        self.assertEqual(prices["date_from"], after["date"])
        self.assertEqual(prices["date_to"], after["date"])
        self.assertTrue(prices["confirmed"])
        self.assertEqual(row_prices(self.row(untouched))["travel"], 388)
        self.assertEqual(list(map(tuple, self.conn.execute("SELECT * FROM ob_segments"))), segments)
        self.assertEqual(list(map(tuple, self.conn.execute("SELECT * FROM balance"))), balance)
        audit = self.conn.execute("SELECT * FROM customer_price_audit").fetchall()
        self.assertEqual(len(audit), 1)
        self.assertEqual(json.loads(audit[0]["previous_json"])["travel"], 388)
        self.assertEqual(json.loads(audit[0]["current_json"])["travel"], 450)
        queued = json.loads(self.conn.execute("SELECT payload_json FROM shift_sync_outbox").fetchone()[0])
        self.assertEqual(json.loads(queued["customer_pricing"]), prices)
        result = self.window.generate_invoice()
        self.assertEqual(result["totals"]["work_amount"], 4 * 520 + 8 * 300)
        self.assertEqual(result["totals"]["transport_amount"], 450 + 388)
        self.assertEqual(result["totals"]["ob_amount"], 246)

    def test_cancel_preview_and_decline_manual_replacement_are_readonly(self):
        identifier = self.shift(transport=1)
        self.old_prices(identifier)
        before = list(self.conn.iterdump())
        question, sync = self.preview_dialog(accepted=False)
        question.assert_not_called()
        sync.assert_not_called()
        self.assertEqual(list(self.conn.iterdump()), before)
        question, sync = self.preview_dialog(replace=False)
        question.assert_called_once()
        sync.assert_not_called()
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_closed_and_fast_pris_protected(self):
        from fixed_pricing import fixed_quote
        closed = self.shift()
        fixed_id = self.shift(day="2026-10-22")
        self.old_prices(closed)
        self.old_prices(fixed_id)
        self.conn.execute("UPDATE shifts SET invoiced=1 WHERE sync_id=?", (closed,))
        self.conn.commit()
        row = self.row(fixed_id)
        # Use the production package builder for its exact signature/validation.
        from fixed_pricing import report_agreements
        report = self.window.collect_invoice_report()
        report.update(is_fast_pris=True, fast_pris_amount=1000,
                      journal_rows=[row], journal_segments={})
        _, allocations = report_agreements(report)
        quote = fixed_quote(row, allocations[fixed_id])
        self.conn.execute("UPDATE shifts SET customer_pricing=? WHERE sync_id=?", (json.dumps(quote), fixed_id))
        self.conn.commit()
        report = self.window.collect_invoice_report()
        before = list(self.conn.iterdump())
        proposals = catalog_price_preview(report["journal_rows"], 520, 450)
        self.assertFalse(any(item["eligible"] for item in proposals))
        for identifier in (closed, fixed_id):
            with self.assertRaisesRegex(ValueError, "защищен"):
                self.window._apply_catalog_prices(report, {identifier}, replace_confirmed=True)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_stale_row_and_catalog_rejected_atomically(self):
        first = self.shift()
        second = self.shift(day="2026-10-22")
        for identifier in (first, second):
            self.old_prices(identifier)
        report = self.window.collect_invoice_report()
        self.conn.execute("UPDATE shifts SET invoiced=1 WHERE sync_id=?", (second,))
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "изменились"):
            self.window._apply_catalog_prices(report, {first, second}, replace_confirmed=True)
        self.assertEqual(row_prices(self.row(first))["base"], 300)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM customer_price_audit").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shift_sync_outbox").fetchone()[0], 0)
        report = self.window.collect_invoice_report()
        self.conn.execute("UPDATE objects SET transport_rate=451 WHERE id=39")
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "справочника изменились"):
            self.window._apply_catalog_prices(report, {first}, replace_confirmed=True)
        self.assertEqual(row_prices(self.row(first))["travel"], 388)

    def test_noop_repeat_and_missing_prices_no_manual_confirmation(self):
        identifier = self.shift(confirmed=False, transport=1)
        self.window._apply_catalog_prices(self.window.collect_invoice_report(), {identifier})
        before = list(self.conn.iterdump())
        self.assertEqual(self.window._apply_catalog_prices(self.window.collect_invoice_report(), {identifier}), 0)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual(self.window.generate_invoice()["totals"]["invoice_total"], 8 * 520 + 450)

    def test_origin_unknown_legacy_and_explicit_zero_require_confirmation(self):
        for source in ("", "Ручной ввод клиентской цены", "Договор"):
            identifier = self.shift()
            self.old_prices(identifier, source=source or "Unknown agreement")
            proposal = catalog_price_preview([self.row(identifier)], 520, 450)[0]
            self.assertTrue(proposal["replace_confirmation"])
            self.assertIn("Ручная" if source.startswith("Ручн") else "происхождение", proposal["origin"])
        identifier = self.shift(rate=300, confirmed=False)
        self.assertTrue(catalog_price_preview([self.row(identifier)], 520, 450)[0]["replace_confirmation"])
        identifier = self.shift()
        quote = json.loads(self.row(identifier)["customer_pricing"])
        quote.update(base=0, travel=0)
        self.conn.execute("UPDATE shifts SET customer_pricing=? WHERE sync_id=?", (json.dumps(quote), identifier))
        self.conn.commit()
        self.assertTrue(catalog_price_preview([self.row(identifier)], 520, 450)[0]["replace_confirmation"])


class CatalogPricesSyncTests(unittest.TestCase):
    setUpClass = classmethod(pricing.PricingSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(pricing.PricingSyncTests.tearDownClass.__func__)
    setUp = pricing.PricingSyncTests.setUp
    _add_company = pricing.PricingSyncTests._add_company
    submit = pricing.PricingSyncTests.submit

    def test_updated_base_travel_and_unchanged_payroll_roundtrip(self):
        from customer_pricing import CATALOG_PRICE_SOURCE
        self.submit("catalog-price-sync", "ob", "Travel B", 1)
        conn = self.server.get_db_connection()
        before = dict(conn.execute("SELECT * FROM incoming_submissions WHERE sync_id='catalog-price-sync'").fetchone())
        audit_before = conn.execute("SELECT COUNT(*) FROM customer_price_audit").fetchone()[0]
        segments = list(map(tuple, conn.execute("SELECT * FROM ob_segments")))
        conn.close()
        quote = {
            "version": 1, "company": before["company"], "object_name": before["object_name"],
            "date_from": before["date"], "date_to": before["date"], "source": CATALOG_PRICE_SOURCE,
            "base": 520, "travel": 450, "ob": {}, "confirmed": True,
            "confirmed_at": "2026-10-23T14:00:00+02:00",
        }
        payload = {key: before[key] for key in ("date", "employee", "company", "object_name", "hours", "rate",
                   "transport", "comment", "start_time", "end_time", "invoiced")}
        payload.update(record_type="ob", customer_pricing=quote)
        request = {"changes": [{"sync_id": before["sync_id"], "operation_id": "apply-catalog",
                               "operation": "upsert", "base_version": before["version"], "shift": payload}]}
        for _ in range(2):
            response = self.server_client.post("/sync/push", json=request,
                                               headers={"X-Sync-Key": "isolated-sync-test-key"})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertTrue(response.get_json()["results"][0]["ok"])
        conn = self.server.get_db_connection()
        try:
            after = dict(conn.execute("SELECT * FROM incoming_submissions WHERE sync_id=?", (before["sync_id"],)).fetchone())
            self.assertEqual(json.loads(after["customer_pricing"]), quote)
            for key in ("hours", "rate", "transport", "invoiced", "ob_base_rate"):
                self.assertEqual(after[key], before[key], key)
            self.assertEqual(list(map(tuple, conn.execute("SELECT * FROM ob_segments"))), segments)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM customer_price_audit").fetchone()[0], audit_before + 1)
            self.assertEqual(after["version"], before["version"] + 1)
            remote = self.server.serialize_submission(after, cursor=conn.cursor())
        finally:
            conn.close()
        from types import SimpleNamespace
        local = self.desktop.get_db_connection()
        try:
            self.desktop.ensure_ob_schema(local.cursor())
            self.desktop.SmartReportApp.apply_remote_shift(SimpleNamespace(), local.cursor(), remote)
            row = local.execute("SELECT * FROM shifts WHERE sync_id=?", (before["sync_id"],)).fetchone()
            self.assertEqual(json.loads(row["customer_pricing"]), quote)
            self.assertEqual((row["invoiced"], row["transport"]), (0, 1))
        finally:
            local.close()


if __name__ == "__main__":
    unittest.main()
