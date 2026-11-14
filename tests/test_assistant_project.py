import unittest
from datetime import date
import json

from assistant_project import expense_summary, project_candidates, project_period, project_request, shift_summary
from customer_report import prepare_customer_costs
from fixed_pricing import create_fixed, fixed_quote


class ProjectRequestTests(unittest.TestCase):
    def test_natural_project_requests_do_not_escape_to_employee_or_global_hours(self):
        for text in ("Что у нас сейчас по Томтебуда Ренатур?",
                     "Что у нас сейчас потом-то, Бода, Ренатур?",
                     "Что у нас сейчас потом Тбода Ренатур?",
                     "Что у нас потом Тбода Ренатур за ноябрь?",
                     "Хочу сводку по объекту, по проекту",
                     "Хочу сводку по проекту", "Дай сводку по Томтебоде",
                     "Теперь по Badbacken"):
            with self.subTest(text=text):
                self.assertTrue(project_request(text))
        for text in ("Кто сколько отработал?", "Покажи часы сотрудника Иван Иванов",
                     "Сводка за неделю", "Покажи расходы фирмы Renatur"):
            with self.subTest(text=text):
                self.assertFalse(project_request(text))

    def test_dictated_names_companies_and_marking_variants(self):
        objects = [
            {"id": 1, "name": "Tomteboda ÄTA", "company": "Renatur", "markning": "p1034"},
            {"id": 2, "name": "Tomteboda ÄTA Rivning", "company": "Renatur", "markning": "p1038"},
            {"id": 3, "name": "Tomteboda ÄTA", "company": "Other", "markning": "p2034"},
            {"id": 4, "name": "Badbacken 5 ext", "company": "Privat", "markning": "p1022"},
        ]
        for text in ("Что по Томтебоде у Ренатура за ноябрь?",
                     "Что у Томтабода Ренатур за ноябрь?",
                     "Что у нас сейчас потом-то, Бода, Ренатур?",
                     "Что у нас сейчас потом Тбода Ренатур?",
                     "Что у нас сейчас по Томтебуда Ренатур?",
                     "Сводка по проекту Томтебода Renatur"):
            with self.subTest(text=text):
                self.assertEqual(project_candidates(text, objects), objects[:2])
        self.assertTrue(project_request("Что у Томтабода Ренатур за ноябрь?", objects))
        self.assertTrue(project_request("Что у неизвестного объекта Ренатур за ноябрь?", objects))
        self.assertFalse(project_request("Что у Иван Иванов за ноябрь?", objects))
        self.assertFalse(project_request("Что у сотрудника Иван Иванов за ноябрь?", objects))
        self.assertEqual(project_candidates("Что по Tomteboda?", objects), objects[:3])
        self.assertEqual(project_candidates("Что по Томтебода Эта Ривнинг у Ренатюр?", objects), objects[1:2])
        for marking in ("p1038", "P1038", "p10-38", "п 1038", "Пэ-1038", "пэ 1038"):
            with self.subTest(marking=marking):
                self.assertEqual(project_candidates("Что по " + marking, objects), objects[1:2])
        self.assertEqual(project_candidates("Что по Tomteboda у неизвестной фирмы?", objects), [])
        self.assertEqual(project_candidates("Что по Неизвестному?", objects), [])
        self.assertEqual(project_candidates("Что по p9999 Tomteboda?", objects), [])

    def test_requests(self):
        for text in ("Что сейчас по Badbacken?", "Покажи состояние объекта p1022",
                     "Сколько часов и расходов по этому объекту?",
                     "Что по Tomteboda за ноябрь?"):
            with self.subTest(text=text):
                self.assertTrue(project_request(text))

    def test_periods(self):
        self.assertIsNone(project_period("Что по Badbacken?"))
        self.assertEqual(project_period("Что по Tomteboda за ноябрь?", date(2026, 11, 13)),
                         ("2026-11-01", "2026-11-30"))
        self.assertEqual(project_period("с 01.02.2024 по 29.02.2024"),
                         ("2024-02-01", "2024-02-29"))
        self.assertEqual(project_period("за прошлый месяц", date(2026, 1, 13)),
                         ("2025-12-01", "2025-12-31"))
        self.assertEqual(project_period("с 01.10.2026 по 31.10.2026, а не за ноябрь", date(2025, 11, 13)),
                         ("2026-10-01", "2026-10-31"))
        self.assertIsNone(project_period("за весь объект"))
        with self.assertRaises(ValueError):
            project_period("за непонятный период")

    def test_marking_and_ambiguous_names(self):
        objects = [
            {"id": 1, "name": "Tomteboda", "company": "A", "markning": "p1000"},
            {"id": 2, "name": "Tomteboda ÄTA", "company": "A", "markning": "p1001"},
            {"id": 3, "name": "Badbacken 5 ext", "company": "B", "markning": "p1022"},
        ]
        self.assertEqual(project_candidates("Что по Tomteboda?", objects), objects[:2])
        self.assertEqual(project_candidates("Покажи объект p1022", objects), objects[2:])
        self.assertEqual(project_candidates("Что по Badbacken?", objects), objects[2:])

    def test_expense_shares_and_unknown_amounts(self):
        documents = [
            {"document_date": "2026-11-02", "verification_status": "confirmed",
             "currency_code": "SEK", "gross_amount": "125", "net_amount": "100",
             "vat_amount": "25", "allocations": [
                 {"object_id": 1, "gross_amount": "75"},
                 {"object_id": 2, "gross_amount": "50"},
             ]},
            {"document_date": "2026-11-03", "verification_status": "confirmed",
             "currency_code": "USD", "gross_amount": "10", "net_amount": None,
             "vat_amount": None, "allocations": [{"object_id": 1, "gross_amount": "10"}]},
            {"document_date": "2026-11-04", "verification_status": "draft",
             "currency_code": "SEK", "allocations": [{"object_id": 1, "gross_amount": "30"}]},
            {"document_date": None, "verification_status": "confirmed",
             "allocations": [{"object_id": 1, "gross_amount": "20"}]},
        ]
        result = expense_summary(documents, 1, ("2026-11-01", "2026-11-30"))
        self.assertEqual(str(result["totals"]["SEK"]["net_amount"]), "60.00")
        self.assertEqual(str(result["totals"]["SEK"]["vat_amount"]), "15.00")
        self.assertEqual(str(result["totals"]["SEK"]["gross_amount"]), "75")
        self.assertEqual(str(result["totals"]["USD"]["gross_amount"]), "10")
        self.assertIn(("USD", "vat_amount"), result["unknown"])
        self.assertIsNone(result["totals"]["USD"]["vat_amount"])
        self.assertEqual(len(result["linked"]), 3)
        self.assertEqual(len(result["undated"]), 1)

    def test_customer_cost_and_ob_use_historical_prices_and_do_not_double_count(self):
        rows = [self.row("one", 4, 400), self.row("two", 4, 400, entry_type="ob", transport=1)]
        segments = {"two": [{"premium_percent": 50, "category": "weekday_early_evening",
                              "segment_date": "2026-11-02", "start_time": "18:00",
                              "end_time": "20:00", "hours": 2}]}
        report = {"journal_rows": rows, "journal_segments": segments, "warnings": [],
                  "totals": {"without_ob_hours": 6}}
        prepare_customer_costs(report)
        summary = shift_summary({"journal_rows": rows, "journal_segments": segments,
                                 "customer_report": report, "hours": 8, "employees": 1})
        self.assertEqual((summary["hours"], summary["ordinary"], summary["ob"]), (8, 6, 2))
        self.assertEqual(summary["work_amount"], 3200)
        self.assertEqual(summary["transport_amount"], 450)
        self.assertEqual(summary["ob_amount"], 200)
        self.assertEqual(summary["total"], 3850)
        self.assertEqual(summary["unknown_records"], 0)

    def test_missing_prices_and_fixed_price_partial_period(self):
        missing = self.row("one", 4, None)
        report = {"journal_rows": [missing], "journal_segments": {}, "warnings": [],
                  "totals": {"without_ob_hours": 4}}
        prepare_customer_costs(report)
        result = shift_summary({"journal_rows": [missing], "journal_segments": {},
                                "customer_report": report, "hours": 4, "employees": 1})
        self.assertIsNone(result["total"])
        self.assertEqual(result["unknown_records"], 1)
        row = self.row("two", 4, 400)
        fixed_report = {"journal_rows": [row], "company": "A", "sync_id": "project",
                        "date_from": "2026-11-01", "date_to": "2026-11-30",
                        "fast_pris_amount": 2000}
        fixed = create_fixed(fixed_report)
        row["customer_pricing"] = json.dumps(fixed_quote(row, fixed))
        report = {"journal_rows": [row], "journal_segments": {}, "warnings": [],
                  "totals": {"without_ob_hours": 4}}
        prepare_customer_costs(report)
        overview = {"journal_rows": [row], "journal_segments": {}, "customer_report": report,
                    "hours": 4, "employees": 1}
        full = shift_summary(overview)
        self.assertEqual(full["total"], 2000)
        partial = shift_summary(overview, ("2026-11-02", "2026-11-02"))
        self.assertIsNone(partial["total"])
        self.assertTrue(partial["incomplete_fixed"])

    @staticmethod
    def row(identifier, hours, rate, entry_type="work", transport=0):
        quote = {"version": 1, "company": "A", "object_name": "Project",
                 "date_from": "2026-11-02", "date_to": "2026-11-02",
                 "source": "test", "base": rate, "travel": 450,
                 "ob": {"weekday_early_evening": 100}}
        return {"id": int({"one": 1, "two": 2}[identifier]), "sync_id": identifier,
                "date": "2026-11-02", "employee": "E", "company": "A",
                "object_name": "Project", "hours": hours, "rate": 0, "transport": transport,
                "entry_type": entry_type, "invoiced": 0, "hours_confirmed": 0,
                "start_time": "", "end_time": "", "work_rate_cache": 0,
                "customer_pricing": json.dumps(quote)}


if __name__ == "__main__":
    unittest.main()
