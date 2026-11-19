from datetime import date
import unittest

import payroll_auto as auto


def holidays(year):
    return {date(year, 12, 25)}


def row(key, kind="ob", day="2026-11-19", start="16:00", end="20:00", hours=4, company="Renatur", **extra):
    return dict(sync_id=key, employee="Worker", entry_type=kind, date=day,
                company=company, object_name=key, start_time=start, end_time=end, hours=hours, **extra)


def hours(result, category):
    return round(sum(s["hours"] for s in result["segments"] if s["category"] == category), 6)


class AutomaticPayrollTests(unittest.TestCase):
    def calculate(self, *rows, settings=None):
        return auto.classify([(r, []) for r in rows], "Worker", {}, holidays, settings)

    def test_day_then_ob_becomes_overtime_across_objects(self):
        result = self.calculate(row("day", "work", start="07:00", end="16:00", hours=8), row("extra"))
        self.assertEqual(hours(result["day"], "day"), 8)
        self.assertEqual([hours(result["extra"], key) for key in auto.OVERTIME], [1, 2, 1, 0])
        self.assertEqual(hours(result["extra"], "ob2"), 0)
        self.assertIn("перерывом", result["day"]["explanation"])

    def test_ob_without_ordinary_day_remains_ob(self):
        result = self.calculate(row("evening"))["evening"]
        self.assertEqual(hours(result, "ob2"), 2)
        self.assertEqual(hours(result, "day"), 2)
        self.assertEqual(sum(hours(result, k) for k in auto.OVERTIME), 0)

    def test_ordinary_untimed_work_counts_and_is_not_unknown(self):
        result = self.calculate(row("day", "work", start=None, end=None, hours=8), row("extra"))
        self.assertEqual(hours(result["day"], "day"), 8)
        self.assertEqual(hours(result["extra"], "ot_b"), 2)
        self.assertEqual(result["day"]["warnings"], [])

    def test_long_single_ob_shift_deducts_exact_standard_break(self):
        result = self.calculate(row("long", start="07:00", end="20:00", hours=12))["long"]
        self.assertEqual(hours(result, "day"), 8)
        self.assertEqual(sum(hours(result, k) for k in auto.OVERTIME), 4)

    def test_weekend_and_holiday_are_ob_even_with_many_hours(self):
        for day in ("2026-11-21", "2026-11-22", "2026-12-25"):
            result = self.calculate(row("weekend", day=day, start="07:00", end="20:00", hours=13))["weekend"]
            self.assertEqual(hours(result, "helg"), 13)
            self.assertEqual(sum(hours(result, k) for k in auto.OVERTIME), 0)

    def test_private_all_hours_regular_even_nights_weekends_and_missing_times(self):
        for day in ("2026-11-19", "2026-11-21"):
            for start, end in (("22:00", "02:00"), (None, None)):
                result = self.calculate(row("private", day=day, start=start, end=end, company=" Privat "))["private"]
                self.assertEqual(hours(result, "day"), 4)
                self.assertEqual(hours(result, "unknown"), 0)
                self.assertIn("Privat", result["explanation"])

    def test_explicit_actual_time_breaks_and_customer_overtime_do_not_control_payroll(self):
        facts = dict(version=1, start="07:00", end="20:00", breaks=[dict(start="11:30", end="12:30")],
                     overtime=[dict(start="07:00", end="08:00")])
        result = self.calculate(row("actual", hours=12, time_facts=facts))["actual"]
        # An explicit lunch replaces the standard lunch; the customer mode is ignored.
        self.assertEqual(sum(hours(result, k) for k in auto.OVERTIME), 4)
        self.assertEqual(sum(s["hours"] for s in result["segments"]), 12)
        self.assertEqual(result["warnings"], [])

    def test_unknown_ob_missing_times_or_break_mismatch(self):
        for r in (row("missing", start=None, end=None), row("mismatch", hours=3)):
            result = self.calculate(r)[r["sync_id"]]
            self.assertEqual(hours(result, "unknown"), r["hours"])
            self.assertTrue(result["warnings"])

    def test_overlap_marks_both_records_without_premiums(self):
        result = self.calculate(row("one"), row("two"))
        for value in result.values():
            self.assertEqual(hours(value, "unknown"), 4)
            self.assertIn("пересекаются", value["warnings"][0])

    def test_midnight_splits_categories_and_preserves_minutes(self):
        result = self.calculate(row("midnight", day="2026-11-20", start="23:15", end="02:45", hours=3.5))["midnight"]
        self.assertEqual(hours(result, "ob3"), .75)
        self.assertEqual(hours(result, "helg"), 2.75)

    def test_overtime_continues_across_midnight_until_next_workday(self):
        result = self.calculate(row("day", "work", start=None, end=None, hours=8),
                                row("night", start="22:00", end="02:00"))["night"]
        self.assertEqual(hours(result, "ot_d"), 4)
        self.assertEqual(hours(result, "ob3"), 0)

    def test_personal_dated_schedule_overrides_global(self):
        settings = dict(version=1, schedules=[dict(employee="Worker", effective_from="2026-11-19",
            schedule=dict(days=[0,1,2,3,4], start="08:00", end="17:00", breaks=[dict(start="12:00", end="13:00")]))])
        result = self.calculate(row("day", "work", start="08:00", end="17:00", hours=8),
                                row("extra", start="17:00", hours=3), settings=settings)
        self.assertEqual(hours(result["extra"], "ot_b"), 2)
        self.assertEqual(auto.schedule_for(settings, "Other", "2026-11-19"), auto.DEFAULT_SCHEDULE)
        self.assertEqual(auto.schedule_for(settings, "Worker", "2026-11-18"), auto.DEFAULT_SCHEDULE)

    def test_dst_uses_elapsed_time_and_ambiguous_time_requires_review(self):
        result = self.calculate(row("dst", day="2026-10-25", start="00:00", end="04:00", hours=5))["dst"]
        self.assertEqual(hours(result, "helg"), 5)
        bad = self.calculate(row("dst", day="2026-10-25", start="02:00", end="04:00", hours=2))["dst"]
        self.assertEqual(hours(bad, "unknown"), 2)

    def test_invalid_schedule_rejected(self):
        for change in (dict(days=[]), dict(end="06:00"), dict(breaks=[dict(start="06:00", end="07:00")])):
            with self.assertRaises(ValueError):
                auto.validate_schedule(dict(auto.DEFAULT_SCHEDULE, **change))
