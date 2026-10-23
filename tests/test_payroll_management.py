from datetime import date
import json
import sqlite3
import unittest

import payroll_history as history
import payroll_management as management


def holidays(year):
    return {date(year, 1, 1), date(year, 12, 25)}


class ManagementPayrollTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE shifts(sync_id TEXT PRIMARY KEY,employee TEXT,date TEXT,
                company TEXT,object_name TEXT,entry_type TEXT,hours REAL,start_time TEXT,end_time TEXT);
            CREATE TABLE employees(name TEXT PRIMARY KEY,salary_rate REAL);
            INSERT INTO employees VALUES('Worker',100);
            CREATE TABLE ob_segments(id INTEGER PRIMARY KEY,shift_id TEXT,segment_date TEXT,
                start_time TEXT,end_time TEXT,hours REAL,category TEXT,premium_percent REAL,
                multiplier REAL,base_rate REAL,amount REAL);
            CREATE TABLE balance(id INTEGER PRIMARY KEY,change_amount REAL);
            INSERT INTO balance VALUES(1,1234.56);
        """)
        history.ensure_schema(self.db.cursor())
        self.addCleanup(self.db.close)

    def add(self, key="one", day="2026-10-21", hours=4, start="16:00", end="20:00", kind="work"):
        self.db.execute("INSERT INTO shifts VALUES(?,?,?,?,?,?,?,?,?)",
                        (key, "Worker", day, "Customer", "Site", kind, hours, start, end))

    def report(self, start="2026-10-01", end="2026-10-31"):
        before = list(self.db.iterdump())
        result = management.reports(self.db.cursor(), start, end, holidays, lambda a, b: 176)
        self.assertEqual(list(self.db.iterdump()), before)
        return result[0]

    def test_user_example_four_hours_five_rates_for_work_and_ob(self):
        for kind in ("work", "ob"):
            with self.subTest(kind=kind):
                self.db.execute("DELETE FROM shifts")
                self.add(kind=kind)
                report = self.report()
                self.assertEqual(report["worked"], 4)
                self.assertEqual(report["categories"], dict(day=1, ob1=1, ob2=2, ob3=0, helg=0, unknown=0))
                self.assertEqual((report["base"], report["premium"], report["total"]), (400, 100, 500))
                self.assertEqual(report["above_norm"], 0)

    def test_all_weekday_boundaries(self):
        for start, end, category, premium in (
            ("00:00", "05:00", "ob3", 350), ("05:00", "06:00", "ob1", 20),
            ("06:00", "17:00", "day", 0), ("17:00", "18:00", "ob1", 20),
            ("18:00", "22:00", "ob2", 160), ("22:00", "24:00", "ob3", 140)):
            with self.subTest(start=start):
                segments = management.split_interval("2026-10-21", start, end, None, holidays)
                self.assertTrue(all(s["category"] == category for s in segments))
                self.assertAlmostEqual(sum(s["hours"]*management.PREMIUM[category]*100 for s in segments), premium)

    def test_weekend_and_public_holiday_do_not_stack_weekday_ob(self):
        for day in ("2026-10-24", "2026-10-25", "2026-12-25"):
            with self.subTest(day=day):
                self.db.execute("DELETE FROM shifts")
                self.add(day=day)
                report = self.report(day, day)
                self.assertEqual(report["categories"]["helg"], 4)
                self.assertEqual(report["categories"]["ob2"], 0)
                self.assertEqual(report["total"], 680)

    def test_holiday_eve_not_automatically_a_holiday(self):
        parts = management.split_interval("2026-12-24", "16:00", "20:00", None, holidays)
        self.assertEqual([p["category"] for p in parts], ["day", "ob1", "ob2"])

    def test_midnight_and_period_boundary_preserve_hours(self):
        self.add(day="2026-10-23", start="23:00", end="03:00")
        all_days = self.report()
        friday = self.report("2026-10-23", "2026-10-23")
        saturday = self.report("2026-10-24", "2026-10-24")
        self.assertEqual(friday["worked"]+saturday["worked"], all_days["worked"])
        self.assertEqual((friday["categories"]["ob3"], saturday["categories"]["helg"]), (1, 3))
        self.assertEqual(friday["total"]+saturday["total"], all_days["total"])

    def test_dst_confirmed_zone_real_elapsed_time(self):
        zone = dict(timezone="Europe/Stockholm", ambiguous_policy="earlier")
        for day, expected in (("2026-10-24", 7), ("2026-03-28", 5)):
            with self.subTest(day=day):
                parts = management.split_interval(day, "22:00", "04:00", zone, holidays)
                self.assertEqual(sum(p["hours"] for p in parts), expected)
                self.assertTrue(all(p["category"] == "helg" for p in parts))

    def test_no_automatic_rounding_of_minutes(self):
        self.add(hours=1.5, start="16:15", end="17:45")
        r = self.report()
        self.assertEqual(r["categories"]["day"], .75)
        self.assertEqual(r["categories"]["ob1"], .75)
        self.assertEqual(r["total"], 165)

    def test_unspecified_break_not_arbitrarily_assigned_to_ob(self):
        self.add(hours=3)
        r = self.report()
        self.assertEqual((r["worked"], r["categories"]["unknown"], r["base"]), (3, 3, 300))
        self.assertIsNone(r["premium"])
        self.assertIsNone(r["total"])
        self.assertIn("перерыва", r["lines"][0]["warnings"][0])

    def test_no_times_exports_hours_not_zero_ob(self):
        self.add(start=None, end=None)
        r = self.report()
        self.assertEqual(r["worked"], 4)
        self.assertEqual(r["categories"]["unknown"], 4)
        self.assertIsNone(r["total"])

    def test_zero_missing_or_inactive_employee_rate_does_not_block_hours(self):
        self.add()
        for value in (None, 0):
            with self.subTest(value=value):
                self.db.execute("UPDATE employees SET salary_rate=?", (value,))
                r = self.report()
                self.assertEqual(r["worked"], 4)
                self.assertIsNone(r["total"])
                self.assertIsNone(r["rate"])
        self.db.execute("DELETE FROM employees")
        self.assertIsNone(self.report()["total"])

    def test_missing_rate_employee_does_not_hide_other_employees(self):
        self.add()
        self.db.execute("INSERT INTO shifts SELECT 'two','Unknown',date,company,object_name,entry_type,hours,start_time,end_time FROM shifts")
        result = management.reports(self.db.cursor(), "2026-10-01", "2026-10-31", holidays, lambda a,b:176)
        self.assertEqual(len(result), 2)
        self.assertEqual(next(r for r in result if r["employee"] == "Worker")["total"], 500)
        self.assertIsNone(next(r for r in result if r["employee"] == "Unknown")["total"])

    def test_atf_and_absences_are_separate_from_worked_and_ob(self):
        self.add()
        for kind in ("atf", "sick", "vacation", "holiday"):
            self.add(key=kind, hours=2, start=None, end=None, kind=kind)
        r = self.report()
        self.assertEqual(r["worked"], 4)
        self.assertEqual(r["total"], 500)
        self.assertEqual(r["absence"], dict(atf=2, sick=2, vacation=2, holiday=2))

    def test_old_ob_segments_reclassify_only_in_memory(self):
        self.add(kind="ob", start=None, end=None)
        self.db.execute("INSERT INTO ob_segments VALUES(1,'one','2026-10-21','16:00','20:00',4,'old',50,1.5,999,5994)")
        r = self.report()
        self.assertEqual(r["total"], 500)
        self.assertEqual(self.db.execute("SELECT amount FROM ob_segments").fetchone()[0], 5994)

    def test_confirmed_history_and_payments_not_changed_by_current_rate_estimate(self):
        self.add()
        snapshot = dict(employee="Worker", date_from="2026-10-01", date_to="2026-10-31",
                        status="closed", paid=True, amounts=dict(base_salary=123), revision=1)
        self.db.execute("INSERT INTO payroll_events VALUES(1,'op','{}',?,'admin','now')",
                        (json.dumps(dict(kind="close", snapshot=snapshot)),))
        self.assertEqual(self.report()["total"], 500)
        self.db.execute("UPDATE employees SET salary_rate=200")
        r = self.report()
        self.assertEqual(r["total"], 1000)
        self.assertEqual(r["saved_periods"], 1)
        self.assertEqual(history.state(history.events(self.db.cursor()))[2][("Worker","2026-10-01","2026-10-31")], snapshot)
        self.assertEqual(self.db.execute("SELECT change_amount FROM balance").fetchone()[0], 1234.56)

    def test_readonly_connection_does_not_require_ledger_or_migration(self):
        self.add()
        self.db.execute("DROP TABLE payroll_events")
        self.db.execute("PRAGMA query_only=ON")
        self.assertEqual(self.report()["total"], 500)

    def test_wrong_date_range_explicit_error(self):
        with self.assertRaisesRegex(ValueError, "Начало"):
            self.report("2026-10-31", "2026-10-01")

    def test_unknown_details_keep_null_partial_and_mismatched_source_times(self):
        for start, end, expected in ((None, None, "начала и окончания"),
                                     ("16:00", None, "окончания"), (None, "20:00", "начала")):
            with self.subTest(start=start, end=end):
                self.db.execute("DELETE FROM shifts")
                self.add(start=start, end=end)
                line = self.report()["lines"][0]
                self.assertEqual(line["start_time"], start)
                self.assertEqual(line["end_time"], end)
                self.assertEqual(line["saved_intervals"], [])
                self.assertIn(expected, line["warnings"][0])
                self.assertEqual(line["categories"]["unknown"], 4)
        self.db.execute("DELETE FROM shifts")
        self.add(start="16:00", end="20:00", hours=3)
        line = self.report()["lines"][0]
        self.assertEqual((line["start_time"], line["end_time"]), ("16:00", "20:00"))
        self.assertIn("перерыва", line["warnings"][0])

    def test_saved_times_and_segment_fallback_are_used_not_marked_unknown(self):
        self.add(kind="work")
        line = self.report()["lines"][0]
        self.assertEqual(line["categories"]["unknown"], 0)
        self.assertEqual((line["start_time"], line["end_time"]), ("16:00", "20:00"))
        self.db.execute("UPDATE shifts SET start_time=NULL,end_time=NULL")
        self.db.execute("INSERT INTO ob_segments VALUES(1,'one','2026-10-21','16:00','20:00',4,'old',50,1.5,999,5994)")
        line = self.report()["lines"][0]
        self.assertEqual(line["categories"]["unknown"], 0)
        self.assertEqual(line["total"], 500)
        self.assertEqual(line["saved_intervals"], [
            dict(date="2026-10-21", start_time="16:00", end_time="20:00", hours=4)])
