import copy
from datetime import date, datetime
import json
import unittest
from unittest.mock import patch

import analytics
from customer_report import prepare_customer_costs
from fixed_pricing import create_fixed, fixed_quote
import payroll_history as payroll
import test_ob_transport as transport


class AnalyticsTests(unittest.TestCase):
    setUpClass = classmethod(transport.OBTransportTests.setUpClass.__func__)
    tearDownClass = classmethod(transport.OBTransportTests.tearDownClass.__func__)
    _add_company = transport.OBTransportTests._add_company
    submit = transport.OBTransportTests.submit

    def setUp(self):
        transport.OBTransportTests.setUp(self)

    def calculate(self, start="2026-10-01", end="2026-10-31", documents=None, **filters):
        c = self.server.get_db_connection()
        try:
            c.execute("PRAGMA query_only=ON")
            c.execute("BEGIN")
            return analytics.build(c.cursor(), start, end, documents or [], **filters)
        finally:
            c.close()

    def quote(self, sync_id, base, travel=0, ob=None):
        c = self.server.get_db_connection()
        try:
            row = c.execute("SELECT * FROM incoming_submissions WHERE sync_id=?", (sync_id,)).fetchone()
            quote = dict(version=1, company=row["company"], object_name=row["object_name"],
                         date_from=row["date"], date_to=row["date"], source="Test historical agreement",
                         base=base, travel=travel, ob=ob or {})
            c.execute("UPDATE incoming_submissions SET customer_pricing=? WHERE sync_id=?",
                      (json.dumps(quote), sync_id))
            c.commit()
        finally:
            c.close()

    def document(self, net="100", gross="125", vat="25", allocations=None, **values):
        return dict(id=1, document_date="2026-10-20", verification_status="confirmed",
                    currency_code="SEK", net_amount=net, gross_amount=gross, vat_amount=vat,
                    allocations=allocations or [], allocation_basis="gross", **values)

    def test_historical_prices_ob_hours_travel_and_existing_customer_report(self):
        self.submit("work", "work", "Travel A", 1)
        self.submit("ob", "ob", "Travel B", 1)
        self.quote("work", 100, 25)
        self.quote("ob", 150, 30, {"weekday_early_evening": 20, "weekday_late_evening_night": 40})
        data = self.calculate()
        self.assertEqual(data["totals"]["hours"], 12)
        self.assertEqual(data["totals"]["employees"], 1)
        self.assertEqual(data["totals"]["known_hours"], 12)
        c = self.server.get_db_connection()
        try:
            expected = 0
            for obj in ("Travel A", "Travel B"):
                rows = [dict(r, entry_type=r["submission_type"]) for r in c.execute(
                    "SELECT * FROM incoming_submissions WHERE object_name=?", (obj,))]
                segments = {r["sync_id"]: [dict(s) for s in c.execute(
                    "SELECT * FROM ob_segments WHERE shift_id=?", (r["sync_id"],))] for r in rows}
                report = dict(journal_rows=rows, journal_segments=segments, totals={"without_ob_hours": 0}, warnings=[])
                prepare_customer_costs(report)
                expected += report["totals"]["invoice_total"]
            self.assertEqual(data["totals"]["revenue"], expected)
        finally:
            c.close()
        self.assertEqual(data["totals"]["revenue"], 1575)  # 800 + 600 + 2*20 + 2*40 + 25 + 30
        self.assertTrue(data["totals"]["partial"])
        self.assertIn("текущей ставке", " ".join(data["rows"][0]["notes"]))
        self.assert_reconciled(data)

    def assert_reconciled(self, data):
        for key in ("hours", "revenue", "cost", "result", "expenses", "wages"):
            self.assertAlmostEqual(data["totals"][key], sum(r[key] for r in data["rows"]), places=2)
        self.assertAlmostEqual(data["totals"]["hours"], sum(p["hours"] for p in data["hours_chart"]))
        for key in ("revenue", "cost"):
            self.assertAlmostEqual(data["totals"][key], sum(p[key] for p in data["objects_chart"]))

    def test_fixed_price_once_and_not_distributed_across_period(self):
        self.submit("one", "work", "Travel A", 0)
        c = self.server.get_db_connection()
        c.execute("UPDATE incoming_submissions SET date='2026-10-19' WHERE sync_id='one'")
        c.commit()
        c.close()
        self.submit("two", "work", "Travel A", 0)
        c = self.server.get_db_connection()
        try:
            c.execute("UPDATE incoming_submissions SET date='2026-10-21' WHERE sync_id='two'")
            c.execute("UPDATE incoming_submissions SET customer_pricing=NULL")
            rows = [dict(r, entry_type=r["submission_type"]) for r in c.execute("SELECT * FROM incoming_submissions")]
            fixed = create_fixed(dict(journal_rows=rows, company="Renatur", sync_id="test-object",
                                      date_from="2026-10-01", date_to="2026-10-31", fast_pris_amount="1000.00"))
            for row in rows:
                c.execute("UPDATE incoming_submissions SET customer_pricing=? WHERE sync_id=?",
                          (json.dumps(fixed_quote(row, fixed)), row["sync_id"]))
            c.commit()
        finally:
            c.close()
        data = self.calculate()
        self.assertEqual(data["totals"]["revenue"], 1000)
        self.assertEqual(data["totals"]["hours"], 16)
        subset = self.calculate(start="2026-10-20", end="2026-10-21")
        self.assertEqual(subset["totals"]["revenue"], 0)
        self.assertTrue(subset["rows"][0]["revenue_missing"])
        self.assertFalse(subset["rows"][0]["fixed"][0]["included"])
        self.assertEqual(subset["rows"][0]["fixed"][0]["amount"], 1000)
        self.assert_reconciled(subset)

    def test_expenses_vat_general_negative_currency_and_no_payment_double_count(self):
        self.submit("work", "work", "Travel A", 0)
        self.quote("work", 10)
        c = self.server.get_db_connection()
        try:
            obj = c.execute("SELECT * FROM meta_objects WHERE name='Travel A'").fetchone()
            allocation = dict(object_id=obj["id"], company="Renatur", object_name="Travel A",
                              gross_amount="75", net_amount=None, vat_amount=None)
        finally:
            c.close()
        docs = [self.document(allocations=[allocation])]
        data = self.calculate(documents=docs)
        self.assertEqual(data["totals"]["expenses"], 100)
        objects = {r["key"]: r for r in data["rows"]}
        self.assertEqual(objects["object:"+str(obj["id"])]["expenses"], 60)
        self.assertEqual(objects["general"]["expenses"], 40)
        self.assertLess(data["totals"]["result"], 0)
        filtered = self.calculate(documents=docs, object_key="object:"+str(obj["id"]))
        self.assertEqual(filtered["totals"]["expenses"], 60)
        self.assertIn("не включены", " ".join(filtered["notes"]))
        # A paid total is deliberately not an input to the projection.
        docs[0]["paid_amount"] = "125"
        self.assertEqual(self.calculate(documents=docs)["totals"], data["totals"])
        docs += [self.document(net=None, vat=None), self.document(currency="ignored")]
        docs[-1]["currency_code"] = "EUR"
        incomplete = self.calculate(documents=docs)
        self.assertEqual(incomplete["totals"]["expenses"], 100)
        self.assertEqual(len(incomplete["unknown_expenses"]), 2)
        self.assert_reconciled(data)

    def test_missing_rates_empty_deleted_and_nonwork_categories(self):
        self.assertFalse(self.calculate()["totals"]["has_data"])
        self.submit("missing", "work", "Travel A", 0)
        self.quote("missing", None)
        data = self.calculate()
        self.assertEqual(data["totals"]["known_hours"], 0)
        self.assertEqual(data["totals"]["unknown_records"], 1)
        self.assertTrue(data["totals"]["partial"])
        c = self.server.get_db_connection()
        try:
            c.execute("UPDATE incoming_submissions SET is_deleted=1")
            c.commit()
        finally:
            c.close()
        self.assertFalse(self.calculate()["totals"]["has_data"])
        c = self.server.get_db_connection()
        c.execute("UPDATE incoming_submissions SET is_deleted=0,submission_type='holiday'")
        c.commit()
        c.close()
        data = self.calculate()
        self.assertEqual(data["totals"]["hours"], 0)
        self.assertTrue(data["totals"]["partial"])
        self.assertEqual(data["totals"]["cost"], 0)

    def test_saved_payroll_cost_uses_existing_amounts_not_net_pay_or_balance(self):
        self.submit("work", "work", "Travel A", 0)
        self.quote("work", 500)
        c = self.server.get_db_connection()
        try:
            # Ordinary work without times uses the confirmed dated rate.
            c.execute("UPDATE incoming_submissions SET start_time=NULL,end_time=NULL")
            payroll.ensure_schema(c.cursor())
            def action(kind, **values):
                payroll.apply_action(c.cursor(), "incoming_submissions", dict(
                    kind=kind, employee="Travel Test", expected_seq=len(payroll.events(c.cursor())),
                    operation_id="analytics-"+kind, **values), "test-admin", writes_enabled=True)
            params = dict(vacation=10, tax=30, employer_fee=20, atf=0, sick=0)
            action("rate", effective_from="2026-10-01", rate=100)
            draft = payroll.preview(c.cursor(), "incoming_submissions", payroll.events(c.cursor()),
                                    "Travel Test", "2026-10-01", "2026-10-31", params)
            action("close", date_from="2026-10-01", date_to="2026-10-31", parameters=params,
                   preview_hash=draft["preview_hash"])
            c.commit()
            before = self.calculate()
            self.assertEqual(before["totals"]["wages"], 1056)  # 800 + 80 vacation + 176 employer fee
            self.assertFalse(before["totals"]["partial"])
            action("payment-status", date_from="2026-10-01", date_to="2026-10-31", paid=True, reason="Test payment")
            c.commit()
            after = self.calculate()
            self.assertEqual(before["totals"], after["totals"])
            self.assertEqual(after["totals"]["result"], 4000-1056)
            self.assertEqual(self.calculate(start="2026-10-20", end="2026-10-20")["totals"]["wages"], 1056)
        finally:
            c.close()

    def test_api_admin_dataset_readonly_and_refresh(self):
        self.submit("one", "work", "Travel A", 0)
        params = dict(start="2026-10-01", end="2026-10-31")
        self.assertEqual(self.server_client.get("/api/admin/analytics", query_string=params).status_code, 403)
        with patch.object(self.server, "bearer_admin_user", return_value={"role": "admin"}):
            c = self.server.get_db_connection()
            before = "\n".join(c.iterdump())
            c.close()
            response = self.server_client.get("/api/admin/analytics", query_string=params)
            self.assertEqual(response.status_code, 200, response.get_json())
            c = self.server.get_db_connection()
            self.assertEqual("\n".join(c.iterdump()), before)
            c.execute("UPDATE incoming_submissions SET hours=7")
            c.commit()
            c.close()
            again = self.server_client.get("/api/admin/analytics", query_string=params).get_json()["analytics"]
            self.assertEqual(again["totals"]["hours"], 7)
            with patch("sync_dataset.file_dataset", return_value="analytics-dataset"):
                wrong = self.server_client.get("/api/admin/analytics", query_string=params,
                                               headers={"X-SmartReport-Dataset": "wrong"})
                self.assertEqual(wrong.status_code, 409)
            self.assertEqual(self.server_client.get("/api/admin/analytics",
                query_string=dict(start="2026-10-31", end="2026-10-01")).status_code, 400)

    def test_saved_accrual_rounding_reconciles_across_days_and_objects(self):
        self.submit("one", "work", "Travel A", 0)
        self.submit("two", "work", "Travel B", 0)
        c = self.server.get_db_connection()
        try:
            c.execute("UPDATE incoming_submissions SET hours=1,date='2026-10-19' WHERE sync_id='one'")
            c.execute("UPDATE incoming_submissions SET hours=1,date='2026-10-20' WHERE sync_id='two'")
            c.execute("UPDATE incoming_submissions SET start_time=NULL,end_time=NULL")
            params = dict(vacation=10, tax=30, employer_fee=20, atf=0, sick=0)
            payroll.apply_action(c.cursor(), "incoming_submissions", dict(
                kind="rate", employee="Travel Test", operation_id="cent-rate",
                expected_seq=len(payroll.events(c.cursor())),
                effective_from="2026-10-01", rate=.02), "test-admin", writes_enabled=True)
            draft = payroll.preview(c.cursor(), "incoming_submissions", payroll.events(c.cursor()),
                                    "Travel Test", "2026-10-01", "2026-10-31", params)
            payroll.apply_action(c.cursor(), "incoming_submissions", dict(
                kind="close", employee="Travel Test", operation_id="cent-close",
                expected_seq=len(payroll.events(c.cursor())),
                date_from="2026-10-01", date_to="2026-10-31", parameters=params,
                preview_hash=draft["preview_hash"]), "test-admin", writes_enabled=True)
            c.commit()
        finally:
            c.close()
        whole = self.calculate()
        pieces = [self.calculate(start=day, end=day) for day in ("2026-10-19", "2026-10-20")]
        self.assertEqual(whole["totals"]["wages"], round(sum(p["totals"]["wages"] for p in pieces), 2))
        for data in [whole, *pieces]:
            self.assert_reconciled(data)

    def test_period_boundaries_stockholm_and_chart_grouping(self):
        self.assertEqual(analytics.period("week", date(2027, 1, 1)), (date(2026, 12, 28), date(2027, 1, 1)))
        self.assertEqual(analytics.period("previous_week", date(2026, 12, 28)), (date(2026, 12, 21), date(2026, 12, 27)))
        self.assertEqual(analytics.period("previous_month", date(2027, 1, 1)), (date(2026, 12, 1), date(2026, 12, 31)))
        self.assertEqual(analytics.period("previous_month", date(2024, 3, 1)), (date(2024, 2, 1), date(2024, 2, 29)))
        self.assertEqual(analytics.period("month", date(2026, 10, 25)), (date(2026, 10, 1), date(2026, 10, 25)))
        data = self.calculate(start="2026-01-01", end="2026-12-31")
        self.assertTrue(data["weekly"])
        self.assertTrue(all(date.fromisoformat(p["label"]).weekday() == 0 for p in data["hours_chart"][1:]))

    def test_missing_employee_binding_inactive_history_and_other_chart_bucket(self):
        self.submit("original", "work", "Travel A", 0)
        c = self.server.get_db_connection()
        try:
            c.execute("UPDATE meta_employees SET is_active=0 WHERE name='Travel Test'")
            original = dict(c.execute("SELECT * FROM incoming_submissions").fetchone())
            for index in range(11):
                row = dict(original, id=None, sync_id=f"unlinked-{index}", request_id=f"request-{index}",
                           employee="Unknown worker", object_name=f"Historical object {index}", hours=index+1,
                           customer_pricing=None, rate=100, transport=0)
                c.execute("INSERT INTO incoming_submissions ("+",".join(row)+") VALUES ("+
                          ",".join("?" for _ in row)+")", tuple(row.values()))
            c.commit()
        finally:
            c.close()
        data = self.calculate()
        self.assertEqual(data["totals"]["hours"], 74)
        self.assertEqual(data["totals"]["employees"], 2)
        self.assertEqual(len(data["objects_chart"]), 9)
        self.assertEqual(data["objects_chart"][-1]["label"], "Остальные")
        self.assertIn("Unknown worker", " ".join(data["notes"]))
        self.assertTrue(any("связь не установлена" in row["label"] for row in data["rows"]))
        self.assert_reconciled(data)

    def test_expense_net_allocations_credit_and_unverified_documents(self):
        docs = [self.document(net="-100", gross="-125", vat="-25")]
        data = self.calculate(documents=docs)
        self.assertEqual(data["totals"]["expenses"], -100)
        self.assertEqual(data["totals"]["result"], 100)
        doc = self.document(net="100", gross="125", vat="25")
        doc["allocation_basis"] = "net"
        doc["allocations"] = [dict(object_id=None, net_amount="100", gross_amount=None, vat_amount=None)]
        data = self.calculate(documents=[doc])
        self.assertEqual(data["totals"]["expenses"], 100)
        self.assertEqual(data["rows"][0]["key"], "general")
        doc["verification_status"] = "draft"
        data = self.calculate(documents=[doc])
        self.assertEqual(data["totals"]["cost"], 0)
        self.assertTrue(data["totals"]["partial"])
        doc["document_date"] = ""
        self.assertIn("без даты", " ".join(self.calculate(documents=[doc])["notes"]))

    def test_true_zero_expense_is_not_missing_and_company_destination_is_not_object_cost(self):
        data = self.calculate(documents=[self.document(net="0", gross="0", vat="0")])
        self.assertTrue(data["totals"]["has_data"])
        self.assertEqual(data["totals"]["expense_documents"], 1)
        self.assertEqual(data["totals"]["cost"], 0)
        self.assertEqual(data["rows"][0]["key"], "general")
        doc = self.document(allocations=[dict(object_id=1, company="Renatur", object_name="Travel A",
                                             destination="company", company_expense=True,
                                             gross_amount="125", net_amount="100", vat_amount="25")])
        all_objects = self.calculate(documents=[doc])
        self.assertEqual(all_objects["rows"][0]["key"], "general")
        scoped = self.calculate(documents=[doc], company="Renatur")
        self.assertEqual(scoped["totals"]["expenses"], 0)
        self.assert_reconciled(all_objects)

    def test_estimate_updates_after_catalogue_salary_change_without_changing_history(self):
        self.submit("work", "work", "Travel A", 0)
        before = self.calculate()
        c = self.server.get_db_connection()
        try:
            c.execute("UPDATE meta_employees SET salary_rate=250 WHERE name='Travel Test'")
            c.commit()
        finally:
            c.close()
        after = self.calculate()
        self.assertEqual(before["totals"]["wages"], 1600)
        self.assertEqual(after["totals"]["wages"], 2000)
        self.assertEqual(after["totals"]["estimated_wage_records"], 1)
        self.assertEqual(before["totals"]["revenue"], after["totals"]["revenue"])
        self.assertTrue(after["totals"]["partial"])

    def test_confirmed_zero_customer_rate_is_not_missing(self):
        self.submit("work", "work", "Travel A", 0)
        self.quote("work", 0)
        data = self.calculate()
        self.assertEqual(data["totals"]["known_hours"], 8)
        self.assertFalse(data["rows"][0]["revenue_missing"])
        self.assertEqual(data["totals"]["revenue"], 0)

    def test_ob_missing_segments_unknown_not_invented_and_no_business_writes(self):
        self.submit("ob", "ob", "Travel B", 1)
        self.quote("ob", 100, 30, {"weekday_early_evening": 20, "weekday_late_evening_night": 40})
        c = self.server.get_db_connection()
        try:
            c.execute("DELETE FROM ob_segments")
            c.commit()
            before = "\n".join(c.iterdump())
            data = self.calculate()
            self.assertEqual(data["totals"]["hours"], 4)
            self.assertTrue(data["rows"][0]["revenue_missing"])
            self.assertEqual(data["totals"]["revenue"], 430)  # known base and one trip, no invented OB
            self.assertEqual("\n".join(c.iterdump()), before)
        finally:
            c.close()

    def test_ui_keeps_filters_selection_scroll_and_does_not_rebuild_identical_data(self):
        from analytics_ui import AnalyticsWidget
        self.submit("one", "work", "Travel A", 0)
        payload = self.calculate()
        window = AnalyticsWidget(lambda params: {"ok": True, "analytics": payload})
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        window.start.setDate(self.desktop.QDate(2026, 10, 1))
        window.end.setDate(self.desktop.QDate(2026, 10, 31))
        window.serial = 7
        window.last_parameters = window.parameters()
        window.received(7, copy.deepcopy(payload), "")
        selected_key = payload["rows"][0]["key"]
        for index in range(window.table.rowCount()):
            if window.table.item(index, 0).data(self.desktop.Qt.ItemDataRole.UserRole) == selected_key:
                window.table.setCurrentCell(index, 0)
                break
        selected_index = window.table.currentRow()
        old_filters = window.parameters()
        count = window._render_count
        payload["calculated_at"] = "2026-10-25T20:11:00+01:00"
        window.received(7, copy.deepcopy(payload), "")
        self.assertEqual(window._render_count, count)
        self.assertEqual(window.parameters(), old_filters)
        self.assertEqual(window.table.currentRow(), selected_index)
        payload["totals"]["revenue"] += 1
        window.received(7, copy.deepcopy(payload), "")
        self.assertEqual(window._render_count, count+1)
        self.assertEqual(window.table.item(window.table.currentRow(), 0).text(), payload["rows"][0]["label"])
        window.serial = 8
        window.received(7, {}, "")
        self.assertEqual(window._render_count, count+1)
        window.object.setCurrentIndex(window.object.findData(selected_key))
        filters = window.parameters()
        removed = copy.deepcopy(payload)
        removed["objects"] = []
        window.render(removed)
        self.assertEqual(window.parameters(), filters)
        self.assertFalse(window.timer.isActive())
