import json
import sqlite3
import unittest
import uuid
from unittest.mock import patch

import payroll_history as payroll


PARAMS = dict(vacation=13, tax=30.06, employer_fee=31.42, atf=0, sick=0)


class PayrollHistoryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE shifts(sync_id TEXT PRIMARY KEY,employee TEXT,date TEXT,
                company TEXT,object_name TEXT,entry_type TEXT,hours REAL,start_time TEXT,end_time TEXT);
            CREATE TABLE ob_segments(id INTEGER PRIMARY KEY,shift_id TEXT,segment_date TEXT,
                start_time TEXT,end_time TEXT,hours REAL,category TEXT,premium_percent REAL,
                multiplier REAL,base_rate REAL,amount REAL);
            CREATE TABLE balance(id INTEGER PRIMARY KEY,change_amount REAL);
            INSERT INTO balance VALUES(1,1234.56);
        """)
        payroll.ensure_schema(self.db.cursor())
        self.add("one", "2026-10-21", 4)

    def tearDown(self):
        self.assertEqual(self.db.execute("SELECT change_amount FROM balance").fetchone()[0], 1234.56)
        self.db.close()

    def add(self, key, day, hours, kind="work", start=None, end=None):
        self.db.execute("INSERT INTO shifts VALUES(?,?,?,?,?,?,?,?,?)",
                        (key, "Worker", day, "Customer", "Site", kind, hours, start, end))

    def action(self, kind, **kwargs):
        body = dict(kind=kind, employee="Worker", expected_seq=len(payroll.events(self.db.cursor())),
                    operation_id=str(uuid.uuid4()), **kwargs)
        payroll.apply_action(self.db.cursor(), "shifts", body, "test-admin", writes_enabled=True)
        return body

    def preview(self, **kwargs):
        return payroll.preview(self.db.cursor(), "shifts", payroll.events(self.db.cursor()), "Worker",
                               kwargs.pop("start", "2026-10-01"), kwargs.pop("end", "2026-10-31"),
                               PARAMS, **kwargs)

    def save(self, kind="close", **kwargs):
        draft = self.preview(**{k: v for k, v in kwargs.items() if k in ("overrides", "reprice")})
        return self.action(kind, date_from=draft["date_from"], date_to=draft["date_to"],
                           parameters=PARAMS, preview_hash=draft["preview_hash"], **kwargs)

    def zone(self):
        self.action("zone", company="Customer", object_name="Site", timezone="Europe/Stockholm")

    def test_migration_never_confirms_history(self):
        self.assertEqual(payroll.events(self.db.cursor()), [])
        self.assertIsNone(self.preview()["amounts"])
        self.assertIn("Требует подтверждения", self.preview()["lines"][0]["warnings"][0])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM shifts").fetchone()[0], 1)

    def test_dated_rate_does_not_backfill(self):
        self.action("rate", effective_from="2026-10-22", rate=300)
        self.assertIsNone(self.preview()["amounts"])
        self.add("two", "2026-10-22", 2)
        line = self.preview()["lines"][1]
        self.assertEqual(line["amount"], 600)

    def test_night_rate_boundary_and_no_lost_hours(self):
        self.db.execute("DELETE FROM shifts")
        self.add("night", "2026-10-21", 6, start="22:00", end="04:00")
        self.zone()
        self.action("rate", effective_from="2026-10-01", rate=100)
        self.action("rate", effective_from="2026-10-22", rate=200)
        line = self.preview()["lines"][0]
        self.assertEqual(line["hours"], 6)
        self.assertEqual(line["amount"], 1000)
        first = self.preview(start="2026-10-21", end="2026-10-21")["lines"][0]
        second = self.preview(start="2026-10-22", end="2026-10-22")["lines"][0]
        self.assertEqual(first["hours"]+second["hours"], 6)
        self.assertEqual(first["amount"]+second["amount"], 1000)

    def test_unconfirmed_zone_blocks_timed_calculation(self):
        self.db.execute("DELETE FROM shifts")
        self.add("night", "2026-10-21", 6, start="22:00", end="04:00")
        self.action("rate", effective_from="2026-10-01", rate=100)
        self.assertIsNone(self.preview()["amounts"])
        self.assertIn("Часовая зона", self.preview()["lines"][0]["warnings"][0])

    def test_dst_real_elapsed_hours(self):
        self.db.execute("DELETE FROM shifts")
        self.zone()
        self.action("rate", effective_from="2026-01-01", rate=100)
        for day, hours in (("2026-10-24", 7), ("2026-03-28", 5)):
            with self.subTest(day=day):
                self.db.execute("DELETE FROM shifts")
                self.add("night", day, hours, start="22:00", end="04:00")
                line = self.preview(start=day[:7]+"-01", end=day[:7]+"-31")["lines"][0]
                self.assertEqual(line["hours"], hours)
                self.assertEqual(line["amount"], hours*100)

    def test_saved_ob_preserved_and_unknown_not_zero(self):
        self.db.execute("UPDATE shifts SET entry_type='ob'")
        self.db.execute("INSERT INTO ob_segments VALUES(1,'one','2026-10-21','16:00','18:00',2,'day',0,1,100,200)")
        self.db.execute("INSERT INTO ob_segments VALUES(2,'one','2026-10-21','18:00','20:00',2,'ob1',50,1.5,100,300)")
        line = self.preview()["lines"][0]
        self.assertEqual(line["amount"], 500)
        self.save()
        self.action("rate", effective_from="2026-10-01", rate=999)
        self.assertEqual(self.preview()["previous"]["amounts"]["base_salary"], 500)
        self.assertEqual(self.preview()["amounts"]["base_salary"], 500)

    def test_closed_snapshot_and_new_rows_need_adjustment(self):
        self.action("rate", effective_from="2026-10-01", rate=100)
        self.save()
        self.add("two", "2026-10-22", 2)
        self.assertTrue(self.preview()["source_changed"])
        self.assertEqual(self.preview()["previous"]["amounts"]["base_salary"], 400)
        with self.assertRaisesRegex(ValueError, "корректировкой"):
            self.save()
        self.save("adjust", reason="Additional approved hours")
        event = json.loads(payroll.events(self.db.cursor())[-1]["event_json"])
        self.assertEqual(event["difference"]["base_salary"], 200)
        self.assertEqual(event["snapshot"]["revision"], 2)
        self.assertFalse(event["snapshot"]["paid"])

    def test_snapshot_stores_outputs_and_formula_version(self):
        self.action("rate", effective_from="2026-10-01", rate=100)
        self.save()
        old = self.preview()["previous"]
        with patch.object(payroll, "RULES_VERSION", "future-rules"):
            self.assertEqual(self.preview()["previous"], old)
        self.assertEqual(old["rules_version"], "payroll-1")
        self.assertEqual(old["amounts"]["base_salary"], 400)

    def test_repeat_and_retry_no_duplicate(self):
        self.action("rate", effective_from="2026-10-01", rate=100)
        request = self.save()
        count = len(payroll.events(self.db.cursor()))
        payroll.apply_action(self.db.cursor(), "shifts", request, "test-admin", writes_enabled=True)
        self.save()
        self.assertEqual(len(payroll.events(self.db.cursor())), count)

    def test_explicit_confirmation_and_paid_separate_from_closure(self):
        self.save("confirm", overrides={"one:2026-10-21": 100})
        self.assertFalse(self.preview()["previous"]["paid"])
        self.assertEqual(self.preview()["previous"]["status"], "confirmed")
        self.action("payment-status", date_from="2026-10-01", date_to="2026-10-31",
                    paid=True, reason="Reconciled existing payment; no new payment")
        self.assertTrue(self.preview()["previous"]["paid"])

    def test_preview_stale_or_overlapping_rejected(self):
        self.action("rate", effective_from="2026-10-01", rate=100)
        draft = self.preview()
        self.db.execute("UPDATE shifts SET hours=5")
        with self.assertRaisesRegex(ValueError, "после просмотра"):
            self.action("close", date_from="2026-10-01", date_to="2026-10-31",
                        parameters=PARAMS, preview_hash=draft["preview_hash"])
        self.save()
        self.assertTrue(self.preview(start="2026-10-20", end="2026-10-31")["overlap"])

    def test_compatible_reader_preserves_future_ledger_and_blocks_writes(self):
        self.action("rate", effective_from="2026-10-01", rate=100)
        self.save()
        before = payroll.events(self.db.cursor())
        with patch.object(payroll, "WRITES_ENABLED", False):
            self.assertEqual(self.preview()["previous"]["amounts"]["base_salary"], 400)
            with self.assertRaisesRegex(ValueError, "только для чтения"):
                payroll.apply_action(self.db.cursor(), "shifts", {}, "test")
        copy = sqlite3.connect(":memory:")
        copy.row_factory = sqlite3.Row
        self.db.commit()
        self.db.backup(copy)
        self.assertEqual(payroll.events(copy.cursor()), before)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            copy.execute("DELETE FROM payroll_events")
        copy.close()

    def test_sync_rejects_truncation_and_missing_versions(self):
        self.action("rate", effective_from="2026-10-01", rate=100)
        with self.assertRaises(ValueError):
            payroll.replace_cache(self.db.cursor(), [])
        before = payroll.events(self.db.cursor())
        payroll.replace_cache(self.db.cursor(), before)
        self.assertEqual(payroll.events(self.db.cursor()), before)

    def test_known_rate_not_silently_overwritten(self):
        self.action("rate", effective_from="2026-10-01", rate=100)
        with self.assertRaisesRegex(ValueError, "защищена"):
            self.preview(overrides={"one:2026-10-21": 999})

    def test_new_ob_uses_segment_date_rates_and_explicit_zone(self):
        self.action("rate", effective_from="2026-10-01", rate=100)
        self.action("rate", effective_from="2026-10-22", rate=200)
        with self.assertRaisesRegex(ValueError, "зону"):
            payroll.entry_interval(self.db.cursor(), "Worker", "Customer", "Site",
                                   "2026-10-21", "22:00", "04:00", 999)
        self.zone()
        result = payroll.entry_interval(self.db.cursor(), "Worker", "Customer", "Site",
                                        "2026-10-21", "22:00", "04:00", 999)
        self.assertEqual(result["total_hours"], 6)
        self.assertEqual({s["base_rate"] for s in result["segments"] if s["date"] == "2026-10-21"}, {100})
        self.assertEqual({s["base_rate"] for s in result["segments"] if s["date"] == "2026-10-22"}, {200})
        self.assertEqual(sum(s["hours"] for s in result["segments"]), 6)

    def test_paid_correction_requires_new_payment_reconciliation_not_balance_write(self):
        self.action("rate", effective_from="2026-10-01", rate=100)
        self.save()
        self.action("payment-status", date_from="2026-10-01", date_to="2026-10-31", paid=True, reason="Existing payment")
        self.add("two", "2026-10-22", 2)
        self.save("adjust", reason="New approved hours")
        saved = self.preview()["previous"]
        self.assertFalse(saved["paid"])
        self.assertEqual(saved["previously_paid_revision"], 1)

    def test_new_or_deleted_closed_rows_do_not_change_customer_payroll(self):
        self.action("rate", effective_from="2026-10-01", rate=100)
        self.save()
        self.db.execute("DELETE FROM shifts")
        self.add("new", "2026-10-21", 12)
        result = payroll.customer_payroll(self.db.cursor(), "Customer", ["Site"], "2026-10-01", "2026-10-31")
        self.assertEqual(result["Worker"]["total"], 400)
        self.assertTrue(result["Worker"]["warnings"])

    def test_unknown_night_zone_not_omitted_on_following_day(self):
        self.db.execute("DELETE FROM shifts")
        self.add("night", "2026-10-21", 6, start="22:00", end="04:00")
        draft = self.preview(start="2026-10-22", end="2026-10-22")
        self.assertEqual(len(draft["lines"]), 1)
        self.assertIsNone(draft["amounts"])


if __name__ == "__main__":
    unittest.main()
