from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QPushButton

import customer_supplements as supplements
from customer_supplements_ui import SupplementDialog, calculate_supplements
from customer_pricing import parse_pricing, row_prices, store_pricing, require_final_prices, new_shift_pricing, fill_missing_prices
import test_customer_intervals as fixture
import test_customer_report as reports


STAMP = "2026-11-11T01:00:00+01:00"
QUANTITIES = {"16-17": 1, "17-19": 2, "19-22": 1}


def quote(row, base=520, scheme="ob", quantities=None, weekend=False):
    previous = row_prices(row)
    previous["base"] = base
    return supplements.confirmed_quote(row, previous, scheme, quantities or QUANTITIES,
                                       weekend=weekend, confirmed_at=STAMP)


class SupplementCalculationTests(unittest.TestCase):
    def row(self, **values):
        return dict(sync_id="row", date="2026-10-21", company="Renatur", object_name="Tomteboda ÄTA",
                    employee="Test employee", hours=4, entry_type="ob", rate=0, invoiced=0, **values)

    def test_control_example_exact_amounts_and_no_fake_time(self):
        row = self.row()
        result = quote(row)
        value = parse_pricing(result, row)["supplement_allocation"]
        self.assertEqual(sum(Decimal(p["base_amount"]) for p in value["parts"]), 2080)
        self.assertEqual(sum(Decimal(p["premium_amount"]) for p in value["parts"]), 416)
        self.assertEqual(sum(Decimal(p["total"]) for p in value["parts"]), 2496)
        self.assertNotIn("time_facts", row)
        self.assertNotIn("schedule_application", result)
        require_final_prices(dict(row, customer_pricing=json.dumps(result)), [])

    def test_ordinary_overtime_weekend_distinct_bases_and_missing_intervals(self):
        row = self.row()
        for base, scheme, quantities, total in (
            (520, "ob", {"ordinary": 4}, 2080),
            (520, "overtime", {"17-19": 2, "19-22": 2}, 3328),
            (300, "ob", QUANTITIES, 1440),
            (520, "ob", {"22-06": 4}, 3536),
        ):
            value = quote(row, base, scheme, quantities)["supplement_allocation"]
            self.assertEqual(sum(Decimal(p["total"]) for p in value["parts"]), total)
        weekend = dict(row, date="2026-10-24")
        for scheme in supplements.SCHEMES:
            value = quote(weekend, scheme=scheme, quantities={"weekend": 4}, weekend=True)["supplement_allocation"]
            self.assertEqual(sum(Decimal(p["total"]) for p in value["parts"]), 4160)
        for quantities, scheme, weekend_flag in (({"16-17": 4}, "overtime", False),
                                                  ({"weekend": 4}, "ob", False),
                                                  ({"ordinary": 1, "weekend": 3}, "ob", True),
                                                  ({"17-19": 3}, "ob", False)):
            with self.subTest(quantities=quantities), self.assertRaises(ValueError):
                quote(weekend, scheme=scheme, quantities=quantities, weekend=weekend_flag)
        with self.assertRaises(ValueError):
            quote(row, quantities={"weekend": 4}, weekend=True)
        with self.assertRaises(ValueError):
            quote(row, base=None)

    def test_saved_snapshot_tampering_hours_prices_identity_and_closed_guard(self):
        row = self.row()
        saved = quote(row)
        for field in ("percent", "hours", "base_amount", "premium_amount", "total"):
            altered = deepcopy(saved)
            altered["supplement_allocation"]["parts"][1][field] = "999"
            with self.subTest(field=field), self.assertRaises(ValueError):
                parse_pricing(altered, row)
        for change in (dict(hours=5), dict(employee="Another"), dict(sync_id="other"),
                       dict(time_facts=fixture.facts())):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_pricing(saved, dict(row, **change))
        with self.assertRaises(ValueError):
            quote(dict(row, invoiced=1))
        with self.assertRaises(ValueError):
            parse_pricing(dict(saved, base=600), row)
        stored = dict(row, customer_pricing=json.dumps(saved))
        fill = dict(saved, travel=450)
        fill.pop("supplement_allocation")
        merged, _ = fill_missing_prices(stored, fill)
        self.assertEqual(merged["supplement_allocation"], saved["supplement_allocation"])
        self.assertEqual(merged["travel"], 450)

    def test_actual_minute_proposal_breaks_midnight_modes_and_missing_not_invented(self):
        row = self.row()
        with self.assertRaises(ValueError):
            supplements.proposed_hours(row, "ob")
        row["time_facts"] = fixture.facts()
        quantities = supplements.proposed_hours(row, "ob")
        self.assertEqual([Decimal(quantities[key]) for key in ("16-17", "17-19", "19-22")], [1, 2, 1])
        row.update(hours=3.5, time_facts=dict(fixture.facts(), breaks=[dict(start="17:00", end="17:30")]))
        self.assertEqual(Decimal(supplements.proposed_hours(row, "ob")["17-19"]), Decimal("1.5"))
        with self.assertRaises(ValueError):
            supplements.proposed_hours(row, "overtime")
        row.update(hours=8, time_facts=dict(fixture.facts("22:00", "06:00"), next_day=True,
                                          overtime=[dict(start="22:00", end="06:00")]))
        self.assertEqual(Decimal(supplements.proposed_hours(row, "overtime")["22-06"]), 8)
        row.update(hours=4, time_facts=fixture.facts("06:00", "10:00"))
        with self.assertRaises(ValueError):
            supplements.proposed_hours(row, "ob")


class SupplementDesktopTests(unittest.TestCase):
    setUpClass = classmethod(fixture.IntervalDesktopTests.setUpClass.__func__)
    tearDownClass = fixture.IntervalDesktopTests.__dict__["tearDownClass"]
    setUp = fixture.IntervalDesktopTests.setUp
    tearDown = fixture.IntervalDesktopTests.tearDown
    shift = fixture.IntervalDesktopTests.shift
    ob = fixture.IntervalDesktopTests.ob

    def dialog(self):
        dialog = SupplementDialog(self.window.collect_invoice_report(), self.window)
        return dialog

    def stage(self, dialog, indices, quantities=QUANTITIES, scheme="ob"):
        dialog.scheme.setCurrentIndex(dialog.scheme.findData(scheme))
        for key, field in dialog.fields.items():
            field.setText(str(quantities.get(key, 0)))
        for index in indices:
            dialog.table.item(index, 0).setCheckState(Qt.CheckState.Checked)
        dialog.preview()

    def test_visible_action_group_preview_different_bases_and_mixed_period_close_pdf(self):
        identifiers = [self.ob(transport=0), self.shift(hours=4, rate=300), self.shift(hours=3)]
        before = [tuple(r) for r in self.conn.execute(
            "SELECT id,hours,entry_type,rate,transport,time_facts,invoiced FROM shifts ORDER BY id")]
        salary = [tuple(r) for r in self.conn.execute("SELECT * FROM ob_segments")]
        self.assertFalse(any(button.text() == "Рассчитать доплаты"
                             for button in self.window.tab_invoice.findChildren(QPushButton)))
        self.assertTrue(any(
            action.text() == "Клиентские доплаты — отдельный отложенный разбор…"
            for tool_button in self.window.tab_invoice.findChildren(self.desktop.QToolButton)
            if tool_button.menu() is not None
            for action in tool_button.menu().actions()
        ))
        def accept(dialog):
            self.assertFalse(dialog.drafts)
            for i in range(3):
                self.assertEqual(dialog.table.item(i, 0).checkState(), Qt.CheckState.Unchecked)
            self.stage(dialog, [0, 1])
            self.assertEqual(len(dialog.drafts), 2)
            self.assertIn("2496", dialog.table.item(0, 7).text())
            self.assertIn("1440", dialog.table.item(1, 7).text())
            self.stage(dialog, [2], {"ordinary": 3})
            self.assertEqual(len(dialog.drafts), 3)
            return self.desktop.QDialog.DialogCode.Accepted
        with patch.object(SupplementDialog, "exec", accept), \
                patch.object(self.desktop, "request_expense_api", return_value={"customer_supplements": 1}), \
                patch.object(self.window, "background_sync_with_cloud") as sync, \
                patch("customer_supplements_ui.QMessageBox.warning", side_effect=AssertionError):
            calculate_supplements(self.window)
        sync.assert_called_once()
        self.assertEqual(before, [tuple(r) for r in self.conn.execute(
            "SELECT id,hours,entry_type,rate,transport,time_facts,invoiced FROM shifts ORDER BY id")])
        self.assertEqual(salary, [tuple(r) for r in self.conn.execute("SELECT * FROM ob_segments")])
        reopened = self.dialog()
        self.assertTrue(row_prices(reopened.rows[0]).get("supplement_allocation"))
        report = self.window.generate_invoice()
        self.assertEqual(report["totals"]["hours"], 11)
        self.assertEqual(report["totals"]["invoice_total"], 2496 + 1440 + 1560)
        text = "\n".join(p.extract_text() for p in reports.CustomerReportTests.pdf(self, report).pages)
        self.assertIn("Bekräftad kundfördelning", text)
        self.assertIn("17:00", text)
        self.window._set_period_invoiced(self.window.collect_invoice_report(), True)
        saved = [r[0] for r in self.conn.execute("SELECT customer_pricing FROM shifts ORDER BY id")]
        self.conn.execute("UPDATE objects SET rate=999")
        self.conn.commit()
        self.assertEqual(self.window.generate_invoice()["totals"]["invoice_total"], 5496)
        closed = self.dialog()
        self.assertFalse(closed.table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEnabled)
        self.window._set_period_invoiced(self.window.collect_invoice_report(), False)
        self.assertEqual(saved, [r[0] for r in self.conn.execute("SELECT customer_pricing FROM shifts ORDER BY id")])

    def test_differing_duration_group_is_atomic_and_ordinary_remainder_explicit(self):
        self.shift(hours=4)
        self.shift(hours=3)
        dialog = self.dialog()
        self.stage(dialog, [0, 1])
        self.assertFalse(dialog.drafts)
        self.assertIn("Группа не применена", dialog.status.text())
        self.assertIn("остаток", dialog.status.text())
        self.assertTrue(all(not row_prices(row).get("supplement_allocation") for row in dialog.rows))

    def test_explicit_customer_allocation_known_even_when_legacy_ob_breakdown_unknown(self):
        identifier = self.ob(transport=0)
        self.conn.execute("DELETE FROM ob_segments WHERE shift_id=?", (identifier,))
        self.conn.commit()
        dialog = self.dialog()
        self.stage(dialog, [0])
        self.assertIn(0, dialog.drafts, dialog.status.text())
        store_pricing(self.conn.cursor(), "shifts", identifier, dialog.drafts[0])
        self.conn.commit()
        report = self.window.generate_invoice()
        self.assertIsNone(report["totals"]["without_ob_hours"])
        self.assertEqual(report["totals"]["ob_amount"], 416)
        self.assertEqual(report["totals"]["invoice_total"], 2496)

    def test_saved_scheme_per_record_overtime_and_weekend_not_period_wide(self):
        self.shift(hours=4)
        self.shift(hours=4, day="2026-10-22")
        self.shift(hours=4, day="2026-10-24")
        dialog = self.dialog()
        self.stage(dialog, [0])
        self.stage(dialog, [1], {"17-19": 2, "19-22": 2}, "overtime")
        dialog.weekend.setChecked(True)
        self.stage(dialog, [2], {"weekend": 4}, "overtime")
        self.assertEqual(len(dialog.drafts), 3)
        for index, expected in enumerate((2496, 3328, 4160)):
            self.assertIn(str(expected), dialog.table.item(index, 7).text())
        self.assertEqual(dialog.rows[0]["hours"], 4)

    def test_exact_facts_proposal_dirty_inputs_and_stale_group_never_saves(self):
        identifier = self.shift(hours=4)
        self.conn.execute("UPDATE shifts SET time_facts=? WHERE sync_id=?",
                          (json.dumps(fixture.facts()), identifier))
        self.conn.commit()
        dialog = self.dialog()
        dialog.scheme.setCurrentIndex(dialog.scheme.findData("ob"))
        dialog.suggest()
        self.assertEqual(Decimal(dialog.fields["17-19"].text()), 2)
        self.assertFalse(dialog.drafts)
        self.stage(dialog, [0])
        dialog.fields["17-19"].setText("1")
        dialog.confirm()
        self.assertIn("без предпросмотра", dialog.status.text())
        before = self.conn.execute("SELECT customer_pricing FROM shifts").fetchone()[0]
        def stale(dialog):
            self.stage(dialog, [0])
            self.conn.execute("UPDATE shifts SET comment='Changed during preview'")
            self.conn.commit()
            return self.desktop.QDialog.DialogCode.Accepted
        with patch.object(SupplementDialog, "exec", stale), \
                patch.object(self.desktop, "request_expense_api", return_value={"customer_supplements": 1}), \
                patch.object(self.window, "background_sync_with_cloud") as sync, \
                patch("customer_supplements_ui.QMessageBox.warning") as warning:
            calculate_supplements(self.window)
        self.assertIn("изменилась", warning.call_args[0][2])
        sync.assert_not_called()
        self.assertEqual(before, self.conn.execute("SELECT customer_pricing FROM shifts").fetchone()[0])


class SupplementServerTests(unittest.TestCase):
    setUpClass = classmethod(fixture.IntervalServerTests.setUpClass.__func__)
    tearDownClass = fixture.IntervalServerTests.__dict__["tearDownClass"]
    setUp = fixture.IntervalServerTests.setUp
    tearDown = fixture.IntervalServerTests.tearDown
    _add_company = fixture.IntervalServerTests._add_company

    def test_simple_web_confirm_sync_close_reopen_edit_and_no_inherited_allocation(self):
        headers = {"X-Sync-Key": "isolated-sync-test-key"}
        with closing(self.server.get_db_connection()) as db, db:
            db.execute("UPDATE meta_objects SET rate=520 WHERE name='Travel A'")
        data = dict(request_id="simple-allocation", employee="Travel Test", company="Renatur",
                    object_name="Travel A", date="2026-10-21", hours=4, transport=0, submission_type="work")
        response = self.server_client.post("/submit-shift", json=data)
        self.assertEqual(response.status_code, 201, response.get_json())
        duplicate = self.server_client.post("/submit-shift", json=data)
        self.assertTrue(duplicate.get_json()["idempotent"])
        with closing(self.server.get_db_connection()) as db:
            row = dict(db.execute("SELECT * FROM incoming_submissions WHERE sync_id='simple-allocation'").fetchone())
        result = quote(row)
        payload = {key: row.get(key) for key in ("date", "employee", "company", "object_name", "hours",
                                                 "rate", "transport", "comment", "submission_type")}
        payload.update(customer_pricing=json.dumps(result), invoiced=1)
        response = self.server_client.post("/sync/push", headers=headers, json={"changes": [dict(
            sync_id=row["sync_id"], operation_id="confirm-and-close", operation="upsert",
            base_version=row["version"], shift=payload)]})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["results"][0]["ok"], response.get_json())
        with closing(self.server.get_db_connection()) as db:
            saved = dict(db.execute("SELECT * FROM incoming_submissions WHERE sync_id=?", (row["sync_id"],)).fetchone())
            self.assertEqual(saved["invoiced"], 1)
            self.assertEqual(saved["hours"], 4)
            self.assertIsNone(saved["time_facts"])
            self.assertEqual(json.loads(saved["customer_pricing"])["supplement_allocation"],
                             result["supplement_allocation"])
        with patch.object(self.server, "bearer_admin_user", return_value={"user_id": 1, "role": "admin"}):
            reopened = self.server_client.post("/api/admin/shifts/reopen", json=dict(
                sync_id=row["sync_id"], operation_id="reopen-allocated", version=saved["version"]))
        self.assertEqual(reopened.status_code, 200, reopened.get_json())
        changed = self.server_client.post("/edit-shift", json=dict(
            sync_id=row["sync_id"], operation_id="comment-after-open", comment="No price loss"))
        self.assertEqual(changed.status_code, 200, changed.get_json())
        with closing(self.server.get_db_connection()) as db:
            current = dict(db.execute("SELECT * FROM incoming_submissions WHERE sync_id=?", (row["sync_id"],)).fetchone())
            self.assertIn("supplement_allocation", json.loads(current["customer_pricing"]))
            new = dict(current, sync_id="new-row")
            fresh = new_shift_pricing(db.cursor(), "incoming_submissions", new)
            self.assertNotIn("supplement_allocation", fresh)
        stripped = json.loads(current["customer_pricing"])
        stripped.pop("supplement_allocation")
        old_client_payload = dict(payload, invoiced=0, customer_pricing=json.dumps(stripped))
        lost = self.server_client.post("/sync/push", headers=headers, json={"changes": [dict(
            sync_id=row["sync_id"], operation_id="old-client-price-loss", operation="upsert",
            base_version=current["version"], shift=old_client_payload)]})
        self.assertEqual(lost.status_code, 400, lost.get_json())
        changed = self.server_client.post("/edit-shift", json=dict(
            sync_id=row["sync_id"], operation_id="correct-actual-hours", hours=3))
        self.assertEqual(changed.status_code, 200, changed.get_json())
        with closing(self.server.get_db_connection()) as db:
            current = dict(db.execute("SELECT * FROM incoming_submissions WHERE sync_id=?", (row["sync_id"],)).fetchone())
            self.assertEqual(current["hours"], 3)
            self.assertNotIn("supplement_allocation", json.loads(current["customer_pricing"]))
