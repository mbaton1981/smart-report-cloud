import unittest
from datetime import date

import assistant_attention as attention


class AttentionTests(unittest.TestCase):
    def test_requests_and_default_periods(self):
        for text in ("Что требует моего внимания?", "Что нужно проверить?",
                     "Что осталось незавершённым?", "Покажи проблемы за эту неделю"):
            self.assertTrue(attention.is_attention_request(text))
        periods = attention.periods("Что требует моего внимания?", date(2026, 11, 13))
        self.assertEqual(periods["hours"], ("2026-11-02", "2026-11-08"))
        self.assertEqual(periods["prices"], ("2026-11-01", "2026-11-30"))
        self.assertIsNone(periods["documents"])
        explicit = attention.periods("Что нужно проверить с 2026-10-01 по 2026-10-31?")
        self.assertEqual(explicit["documents"], ("2026-10-01", "2026-10-31"))
        month = attention.periods("Что нужно проверить за прошлый месяц?", date(2026, 11, 13))
        self.assertEqual(month["hours"], ("2026-10-01", "2026-10-31"))
        self.assertEqual(month["documents"], month["hours"])

    def test_active_employees_without_work_or_ob(self):
        employees = [{"name": name, "is_active": active} for name, active in (
            ("Alice", 1), ("Bob", 1), ("Carol", 1), ("Inactive", 0))]
        rows = [
            {"employee": "Alice", "entry_type": "work", "hours": 8, "date": "2026-11-02",
             "company": "A", "object_name": "X"},
            {"employee": "Bob", "entry_type": "ob", "hours": 2, "date": "2026-11-03",
             "company": "A", "object_name": "X"},
            {"employee": "Carol", "entry_type": "transport", "hours": 0, "date": "2026-11-03",
             "company": "A", "object_name": "X"},
        ]
        missing, _warnings = attention.missing_hours(employees, rows, ("2026-11-02", "2026-11-08"))
        self.assertEqual([row["employee"] for row in missing], ["Carol"])

    def test_prices_use_saved_snapshot_and_distinguish_zero(self):
        row = dict(sync_id="one", date="2026-11-02", company="A", object_name="X",
                   employee="Alice", entry_type="work", rate=0, transport=0)
        quote = dict(version=1, company="A", object_name="X", date_from="2026-11-01",
                     date_to="2026-11-30", source="confirmed", base=450, travel=0, ob={})
        known = dict(row, customer_pricing=quote)
        zero = dict(row, sync_id="two", customer_pricing={**quote, "base": 0})
        unknown = dict(row, sync_id="three", customer_pricing=None, transport=1)
        issues = attention.price_issues([known, zero, unknown])
        self.assertEqual([issue["id"] for issue in issues], ["three"])
        self.assertIn("почасовая", issues[0]["reason"])
        self.assertIn("транспорта", issues[0]["reason"])

    def test_scope_must_be_unambiguous(self):
        objects = [{"company": "First", "name": "Shared"}, {"company": "Second", "name": "Shared"}]
        with self.assertRaisesRegex(ValueError, "неоднозначно"):
            attention.scope_for_text("Что нужно проверить по объекту Shared", objects)
        self.assertEqual(attention.scope_for_text("Что нужно проверить по объекту Shared фирмы First", objects),
                         ("First", "Shared"))
        self.assertEqual(attention.scope_for_text("Что нужно проверить", objects), ("", ""))

    def test_ob_uses_saved_category_conditions(self):
        quote = dict(version=1, company="A", object_name="X", date_from="2026-11-01",
                     date_to="2026-11-30", source="confirmed", base=450, travel=0, ob={})
        row = dict(sync_id="ob", date="2026-11-03", company="A", object_name="X",
                   employee="Alice", entry_type="ob", hours=2, transport=0, customer_pricing=quote,
                   _attention_segments=[{"hours": 2, "premium_percent": 50,
                                         "category": "weekday_early_evening"}])
        self.assertIn("OB1", attention.price_issues([row])[0]["reason"])
        row["customer_pricing"] = {**quote, "ob": {"weekday_early_evening": 0}}
        self.assertEqual(attention.price_issues([row]), [])
