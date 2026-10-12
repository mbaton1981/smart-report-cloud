import unittest
from datetime import date

from ob_calculator import calculate_work_interval, load_ob_rules


class ObCalculatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_ob_rules()

    def calculate(self, day, start, end, rate=100):
        return calculate_work_interval(day, start, end, rate, self.rules)

    def test_weekday_day_period_uses_base_rate(self):
        result = self.calculate("2026-10-12", "07:00", "18:00")
        self.assertEqual(result["total_hours"], 11.0)
        self.assertEqual(result["total_amount"], 1100.0)
        self.assertEqual(result["segments"][0]["multiplier"], 1.0)
        self.assertEqual(result["segments"][0]["premium_percent"], 0.0)

    def test_early_evening_period_uses_fifty_percent(self):
        result = self.calculate("2026-10-12", "18:00", "20:00")
        self.assertEqual(result["total_amount"], 300.0)
        self.assertEqual(result["segments"][0]["multiplier"], 1.5)

    def test_late_evening_period_uses_seventy_percent(self):
        result = self.calculate("2026-10-12", "20:00", "00:00")
        self.assertEqual(result["total_hours"], 4.0)
        self.assertEqual(result["total_amount"], 680.0)
        self.assertEqual(result["segments"][0]["multiplier"], 1.7)

    def test_saturday_and_sunday_use_double_rate(self):
        saturday = self.calculate("2026-10-10", "09:00", "13:00")
        sunday = self.calculate("2026-10-11", "09:00", "13:00")
        self.assertEqual(saturday["total_amount"], 800.0)
        self.assertEqual(sunday["total_amount"], 800.0)
        self.assertEqual(saturday["segments"][0]["multiplier"], 2.0)
        self.assertEqual(sunday["segments"][0]["multiplier"], 2.0)

    def test_interval_crossing_three_weekday_periods_is_split(self):
        result = self.calculate("2026-10-12", "17:00", "21:00")
        self.assertEqual(
            [(segment["start_time"], segment["end_time"], segment["multiplier"]) for segment in result["segments"]],
            [("17:00", "18:00", 1.0), ("18:00", "20:00", 1.5), ("20:00", "21:00", 1.7)],
        )
        self.assertEqual(result["total_hours"], 4.0)
        self.assertEqual(result["total_amount"], 570.0)

    def test_overnight_interval_uses_each_local_dates_rules(self):
        result = self.calculate("2026-10-09", "22:00", "02:00")
        self.assertEqual(
            [(segment["date"], segment["start_time"], segment["end_time"], segment["multiplier"])
             for segment in result["segments"]],
            [
                ("2026-10-09", "22:00", "00:00", 1.7),
                ("2026-10-10", "00:00", "02:00", 2.0),
            ],
        )
        self.assertEqual(result["total_amount"], 740.0)

    def test_stockholm_fall_back_duration_uses_elapsed_time(self):
        result = self.calculate("2026-10-25", "01:30", "03:30")
        self.assertEqual(result["total_hours"], 3.0)
        self.assertEqual(result["total_amount"], 600.0)
        self.assertEqual(result["timezone"], "Europe/Stockholm")

    def test_rejects_invalid_or_zero_length_intervals_and_negative_rate(self):
        invalid = [
            ("2026-10-12", "25:00", "26:00", 100),
            ("2026-10-12", "10:00", "10:00", 100),
            ("2026-10-12", "10:00", "11:00", -1),
            ("not-a-date", "10:00", "11:00", 100),
        ]
        for day, start, end, rate in invalid:
            with self.subTest(day=day, start=start, end=end, rate=rate):
                with self.assertRaises(ValueError):
                    self.calculate(day, start, end, rate)

    def test_holiday_rule_can_override_weekday_configuration(self):
        rules = dict(self.rules)
        rules["holidays"] = {
            date(2026, 10, 12).isoformat(): [
                {"start": "00:00", "end": "24:00", "category": "holiday", "premium_percent": 100, "multiplier": 2.0}
            ]
        }
        result = calculate_work_interval("2026-10-12", "07:00", "09:00", 100, rules)
        self.assertEqual(result["segments"][0]["category"], "holiday")
        self.assertEqual(result["total_amount"], 400.0)


if __name__ == "__main__":
    unittest.main()