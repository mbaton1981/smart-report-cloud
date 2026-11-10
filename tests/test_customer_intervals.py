import json
import unittest
from decimal import Decimal
from unittest.mock import patch
from contextlib import closing

import customer_intervals as intervals
from customer_pricing import row_prices, store_pricing
import test_customer_pricing as historical
import test_ob_transport as transport


def example_terms():
    rules = []
    for mode, percentages in (("normal", (0, 20, 40, 70)), ("overtime", (None, 50, 70, 100))):
        for (start, end), percent in zip((("06:00", "17:00"), ("17:00", "19:00"),
                                        ("19:00", "22:00"), ("22:00", "06:00")), percentages):
            rules.append(dict(day="weekday", mode=mode, start=start, end=end, percent=percent))
        for day in ("saturday", "sunday"):
            rules.append(dict(day=day, mode=mode, start="00:00", end="00:00", percent=100))
    return dict(version=1, effective_from="2026-10-01", holidays=[], rules=rules)


def facts(start="16:00", end="20:00", **extra):
    return dict(version=1, start=start, end=end, next_day=False, breaks=[], overtime=[], **extra)


def scheduled_terms():
    return dict(example_terms(), schedule={"days": [0, 1, 2, 3, 4], "time": facts()})


class IntervalCalculationTests(unittest.TestCase):
    def total(self, value=None, day="2026-10-21", base=520, conditions=None):
        parts = intervals.calculate(value or facts(), day, base, conditions or example_terms())
        self.assertTrue(all(part["total"] is not None for part in parts), parts)
        return sum(Decimal(part["total"]) for part in parts), parts

    def test_exact_customer_example_and_distinct_base(self):
        total, parts = self.total()
        self.assertEqual(total, Decimal("2496.00"))
        self.assertEqual([p["hours"] for p in parts], ["1", "2", "1"])
        self.assertEqual([p["percent"] for p in parts], ["0", "20", "40"])
        self.assertEqual(self.total(base=260)[0], Decimal("1248.00"))

    def test_overtime_explicit_not_evening_and_mixed_no_double_hours(self):
        value = facts()
        value["overtime"] = [dict(start="17:00", end="20:00")]
        total, parts = self.total(value)
        self.assertEqual(total, Decimal("2964.00"))
        self.assertEqual(sum(Decimal(p["hours"]) for p in parts), 4)
        self.assertEqual([p["percent"] for p in parts], ["0", "50", "70"])

    def test_break_position_changes_correct_segment(self):
        first = facts()
        first["breaks"] = [dict(start="16:00", end="16:30")]
        second = facts()
        second["breaks"] = [dict(start="19:00", end="19:30")]
        self.assertEqual(self.total(first)[0], Decimal("2236"))
        self.assertEqual(self.total(second)[0], Decimal("2132"))
        self.assertEqual(intervals.paid_hours(first, "2026-10-21"), 3.5)

    def test_midnight_and_weekend_priority(self):
        value = facts("22:00", "02:00")
        value["next_day"] = True
        total, parts = self.total(value, day="2026-10-23")
        self.assertEqual(total, Decimal("3848"))
        self.assertEqual([p["day"] for p in parts], ["weekday", "saturday"])
        value["overtime"] = [dict(start="22:00", end="02:00")]
        self.assertEqual(self.total(value, day="2026-10-23")[0], Decimal("4160"))

    def test_holiday_explicit_and_uncovered_not_zero(self):
        conditions = example_terms()
        conditions["holidays"] = ["2026-10-21"]
        parts = intervals.calculate(facts(), "2026-10-21", 520, conditions)
        self.assertIsNone(parts[0]["total"])
        conditions["rules"].append(dict(day="holiday", mode="normal", start="00:00", end="00:00", percent=0))
        self.assertEqual(self.total(conditions=conditions)[0], Decimal("2080"))
        conditions["rules"] = []
        self.assertIsNone(intervals.calculate(facts(), "2026-10-21", 520, conditions)[0]["total"])

    def test_strict_boundaries_validation_and_legacy_no_invention(self):
        for value in (facts("20:00", "16:00"), facts("16:00", "16:00"),
                      dict(facts(), breaks=[dict(start="15:30", end="16:30")]),
                      dict(facts(), breaks=[dict(start="17:00", end="18:00"), dict(start="17:30", end="19:00")])):
            with self.subTest(value=value), self.assertRaises(ValueError):
                intervals.time_facts(value, "2026-10-21")
        self.assertIsNone(intervals.time_facts(None, "2026-10-21"))
        with self.assertRaises(ValueError):
            intervals.calculate(None, "2026-10-21", 520, example_terms())
        with self.assertRaises(ValueError):
            intervals.time_facts(facts(), "2026-10-21", 8)
        overlap = example_terms()
        overlap["rules"].append(dict(day="weekday", mode="normal", start="17:00", end="18:00", percent=10))
        with self.assertRaises(ValueError):
            intervals.terms(overlap)

    def test_next_day_break_and_overtime_position(self):
        value = facts("22:00", "06:00")
        value.update(next_day=True, breaks=[dict(start="00:30", end="01:00")],
                     overtime=[dict(start="01:00", end="03:00")])
        self.assertEqual(intervals.paid_hours(value, "2026-10-21"), 7.5)
        total, parts = self.total(value)
        self.assertEqual(sum(Decimal(p["hours"]) for p in parts), Decimal("7.5"))
        self.assertEqual(total, Decimal("6942"))

    def test_minute_precision_and_dst_ambiguous_endpoint(self):
        self.assertEqual(intervals.paid_hours(facts("16:00", "16:15"), "2026-10-21"), .25)
        with self.assertRaisesRegex(ValueError, "Переход"):
            intervals.time_facts(facts("02:00", "03:00"), "2026-10-25")

    def test_schedule_is_explicit_row_scoped_and_never_factual_or_automatic_overtime(self):
        row = dict(sync_id="schedule-row", date="2026-10-21", hours=4, entry_type="work",
                   invoiced=0, time_facts=None)
        quote = dict(base=520, interval_terms={"id": "test-version", "revision": 1,
                                              "conditions": scheduled_terms()})
        with self.assertRaises(ValueError):
            intervals.calculated_row(row, quote)
        applied = intervals.apply_schedule(row, quote)
        self.assertNotIn("schedule_application", quote)
        self.assertIsNone(row["time_facts"])
        parts = intervals.calculated_row(row, applied)
        self.assertEqual(sum(Decimal(p["total"]) for p in parts), 2496)
        self.assertEqual({p["mode"] for p in parts}, {"normal"})
        for change in (dict(hours=3), dict(date="2026-10-22"), dict(sync_id="different")):
            with self.subTest(change=change), self.assertRaises(ValueError):
                intervals.calculated_row(dict(row, **change), applied)
        for change in (dict(hours=3), dict(date="2026-10-24"), dict(entry_type="ob"),
                       dict(time_facts=facts()), dict(invoiced=1), dict(date="2026-09-30")):
            with self.subTest(change=change), self.assertRaises(ValueError):
                intervals.apply_schedule(dict(row, **change), quote)
        snapshot = dict(applied, interval_calculation=parts)
        self.assertEqual(intervals.calculated_row(row, snapshot), parts)
        for schedule in ({"days": [], "time": facts()}, {"days": [True], "time": facts()},
                         {"days": [1], "time": None}):
            with self.assertRaises(ValueError):
                intervals.terms(dict(example_terms(), schedule=schedule))

    def test_schedule_break_midnight_and_explicit_overtime_rules(self):
        value = facts("22:00", "06:00")
        value.update(next_day=True, breaks=[dict(start="00:30", end="01:00")],
                     overtime=[dict(start="01:00", end="03:00")])
        quote = dict(base=520, interval_terms={"conditions": dict(
            example_terms(), schedule={"days": [2], "time": value})})
        row = dict(sync_id="night", date="2026-10-21", hours=7.5, entry_type="work", invoiced=0)
        parts = intervals.calculated_row(row, intervals.apply_schedule(row, quote))
        self.assertEqual(sum(Decimal(p["total"]) for p in parts), 6942)
        self.assertEqual(sum(Decimal(p["hours"]) for p in parts), Decimal("7.5"))


class IntervalDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        transport.OBTransportTests.setUpClass.__func__(cls)
        historical.HistoricalPricesTests.setUpClass.__func__(cls)
    tearDownClass = classmethod(transport.OBTransportTests.tearDownClass.__func__)
    setUp = historical.HistoricalPricesTests.setUp
    def tearDown(self):
        from PyQt6.QtCore import QCoreApplication, QEvent
        historical.HistoricalPricesTests.tearDown(self)
        QCoreApplication.sendPostedEvents(self.window, QEvent.Type.DeferredDelete)
    shift = historical.HistoricalPricesTests.shift
    ob = historical.HistoricalPricesTests.ob

    def prepared(self, kind="work"):
        identifier = self.ob(transport=0) if kind == "ob" else self.shift(hours=4)
        row = dict(self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (identifier,)).fetchone())
        intervals.save_terms(self.conn.cursor(), row["company"], row["object_name"], example_terms(), 0)
        quote = intervals.attach_terms(self.conn.cursor(), row, row_prices(row))
        intervals.store_time(self.conn.cursor(), "shifts", identifier, facts())
        store_pricing(self.conn.cursor(), "shifts", identifier, quote)
        self.conn.commit()
        return identifier

    def test_opening_objects_tab_loads_catalog_for_customer_terms(self):
        with patch.object(self.window, "load_dropdowns") as dropdowns, \
                patch.object(self.window, "load_objects_table") as catalog:
            self.window.on_tab_changed(5)
        dropdowns.assert_called_once_with(refresh_tables=False)
        catalog.assert_called_once_with()

    def test_schedule_preview_selected_only_preserves_hours_ob_and_closed_history(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QTableWidget
        from test_customer_report import CustomerReportTests
        identifier = self.shift(hours=4)
        mismatch = self.shift(hours=3, day="2026-10-22")
        self.conn.execute("UPDATE shifts SET transport=0")
        before = [tuple(r) for r in self.conn.execute(
            "SELECT id,date,hours,entry_type,transport,rate,invoiced,time_facts FROM shifts ORDER BY id")]
        salary_before = [tuple(r) for r in self.conn.execute("SELECT * FROM ob_segments")]
        obj = self.conn.execute("SELECT company,object_name FROM shifts WHERE sync_id=?", (identifier,)).fetchone()
        intervals.save_terms(self.conn.cursor(), obj["company"], obj["object_name"], scheduled_terms(), 0)
        self.conn.commit()
        self.window._customer_schedules_supported = True
        def select(dialog):
            table = dialog.findChild(QTableWidget, "customerTermsPreview")
            self.assertEqual(table.rowCount(), 2)
            for i in range(2):
                if f"№{self.conn.execute('SELECT id FROM shifts WHERE sync_id=?', (identifier,)).fetchone()[0]} " in table.item(i, 1).text():
                    self.assertIn("Типовой график", table.item(i, 3).text())
                    self.assertIn("2496.00", table.item(i, 4).text())
                    self.assertEqual(table.item(i, 0).checkState(), Qt.CheckState.Unchecked)
                    table.item(i, 0).setCheckState(Qt.CheckState.Checked)
                else:
                    self.assertFalse(table.item(i, 0).flags() & Qt.ItemFlag.ItemIsEnabled)
                    self.assertIn("отличается", table.item(i, 4).text())
            return self.desktop.QDialog.DialogCode.Accepted
        with patch.object(self.window, "_terms_versions"), \
                patch.object(self.desktop.QDialog, "exec", select), \
                patch.object(self.window, "background_sync_with_cloud") as sync, \
                patch.object(self.desktop.QMessageBox, "warning", side_effect=AssertionError):
            self.window.preview_customer_terms()
        sync.assert_called_once()
        row = dict(self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (identifier,)).fetchone())
        self.assertIn("schedule_application", row_prices(row))
        other = dict(self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (mismatch,)).fetchone())
        self.assertNotIn("schedule_application", row_prices(other))
        self.assertEqual(before, [tuple(r) for r in self.conn.execute(
            "SELECT id,date,hours,entry_type,transport,rate,invoiced,time_facts FROM shifts ORDER BY id")])
        self.assertEqual(salary_before, [tuple(r) for r in self.conn.execute("SELECT * FROM ob_segments")])
        self.conn.execute("DELETE FROM shifts WHERE sync_id=?", (mismatch,))
        self.conn.commit()
        report = self.window.generate_invoice()
        self.assertEqual(report["totals"]["invoice_total"], 2496)
        self.assertEqual({p["time_source"] for p in report["interval_details"]}, {"schedule"})
        pdf = CustomerReportTests.pdf(self, report)
        self.assertIn("schema", "\n".join(p.extract_text() for p in pdf.pages))
        self.window._set_period_invoiced(self.window.collect_invoice_report(), True)
        changed = scheduled_terms()
        changed["rules"][1]["percent"] = 80
        intervals.save_terms(self.conn.cursor(), obj["company"], obj["object_name"], changed, 1)
        self.conn.commit()
        self.assertEqual(self.window.generate_invoice()["totals"]["invoice_total"], 2496)
        self.window._set_period_invoiced(self.window.collect_invoice_report(), False)
        self.assertEqual(self.window.generate_invoice()["totals"]["invoice_total"], 2496)
        row_id = self.conn.execute("SELECT id FROM shifts WHERE sync_id=?", (identifier,)).fetchone()[0]
        with patch.object(self.desktop.QDialog, "exec", return_value=self.desktop.QDialog.DialogCode.Accepted), \
                patch("customer_intervals_desktop.TimeFactsEditor.value", return_value=facts()), \
                patch.object(self.window, "background_sync_with_cloud"), \
                patch.object(self.desktop.QMessageBox, "warning", side_effect=AssertionError):
            self.window.edit_time_facts(row_id)
        updated = dict(self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (identifier,)).fetchone())
        self.assertNotIn("schedule_application", row_prices(updated))
        self.assertNotIn("interval_calculation", row_prices(updated))
        self.assertEqual(json.loads(updated["time_facts"]), facts())
        self.assertEqual(updated["hours"], 4)

    def test_terms_dialog_schedule_save_reopen_copy_without_default_activation(self):
        from customer_intervals_ui import CustomerTermsDialog
        obj = {"name": "Travel A", "company": "Renatur", "markning": "test-only", "rate": 520}
        dialog = CustomerTermsDialog(obj, [], [obj], lambda _: [])
        def dispose():
            from PyQt6.QtCore import QCoreApplication, QEvent
            dialog.close()
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)
        self.addCleanup(dispose)
        self.assertFalse(dialog.schedule_enabled.isChecked())
        dialog.populate(scheduled_terms())
        saved = dialog.value()
        self.assertEqual(saved, intervals.terms(scheduled_terms()))
        dialog.populate(example_terms())
        self.assertNotIn("schedule", dialog.value())
        dialog.populate(saved, keep_date=True)
        self.assertEqual(dialog.value(), saved)

    def test_report_close_reopen_snapshot_and_no_payroll_double_count(self):
        identifier = self.prepared("ob")
        salary_before = list(map(tuple, self.conn.execute("SELECT * FROM ob_segments")))
        report = self.window.generate_invoice()
        self.assertEqual(report["totals"]["hours"], 4)
        self.assertEqual(report["totals"]["invoice_total"], 2496)
        self.assertEqual(report["totals"]["work_amount"], 2080)
        self.assertEqual(report["totals"]["ob_amount"], 416)
        self.assertEqual(len(report["interval_details"]), 3)
        self.assertEqual(self.window._set_period_invoiced(self.window.collect_invoice_report(), True), (1, 4))
        row = dict(self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (identifier,)).fetchone())
        quote = row_prices(row)
        self.assertEqual(sum(Decimal(p["total"]) for p in quote["interval_calculation"]), Decimal("2496"))
        changed = example_terms()
        changed["rules"][1]["percent"] = 80
        intervals.save_terms(self.conn.cursor(), row["company"], row["object_name"], changed, 1)
        self.conn.execute("UPDATE objects SET rate=999")
        self.conn.commit()
        self.assertEqual(self.window.generate_invoice()["totals"]["invoice_total"], 2496)
        with self.assertRaises(ValueError):
            intervals.store_time(self.conn.cursor(), "shifts", identifier, facts("15:00", "19:00"))
        self.conn.rollback()
        self.window._set_period_invoiced(self.window.collect_invoice_report(), False)
        self.assertEqual(self.window.generate_invoice()["totals"]["invoice_total"], 2496)
        self.assertEqual(list(map(tuple, self.conn.execute("SELECT * FROM ob_segments"))), salary_before)

    def test_missing_times_block_without_fabrication_and_legacy_remains(self):
        identifier = self.prepared()
        self.conn.execute("UPDATE shifts SET time_facts=NULL WHERE sync_id=?", (identifier,))
        self.conn.commit()
        report = self.window.generate_invoice()
        self.assertIsNone(report["totals"]["invoice_total"])
        with self.assertRaisesRegex(ValueError, "Уточните время"):
            self.window._set_period_invoiced(self.window.collect_invoice_report(), True)
        self.assertEqual(self.conn.execute("SELECT invoiced FROM shifts").fetchone()[0], 0)

    def test_pdf_includes_interval_calculation(self):
        from test_customer_report import CustomerReportTests
        self.prepared()
        pdf = CustomerReportTests.pdf(self, self.window.generate_invoice())
        text = "\n".join(page.extract_text() for page in pdf.pages)
        self.assertIn("Intervall", text)
        self.assertIn("Kundtillägg", text)
        self.assertIn("2496", text.replace(" ", ""))

    def test_payroll_closed_guard_applies_to_sync_and_original_date(self):
        identifier = self.prepared()
        row = dict(self.conn.execute("SELECT * FROM shifts WHERE sync_id=?", (identifier,)).fetchone())
        before = self.conn.execute("SELECT COUNT(*) FROM time_facts_audit").fetchone()[0]
        periods = {(row["employee"], row["date"], row["date"]): {"status": "closed"}}
        with patch("payroll_history.state", return_value=(None, None, periods)):
            with self.assertRaisesRegex(ValueError, "Зарплатный период закрыт"):
                intervals.store_time(self.conn.cursor(), "shifts", identifier, facts("17:00", "21:00"),
                                     from_sync=True, previous_row=row, protect_payroll=True)
            self.conn.execute("UPDATE shifts SET date='2026-11-02' WHERE sync_id=?", (identifier,))
            with self.assertRaisesRegex(ValueError, "Зарплатный период закрыт"):
                intervals.store_time(self.conn.cursor(), "shifts", identifier, facts(),
                                     previous_row=row)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM time_facts_audit").fetchone()[0], before)


class IntervalServerTests(unittest.TestCase):
    setUpClass = classmethod(transport.OBTransportTests.setUpClass.__func__)
    tearDownClass = classmethod(transport.OBTransportTests.tearDownClass.__func__)
    setUp = transport.OBTransportTests.setUp
    _add_company = transport.OBTransportTests._add_company
    records = transport.OBTransportTests.records

    def test_simple_worker_form_legacy_hours_edit_and_retained_detailed_intervals(self):
        from pathlib import Path
        markup = (Path(self.server.__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
        for removed in ("timeEditors", "createTimeEditor", "Рассчитать часы", "time-facts-fields"):
            self.assertNotIn(removed, markup)
        self.assertIn('OB/сверхурочные</option>', markup)
        payload = dict(request_id="simple-hours", date="2026-10-21", employee="Travel Test",
                       company="Renatur", object_name="Travel A", submission_type="work", hours=4, transport=1)
        response = self.server_client.post("/submit-shift", json=payload)
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertTrue(self.server_client.post("/submit-shift", json=payload).get_json()["idempotent"])
        row = self.records()[0]
        self.assertIsNone(row["time_facts"])
        response = self.server_client.post("/edit-shift", json=dict(
            sync_id=row["sync_id"], operation_id="simple-edit", version=row["version"], hours=5))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.records()[0]["hours"], 5)
        payload.update(request_id="retained-details", date="2026-10-22", time_facts=facts())
        self.assertEqual(self.server_client.post("/submit-shift", json=payload).status_code, 201)
        row = self.records()[-1]
        original_facts = row["time_facts"]
        response = self.server_client.post("/edit-shift", json=dict(
            sync_id=row["sync_id"], operation_id="simple-comment", version=row["version"],
            hours=4, transport=0, comment="simple form preserves details"))
        self.assertEqual(response.status_code, 200, response.get_json())
        row = self.records()[-1]
        self.assertEqual(row["time_facts"], original_facts)
        self.assertEqual(row["hours"], 4)
        self.assertEqual(row["transport"], 0)
        for changes in (dict(hours=3), dict(date="2026-10-23"), dict(submission_type="ob")):
            response = self.server_client.post("/edit-shift", json=dict(
                sync_id=row["sync_id"], operation_id="unsafe-simple-edit", version=row["version"], **changes))
            self.assertEqual(response.status_code, 400, response.get_json())
            self.assertIn("десктопе", response.get_json()["error"])
            self.assertEqual(self.records()[-1]["time_facts"], original_facts)
            self.assertEqual(self.records()[-1]["hours"], 4)

    def test_customer_terms_reject_different_dataset_before_any_write(self):
        from sync_dataset import HEADER
        with patch("sync_dataset.file_dataset", return_value="prepared-dataset"):
            for method in ("get", "post"):
                response = getattr(self.server_client, method)(
                    "/api/customer-terms", headers={HEADER: "old-dataset"})
                self.assertEqual(response.status_code, 409, response.get_json())
        with closing(self.server.get_db_connection()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM customer_terms").fetchone()[0], 0)

    def test_full_web_desktop_pdf_close_sync_reopen_path(self):
        self.full_web_desktop_path(schedule=False)

    def test_simple_web_desktop_explicit_schedule_pdf_close_sync_reopen_and_edit(self):
        self.full_web_desktop_path(schedule=True)

    def full_web_desktop_path(self, *, schedule):
        from types import SimpleNamespace
        from io import BytesIO
        from pypdf import PdfReader
        from customer_report import write_customer_pdf
        from customer_intervals_desktop import cache_terms
        with closing(self.server.get_db_connection()) as db, db:
            db.execute("UPDATE meta_objects SET rate=520 WHERE name='Travel A'")
            intervals.save_terms(db.cursor(), "Renatur", "Travel A",
                                 scheduled_terms() if schedule else example_terms(), 0)
        data = dict(
            request_id="full-timed", date="2026-10-21", employee="Travel Test", company="Renatur",
            object_name="Travel A", submission_type="work", hours=4, transport=0)
        if not schedule:
            data["time_facts"] = facts()
        response = self.server_client.post("/submit-shift", json=data)
        self.assertEqual(response.status_code, 201, response.get_json())
        headers = {"X-Sync-Key": "isolated-sync-test-key"}
        pulled = self.server_client.get("/sync/pull?since=0", headers=headers).get_json()
        with closing(self.desktop.get_db_connection()) as db, db:
            object_id = db.execute("""INSERT INTO objects(company,name,markning,rate,transport_rate,sync_id)
                VALUES('Renatur','Travel A','test-a',520,450,'object-a')""").lastrowid
            db.execute("INSERT INTO employees(name,salary_rate,is_active) VALUES('Travel Test',200,1)")
            cache_terms(db.cursor(), pulled["customer_terms"])
            for remote in pulled["changes"]:
                self.desktop.SmartReportApp.apply_remote_shift(SimpleNamespace(), db.cursor(), remote)
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        def dispose():
            from PyQt6.QtCore import QCoreApplication, QEvent
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.addCleanup(dispose)
        window.tab_invoice = self.desktop.QWidget(window)
        window.setup_invoice_tab()
        window.inv_comp_cb.setCurrentText("Renatur")
        window.inv_obj_cb.setCurrentIndex(window.inv_obj_cb.findData(object_id))
        window.inv_date_from.setDate(self.desktop.QDate(2026, 10, 1))
        window.inv_date_to.setDate(self.desktop.QDate(2026, 10, 31))
        if schedule:
            from PyQt6.QtCore import Qt
            from PyQt6.QtWidgets import QTableWidget
            window._customer_schedules_supported = True
            self.assertIsNone(window.generate_invoice()["totals"]["invoice_total"])
            def select(dialog):
                table = dialog.findChild(QTableWidget, "customerTermsPreview")
                self.assertEqual(table.rowCount(), 1)
                self.assertEqual(table.item(0, 0).checkState(), Qt.CheckState.Unchecked)
                table.item(0, 0).setCheckState(Qt.CheckState.Checked)
                return self.desktop.QDialog.DialogCode.Accepted
            with patch.object(window, "_terms_versions"), \
                    patch.object(self.desktop.QDialog, "exec", select), \
                    patch.object(window, "background_sync_with_cloud"):
                window.preview_customer_terms()
        report = window.generate_invoice()
        self.assertEqual(report["totals"]["hours"], 4)
        self.assertEqual(report["totals"]["invoice_total"], 2496)
        output = BytesIO()
        write_customer_pdf(report, output, self.desktop.DEFAULT_FONT, self.desktop.DEFAULT_FONT_BOLD,
                           lambda text: text)
        self.assertIn("Intervall", "\n".join(p.extract_text() for p in PdfReader(output).pages))
        window._set_period_invoiced(window.collect_invoice_report(), True)
        with closing(self.desktop.get_db_connection()) as db:
            local = db.execute("SELECT * FROM shifts WHERE sync_id='full-timed'").fetchone()
            payload = self.desktop.shift_payload(local)
            self.assertIn("interval_calculation", json.loads(payload["customer_pricing"]))
            salary_before = local["rate"], local["hours"]
        response = self.server_client.post("/sync/push", headers=headers, json=dict(changes=[dict(
            sync_id="full-timed", operation_id="close-full-timed", operation="upsert", base_version=1, shift=payload)]))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["results"][0]["ok"])
        with closing(self.server.get_db_connection()) as db:
            saved = db.execute("SELECT * FROM incoming_submissions WHERE sync_id='full-timed'").fetchone()
            self.assertEqual(saved["invoiced"], 1)
            self.assertEqual((saved["rate"], saved["hours"]), salary_before)
        with patch.object(self.server, "bearer_admin_user", return_value={"user_id": 1, "role": "admin"}):
            reopened = self.server_client.post("/api/admin/shifts/reopen", json=dict(
                sync_id="full-timed", operation_id="reopen-full-timed", version=saved["version"]))
        self.assertEqual(reopened.status_code, 200, reopened.get_json())
        remote = reopened.get_json()["shift"]
        with closing(self.desktop.get_db_connection()) as db, db:
            db.execute("DELETE FROM shift_sync_outbox WHERE sync_id='full-timed'")
            self.desktop.SmartReportApp.apply_remote_shift(SimpleNamespace(), db.cursor(), remote)
        self.assertEqual(window.generate_invoice()["totals"]["invoice_total"], 2496)
        self.assertEqual(window.collect_invoice_report()["journal_rows"][0]["invoiced"], 0)
        if schedule:
            with closing(self.server.get_db_connection()) as db:
                saved = db.execute("SELECT * FROM incoming_submissions WHERE sync_id='full-timed'").fetchone()
                self.assertIsNone(saved["time_facts"])
                self.assertIn("schedule_application", json.loads(saved["customer_pricing"]))
            changed = self.server_client.post("/edit-shift", json=dict(
                sync_id="full-timed", operation_id="correct-legacy-hours",
                date="2026-10-21", company="Renatur", object_name="Travel A",
                submission_type="work", hours=3, transport=0, comment="Actual hours corrected"))
            self.assertEqual(changed.status_code, 200, changed.get_json())
            with closing(self.server.get_db_connection()) as db:
                saved = db.execute("SELECT * FROM incoming_submissions WHERE sync_id='full-timed'").fetchone()
                self.assertEqual(saved["hours"], 3)
                self.assertIsNone(saved["time_facts"])
                quote = json.loads(saved["customer_pricing"])
                self.assertNotIn("schedule_application", quote)
                self.assertNotIn("interval_calculation", quote)
                self.assertEqual(quote["base"], 520)

    def test_web_submit_dedup_edit_sync_and_closed_protection(self):
        with closing(self.server.get_db_connection()) as db, db:
            from customer_pricing import ensure_pricing_schema
            ensure_pricing_schema(db.cursor(), "incoming_submissions")
            intervals.save_terms(db.cursor(), "Renatur", "Travel A", example_terms(), 0)
        data = dict(request_id="timed-shift", date="2026-10-21", employee="Travel Test", company="Renatur",
                    object_name="Travel A", submission_type="work", time_facts=facts(), transport=0)
        response = self.server_client.post("/submit-shift", json=data)
        self.assertEqual(response.status_code, 201, response.get_json())
        again = self.server_client.post("/submit-shift", json=data)
        self.assertTrue(again.get_json()["idempotent"])
        row = self.records()[0]
        self.assertEqual(row["hours"], 4)
        self.assertEqual(json.loads(row["time_facts"]), facts())
        updated = self.server_client.post("/edit-shift", json=dict(
            sync_id=row["sync_id"], operation_id="new-time", version=row["version"], time_facts=facts("16:00", "19:00")))
        self.assertEqual(updated.status_code, 200, updated.get_json())
        self.assertEqual(self.records()[0]["hours"], 3)
        with closing(self.server.get_db_connection()) as db, db:
            row = db.execute("SELECT * FROM incoming_submissions WHERE sync_id=?", (row["sync_id"],)).fetchone()
            payload = self.server.serialize_submission(row)
            self.assertIn("interval_terms", json.loads(payload["customer_pricing"]))
            db.execute("UPDATE incoming_submissions SET invoiced=1")
        response = self.server_client.post("/edit-shift", json=dict(
            sync_id=row["sync_id"], operation_id="closed-time", version=row["version"], time_facts=facts()))
        self.assertEqual(response.status_code, 400, response.get_json())
