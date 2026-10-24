from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
import base64
import json
import os
import sqlite3
import uuid
import unittest
import wave
from unittest.mock import Mock, patch

from reportlab.pdfgen.canvas import Canvas
import requests

import assistant_api as assistant
import test_employee_cards as cards


def plan(purpose="check", **changes):
    return dict(purpose=purpose, date_from="2026-10-01", date_to="2026-10-31",
                employee="", company="", object_name="") | changes


# Reuse the fixture methods without inheriting its tests.
class AssistantApiTests(unittest.TestCase):
    setUpClass = classmethod(cards.EmployeeCardTests.setUpClass.__func__)
    tearDownClass = classmethod(cards.EmployeeCardTests.tearDownClass.__func__)
    _add_company = cards.EmployeeCardTests._add_company
    headers = cards.EmployeeCardTests.headers

    def setUp(self):
        cards.EmployeeCardTests.setUp(self)
        self.addCleanup(self._case.cleanup)
        self.environment = patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-api-key-not-real"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        with closing(self.server.get_db_connection()) as conn:
            assistant.ensure_schema(conn)
            conn.execute("UPDATE assistant_settings SET enabled=1")
            conn.commit()
        self.base = "/api/assistant/"

    def post(self, path, data):
        return self.server_client.post(self.base + path, json=data, headers=self.headers())

    def request_command(self):
        return dict(text="Проверь смены с 2026-10-01 по 2026-10-31", operation_id=str(uuid.uuid4()))

    def response(self, action=None):
        return Mock(status_code=200, json=Mock(return_value={
            "status": "completed", "output": [{"type": "function_call", "name": "inspect_records",
                                              "arguments": json.dumps(action or plan())}]}))

    def business(self):
        with closing(self.server.get_db_connection()) as conn:
            return {name: list(map(tuple, conn.execute('SELECT * FROM "' + name + '"')))
                    for name in ("incoming_submissions", "meta_employees", "meta_objects", "payroll_events")}

    def test_roles_and_transport_boundary(self):
        for role in ("accountant", "user"):
            self.assertEqual(self.server_client.get(self.base + "status", headers=self.headers(role)).status_code, 403)
        self.assertEqual(self.server_client.get(self.base + "status").status_code, 403)
        self.assertEqual(self.server_client.get(self.base + "status", headers={"X-Sync-Key": "isolated-sync-test-key"}).status_code, 403)
        response = self.server_client.get(self.base + "status", headers=self.headers(),
                                          base_url="http://external.invalid", environ_base={"REMOTE_ADDR": "1.2.3.4"})
        self.assertEqual(response.status_code, 403)
        good = self.server_client.get(self.base + "status", headers=self.headers())
        self.assertEqual(good.status_code, 200)
        self.assertIn("no-store", good.headers["Cache-Control"])
        self.assertNotIn("synthetic-api-key", good.get_data(as_text=True))

    def test_no_key_disabled_zero_limit_never_contact_provider(self):
        with patch("assistant_api.requests.post") as provider:
            for settings, key in ((dict(enabled=False, limit_usd=10), "x"),
                                  (dict(enabled=True, limit_usd=10), ""),
                                  (dict(enabled=True, limit_usd=0), "x")):
                self.assertEqual(self.post("status", settings).status_code, 200)
                with patch.dict(os.environ, {"OPENAI_API_KEY": key}):
                    response = self.post("command", self.request_command())
                self.assertEqual(response.status_code, 400)
            provider.assert_not_called()

    def test_setting_invalid_numbers_no_silent_defaults(self):
        for value in ("NaN", "Infinity", -1, 1001, "not-money"):
            self.assertEqual(self.post("status", dict(enabled=True, limit_usd=value)).status_code, 400)
        self.assertEqual(self.post("status", dict(enabled="yes", limit_usd=1)).status_code, 400)

    def test_command_uses_only_tool_result_no_business_changes_or_data_to_model(self):
        before = self.business()
        with patch("assistant_api.requests.post", return_value=self.response()) as provider:
            response = self.post("command", self.request_command())
        self.assertEqual(response.status_code, 200, response.get_json())
        result = response.get_json()["result"]
        self.assertEqual(result["records"], [])
        self.assertEqual(before, self.business())
        body = provider.call_args.kwargs["json"]
        self.assertFalse(body["store"])
        self.assertEqual(body["model"], assistant.MODEL)
        self.assertNotIn("Synthetic Employee", json.dumps(body))
        with closing(self.server.get_db_connection()) as conn:
            audit = list(conn.execute("SELECT * FROM assistant_audit"))
            self.assertTrue(audit)
            self.assertNotIn("Проверь", repr(audit))
            self.assertNotIn("synthetic-api-key", repr(audit))

    def test_provider_error_charged_once_and_retry_rejected(self):
        command = self.request_command()
        with patch("assistant_api.requests.post", side_effect=requests.Timeout("synthetic-api-key-sensitive")) as provider:
            response = self.post("command", command)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("sensitive", response.get_data(as_text=True))
            response = self.post("command", command)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(provider.call_count, 1)
        with closing(self.server.get_db_connection()) as conn:
            self.assertGreater(assistant.status(conn)["reserved_usd"], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM assistant_requests").fetchone()[0], 1)

    def test_model_cannot_invoke_arbitrary_tool_or_writes(self):
        response = self.response()
        response.json.return_value["output"][0]["name"] = "execute_sql"
        before = self.business()
        with patch("assistant_api.requests.post", return_value=response):
            self.assertEqual(self.post("command", self.request_command()).status_code, 400)
        self.assertEqual(before, self.business())
        for bad in (plan(purpose="delete"), plan(date_from="yesterday"), plan(date_from="20261001"), plan(date_to="2029-01-01"),
                    plan(extra="sql"), plan(employee=5)):
            with self.assertRaises(assistant.AssistantError):
                assistant.validate_plan(bad)

    def test_duplicate_employee_never_picks_first(self):
        with closing(self.server.get_db_connection()) as conn:
            conn.execute("INSERT INTO meta_employees(name) VALUES('SYNTHETIC EMPLOYEE')")
            conn.commit()
        with patch("assistant_api.requests.post", return_value=self.response(plan(employee="Synthetic Employee"))):
            response = self.post("command", self.request_command())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["clarification"]["field"], "scope")
        self.assertNotIn("result", response.get_json())

    def test_budget_reservation_atomic_with_two_connections(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        with closing(self.server.get_db_connection()) as a:
            a.execute("UPDATE assistant_settings SET limit_micro=100")
            a.commit()
        barrier = Barrier(2)

        def reserve():
            with closing(self.server.get_db_connection()) as conn:
                barrier.wait(timeout=10)
                try:
                    assistant.reserve(conn, dict(user_id=self.admin_id), str(uuid.uuid4()), "text", 70)
                    return True
                except assistant.AssistantError:
                    conn.rollback()
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: reserve(), range(2))), [False, True])
        with closing(self.server.get_db_connection()) as a:
            self.assertEqual(a.execute("SELECT SUM(charged_micro) FROM assistant_requests").fetchone()[0], 70)

    def test_voice_validation_transcription_never_executes_command(self):
        audio = BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 1600)
        provider = Mock(status_code=200, json=Mock(return_value={"text": "Покажи отчёт"}))
        with patch("assistant_api.requests.post", return_value=provider) as post:
            bad = self.server_client.post(self.base + "transcribe", data=b"notwav",
                                          headers=self.headers() | {"X-Operation-ID": str(uuid.uuid4())})
            self.assertEqual(bad.status_code, 400)
            post.assert_not_called()
            good = self.server_client.post(self.base + "transcribe", data=audio.getvalue(),
                                           headers=self.headers() | {"X-Operation-ID": str(uuid.uuid4())})
        self.assertEqual(good.get_json()["text"], "Покажи отчёт")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.business()["incoming_submissions"], [])

    def mail(self):
        stream = BytesIO()
        canvas = Canvas(stream)
        canvas.drawString(20, 700, "Synthetic SmartReport")
        canvas.save()
        return dict(to="synthetic@example.invalid", subject="Test report", body="Synthetic only",
                    pdf=base64.b64encode(stream.getvalue()).decode())

    def smtp_env(self):
        return patch.dict(os.environ, dict(SMARTREPORT_SMTP_HOST="smtp.example.invalid",
                                          SMARTREPORT_SMTP_USER="synthetic",
                                          SMARTREPORT_SMTP_PASSWORD="synthetic-secret",
                                          SMARTREPORT_SMTP_FROM="sender@example.invalid"))

    def test_mail_requires_explicit_confirmation_and_unchanged_content(self):
        payload = self.mail()
        with self.smtp_env(), patch("assistant_api.smtplib.SMTP") as smtp:
            draft = self.post("mail/prepare", payload).get_json()["draft"]
            smtp.assert_not_called()
            command = dict(draft_id=draft["draft_id"], payload=payload, confirmed=False)
            self.assertEqual(self.post("mail/confirm", command).status_code, 400)
            command["confirmed"] = True
            command["payload"] = payload | {"to": "changed@example.invalid"}
            self.assertEqual(self.post("mail/confirm", command).status_code, 400)
            smtp.assert_not_called()
            command["payload"] = payload
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            response = self.post("mail/confirm", command)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(self.post("mail/confirm", command).status_code, 400)
            self.assertEqual(smtp.call_count, 1)
            smtp.return_value.__enter__.return_value.starttls.assert_called_once()

    def test_mail_failure_never_retries_or_leaks_password(self):
        with self.smtp_env(), patch("assistant_api.smtplib.SMTP", side_effect=OSError("synthetic-secret")) as smtp:
            payload = self.mail()
            draft = self.post("mail/prepare", payload).get_json()["draft"]
            command = dict(draft_id=draft["draft_id"], payload=payload, confirmed=True)
            response = self.post("mail/confirm", command)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("synthetic-secret", response.get_data(as_text=True))
            self.assertEqual(self.post("mail/confirm", command).status_code, 400)
            self.assertEqual(smtp.call_count, 1)

    def test_pdf_plan_never_closes_period_or_confirms_prices(self):
        before = self.business()
        with patch("assistant_api.requests.post", return_value=self.response(plan("payroll_pdf"))):
            response = self.post("command", self.request_command())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["result"]["plan"]["purpose"], "payroll_pdf")
        self.assertEqual(before, self.business())

    def fixture_shift(self):
        from customer_pricing import new_shift_pricing, store_pricing
        with closing(self.server.get_db_connection()) as conn:
            conn.execute("INSERT INTO meta_objects(company,name,rate,transport_rate,is_active) VALUES('Renatur','Hotel',520,450,1)")
            conn.execute("""INSERT INTO incoming_submissions(sync_id,request_id,date,employee,company,
                object_name,hours,rate,transport,submission_type,created_at,updated_at)
                VALUES('assistant-five','assistant-five','2026-10-23','Synthetic Employee','Renatur',
                'Hotel',5,0,0,'work','2026-10-23','2026-10-23')""")
            row = dict(conn.execute("SELECT * FROM incoming_submissions WHERE sync_id='assistant-five'").fetchone())
            quote = new_shift_pricing(conn.cursor(), "incoming_submissions", row)
            store_pricing(conn.cursor(), "incoming_submissions", row["sync_id"], quote)
            conn.commit()

    def test_transport_command_and_button_produce_same_clear_result_without_provider(self):
        self.fixture_shift()
        before = self.business()
        command = dict(text="Проверь транспорт за текущую неделю", operation_id=str(uuid.uuid4()))
        with patch("assistant_api.requests.post") as provider, patch("assistant_api.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
            response = self.post("command", command)
            self.assertEqual(response.status_code, 200, response.get_json())
            result = response.get_json()["result"]
            self.assertEqual(result["plan"]["purpose"], "transport_check")
            self.assertEqual(result["plan"]["date_from"], "2026-10-19")
            self.assertEqual(result["plan"]["date_to"], "2026-10-25")
            button = self.post("inspect", dict(plan=result["plan"], operation_id=str(uuid.uuid4())))
            self.assertEqual(button.status_code, 200, button.get_json())
            self.assertEqual(button.get_json()["result"], result)
            provider.assert_not_called()
        self.assertEqual(result["transport"]["checked"], 1)
        self.assertEqual(result["transport"]["attention"], 0)
        self.assertEqual(Decimal(result["transport"]["known_subtotal"]), Decimal("0"))
        self.assertEqual(before, self.business())
        from assistant_ui import result_text, result_table
        text = result_text(result)
        self.assertIn("2026-10-19", text)
        self.assertIn("требуют внимания: 0", text)
        self.assertIn("Фактические поездки", text)
        self.assertNotIn("assistant-five", text)
        headers, rows = result_table(result)
        self.assertIn("Справочник, SEK/выезд", headers)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(headers), len(rows[0]))
        self.assertNotIn("assistant-five", repr(rows))
        with closing(self.server.get_db_connection()) as conn:
            self.assertEqual(assistant.status(conn)["reserved_usd"], 0)

    def test_transport_mismatch_missing_quantity_fixed_and_closed_are_not_repriced(self):
        self.fixture_shift()
        with closing(self.server.get_db_connection()) as conn:
            conn.execute("UPDATE incoming_submissions SET transport=2,invoiced=1")
            conn.execute("UPDATE meta_objects SET transport_rate=500")
            conn.commit()
        before = self.business()
        response = self.post("inspect", dict(plan=plan("transport_check"), operation_id=str(uuid.uuid4())))
        result = response.get_json()["result"]
        self.assertEqual(result["transport"]["attention"], 1)
        self.assertEqual(Decimal(result["transport"]["known_subtotal"]), Decimal("900"))
        self.assertEqual(before, self.business())
        with closing(self.server.get_db_connection()) as conn:
            records = result["records"]
            records[0].update(travel_rate=None, travel_only=None)
            missing = assistant.check_transport(conn, records)
            self.assertEqual(missing["attention"], 1)
            self.assertIn("не задана", " ".join(records[0]["transport_findings"]))
            records[0].update(travel_rate=450, trips=-1)
            self.assertEqual(assistant.check_transport(conn, records)["attention"], 1)
            records[0]["fixed"] = True
            fixed = assistant.check_transport(conn, records)
            self.assertEqual((fixed["checked"], fixed["excluded"]), (0, 1))

    def test_buttons_preserve_admin_boundary_disabled_gate_and_duplicate_protection(self):
        payload = dict(plan=plan("transport_check"), operation_id=str(uuid.uuid4()))
        for role in ("user", "accountant"):
            self.assertEqual(self.server_client.post(self.base + "inspect", json=payload,
                             headers=self.headers(role)).status_code, 403)
        self.assertEqual(self.post("inspect", payload).status_code, 200)
        self.assertEqual(self.post("inspect", payload).status_code, 400)
        missing_object = self.post("inspect", dict(plan=plan("catalog_preview"), operation_id=str(uuid.uuid4())))
        self.assertEqual(missing_object.get_json()["clarification"]["field"], "object")
        self.assertEqual(self.post("inspect", dict(plan=plan("delete"),
                         operation_id=str(uuid.uuid4()))).status_code, 400)
        self.post("status", dict(enabled=False, limit_usd=10))
        self.assertEqual(self.post("inspect", dict(plan=plan(), operation_id=str(uuid.uuid4()))).status_code, 400)

    def test_specific_questions_and_unsupported_action(self):
        with patch("assistant_api.requests.post") as provider:
            response = self.post("command", dict(text="Проверь транспорт", operation_id=str(uuid.uuid4())))
            provider.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["clarification"]["field"], "period")
        for field in ("scope", "unsupported"):
            mock = self.response()
            mock.json.return_value["output"][0] = dict(type="function_call", name="request_clarification",
                                                       arguments=json.dumps(dict(field=field)))
            with patch("assistant_api.requests.post", return_value=mock):
                response = self.post("command", self.request_command())
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()["clarification"]["field"], field)
            self.assertNotIn("Уточните команду", response.get_json()["clarification"]["question"])

    def test_transport_variants_use_supported_function_and_specific_scope_question(self):
        with patch("assistant_api.requests.post") as provider, patch("assistant_api.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
            for text in ("Проверь транспорт на этой неделе для всех сотрудников",
                         "Проверьте клиентский транспорт за текущую неделю, ничего не изменяй.",
                         "Проверь транспорт за тот же период, ничего не меняй",
                         "Проверь транспорт за тот же период, ничего не меняйте.",
                         "Проверить транспорт за эту неделю",
                         "Проверь транспорт с 2026-10-19 по 2026-10-25 для сотрудника Synthetic Employee"):
                with self.subTest(text=text):
                    context = (dict(plan=plan("hours_summary", date_from="2026-10-19", date_to="2026-10-25"),
                                    pending="")
                               if "тот же период" in text else None)
                    payload = dict(text=text, operation_id=str(uuid.uuid4()))
                    if context is not None:
                        payload["context"] = context
                    response = self.post("command", payload)
                    self.assertEqual(response.status_code, 200, response.get_json())
                    chosen = response.get_json()["result"]["plan"]
                    self.assertEqual(chosen["purpose"], "transport_check")
                    self.assertEqual((chosen["date_from"], chosen["date_to"]), ("2026-10-19", "2026-10-25"))
            response = self.post("command", dict(text="Проверь транспорт за текущую неделю для одного сотрудника",
                                                 operation_id=str(uuid.uuid4())))
            self.assertEqual(response.get_json()["clarification"]["field"], "scope")
            context = dict(plan=plan("hours_summary", date_from="2026-10-19", date_to="2026-10-25"), pending="")
            response = self.post("command", dict(text="Проверь транспорт за тот же период",
                                                 operation_id=str(uuid.uuid4()), context=context))
            self.assertEqual(response.status_code, 200, response.get_json())
            reused = response.get_json()["result"]["plan"]
            self.assertEqual(reused["purpose"], "transport_check")
            self.assertEqual((reused["date_from"], reused["date_to"]), ("2026-10-19", "2026-10-25"))
            self.assertEqual(reused["employee"], "")
            provider.assert_not_called()
        self.assertIsNone(assistant.transport_command("Проверь транспорт за текущую неделю и удали дубликаты"))
        self.assertIsNone(assistant.transport_command("Проверь транспорт по объекту Hotel"))
        with self.assertRaises(assistant.AssistantError):
            assistant.transport_command("Проверь транспорт с 2026-99-19 по 2026-10-25")

    def test_no_change_suffix_does_not_hide_hours_or_anomaly_intent(self):
        self.fixture_shift()
        with patch("assistant_api.requests.post") as provider, patch("assistant_api.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
            for text, purpose in (
                ("Покажи, кто сколько отработал за эту неделю, ничего не меняй", "hours_summary"),
                ("Найди дубли и пропущенные ставки за эту неделю, ничего не меняй.", "anomalies"),
            ):
                with self.subTest(text=text):
                    response = self.post("command", dict(text=text, operation_id=str(uuid.uuid4())))
                    self.assertEqual(response.status_code, 200, response.get_json())
                    result = response.get_json()["result"]
                    self.assertEqual(result["plan"]["purpose"], purpose)
                    self.assertEqual((result["plan"]["date_from"], result["plan"]["date_to"]),
                                     ("2026-10-19", "2026-10-25"))
            provider.assert_not_called()

    def test_natural_weekly_hours_and_anomalies_keep_intent_and_period_without_model(self):
        self.fixture_shift()
        selection = plan("transport_check", date_from="2026-10-19", date_to="2026-10-25")
        context = dict(plan=selection, pending="")
        with patch("assistant_api.requests.post") as provider, \
                patch("assistant_api.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
            response = self.post("command", dict(text="Покажи, кто сколько отработал за эту неделю",
                                                 operation_id=str(uuid.uuid4())))
            self.assertEqual(response.status_code, 200, response.get_json())
            result = response.get_json()["result"]
            self.assertEqual(result["plan"]["purpose"], "hours_summary")
            self.assertEqual((result["plan"]["date_from"], result["plan"]["date_to"]),
                             ("2026-10-19", "2026-10-25"))
            self.assertEqual(len(result["records"]), 1)

            response = self.post("command", dict(text="Найди дубли и пропущенные ставки",
                                                 operation_id=str(uuid.uuid4()), context=context))
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()["result"]["plan"]["purpose"], "anomalies")
            self.assertEqual(response.get_json()["result"]["plan"]["date_from"], "2026-10-19")

            response = self.post("command", dict(text="Найди дубли и пропущенные ставки",
                                                 operation_id=str(uuid.uuid4())))
            self.assertEqual(response.get_json()["clarification"]["field"], "period")
            self.assertEqual(response.get_json()["clarification"]["plan"]["purpose"], "anomalies")
            provider.assert_not_called()

    def test_hours_followup_reuses_transport_period_and_returns_only_employee_totals(self):
        self.fixture_shift()
        with patch("assistant_api.requests.post") as provider:
            transport = self.post("command", dict(
                text="Проверь транспорт с 2026-10-19 по 2026-10-25",
                operation_id=str(uuid.uuid4()),
            ))
            self.assertEqual(transport.status_code, 200, transport.get_json())
            context = transport.get_json()["result"]["context"]
            self.assertEqual(
                (context["plan"]["date_from"], context["plan"]["date_to"]),
                ("2026-10-19", "2026-10-25"),
            )

            followup = self.post("command", dict(
                text=("Покажи, сколько часов отработал каждый сотрудник за тот же период. "
                      "Только итог по каждому и общий итог, без списка всех смен"),
                operation_id=str(uuid.uuid4()),
                context=context,
            ))
            self.assertEqual(followup.status_code, 200, followup.get_json())
            result = followup.get_json()["result"]

        self.assertEqual(result["plan"]["purpose"], "hours_summary")
        self.assertEqual(
            (result["plan"]["date_from"], result["plan"]["date_to"]),
            ("2026-10-19", "2026-10-25"),
        )
        self.assertEqual(result["table"]["headers"], ["Сотрудник", "Отработано, ч"])
        self.assertEqual(result["table"]["rows"], [
            ["Synthetic Employee", "5.0"],
            ["Общий итог", "5"],
        ])
        self.assertIn("Общий итог отработанного времени: 5 ч.", result["summary"])
        self.assertTrue(result["summary_only"])
        self.assertNotIn("assistant-five", repr(result["table"]))
        self.assertNotIn("смен", result["summary"].casefold())
        provider.assert_not_called()

        from assistant_ui import result_table, result_text
        headers, rows = result_table(result)
        self.assertEqual(headers, ["Сотрудник", "Отработано, ч"])
        self.assertEqual(rows, result["table"]["rows"])
        rendered = result_text(result)
        self.assertIn("2026-10-19 — 2026-10-25", rendered)
        self.assertIn("Общий итог отработанного времени: 5 ч.", rendered)
        self.assertNotIn("assistant-five", rendered)

        override = self.post("command", dict(
            text=("Покажи, сколько часов отработал каждый сотрудник с 20.10.2026 по 22.10.2026. "
                  "Только итог по каждому и общий итог, без списка всех смен"),
            operation_id=str(uuid.uuid4()),
            context=context,
        ))
        self.assertEqual(override.status_code, 200, override.get_json())
        override_result = override.get_json()["result"]
        self.assertEqual(
            (override_result["plan"]["date_from"], override_result["plan"]["date_to"]),
            ("2026-10-20", "2026-10-22"),
        )
        self.assertEqual(override_result["table"]["rows"], [["Общий итог", "0"]])
        provider.assert_not_called()

    def test_transport_and_anomaly_followups_keep_context_and_explain_scope(self):
        self.fixture_shift()
        context = dict(
            plan=plan("hours_summary", date_from="2026-10-19", date_to="2026-10-25"),
            pending="",
        )
        with patch("assistant_api.requests.post") as provider:
            transport = self.post("command", dict(
                text="А теперь проверь транспорт за тот же период",
                operation_id=str(uuid.uuid4()),
                context=context,
            ))
            self.assertEqual(transport.status_code, 200, transport.get_json())
            transport_result = transport.get_json()["result"]
            self.assertEqual(transport_result["plan"]["purpose"], "transport_check")
            self.assertEqual(
                (transport_result["plan"]["date_from"], transport_result["plan"]["date_to"]),
                ("2026-10-19", "2026-10-25"),
            )
            self.assertIn("Проверено: 1; требуют внимания: 0", __import__(
                "assistant_ui").result_text(transport_result))

            clear = self.post("command", dict(
                text="Найди дубли и проблемы со ставками за тот же период",
                operation_id=str(uuid.uuid4()),
                context=transport_result["context"],
            ))
            self.assertEqual(clear.status_code, 200, clear.get_json())
            clear_result = clear.get_json()["result"]
            self.assertEqual(clear_result["plan"]["purpose"], "anomalies")
            self.assertEqual(clear_result["checked_count"], 1)
            from assistant_ui import result_text
            clear_text = result_text(clear_result)
            self.assertIn("Дублей и проблем со ставками в проверенном охвате не найдено.", clear_text)
            self.assertNotIn("записей нет", clear_text)
            self.assertNotIn("assistant-five", clear_text)

            with closing(self.server.get_db_connection()) as conn:
                conn.execute("UPDATE meta_objects SET rate=600 WHERE company='Renatur' AND name='Hotel'")
                conn.execute("""INSERT INTO incoming_submissions
                    (sync_id,request_id,date,employee,company,object_name,hours,rate,transport,
                     submission_type,created_at,updated_at)
                    VALUES('assistant-duplicate','assistant-duplicate','2026-10-23',
                           'Synthetic Employee','Renatur','Hotel',5,520,0,'work','2026-10-23','2026-10-23')""")
                conn.commit()
            issues = self.post("command", dict(
                text="Найди дубли и проблемы со ставками за тот же период",
                operation_id=str(uuid.uuid4()),
                context=transport_result["context"],
            ))
            self.assertEqual(issues.status_code, 200, issues.get_json())
            issue_result = issues.get_json()["result"]

        self.assertEqual(issue_result["checked_count"], 2)
        self.assertEqual(len(issue_result["records"]), 2)
        self.assertIn("ставка отличается от справочника: 2", issue_result["summary"])
        self.assertIn("возможные дубли: 2 записей", issue_result["summary"])
        from assistant_ui import result_table, result_text
        headers, rows = result_table(issue_result)
        self.assertIn("Ставка справочника, SEK/ч", headers)
        self.assertEqual(len(headers), len(rows[0]))
        output = result_text(issue_result)
        self.assertIn("Проверено записей: 2.", output)
        self.assertIn("Требующие внимания записи перечислены в таблице.", output)
        self.assertNotIn("assistant-duplicate", output)
        self.assertNotIn("список смен", output.casefold())
        provider.assert_not_called()

    def test_explicit_dates_override_followup_period_in_european_format(self):
        context = dict(
            plan=plan("transport_check", date_from="2026-10-19", date_to="2026-10-25"),
            pending="",
        )
        parsed = assistant.recognized_read_command(
            "Покажи, сколько часов отработал каждый сотрудник с 20.10.2026 по 22.10.2026",
            context,
        )
        self.assertEqual(parsed["purpose"], "hours_summary")
        self.assertEqual((parsed["date_from"], parsed["date_to"]), ("2026-10-20", "2026-10-22"))

    def test_general_transport_router_documents_real_capability_and_requires_a_tool(self):
        with patch("assistant_api.requests.post", return_value=self.response(plan("transport_check"))) as provider:
            response = self.post("command", dict(text="Сверь цены выезда с текущим справочником за октябрь 2026",
                                                 operation_id=str(uuid.uuid4())))
        self.assertEqual(response.status_code, 200)
        body = provider.call_args.kwargs["json"]
        self.assertEqual(body["tool_choice"], "required")
        self.assertIn("ARE SUPPORTED", body["instructions"])
        self.assertEqual(response.get_json()["result"]["plan"]["purpose"], "transport_check")

    def test_integrated_hours_summary_does_not_count_ob_twice_or_special_time_as_work(self):
        self.fixture_shift()
        with closing(self.server.get_db_connection()) as conn:
            conn.execute("UPDATE incoming_submissions SET submission_type='ob'")
            conn.execute("""INSERT INTO incoming_submissions(sync_id,request_id,date,employee,company,object_name,
                         hours,rate,transport,submission_type,created_at,updated_at)
                         VALUES('assistant-holiday','assistant-holiday','2026-10-23','Synthetic Employee',
                         'Renatur','Hotel',8,0,0,'holiday','2026-10-23','2026-10-23')""")
            conn.commit()
        before = self.business()
        response = self.post("inspect", dict(plan=plan("hours_summary"), operation_id=str(uuid.uuid4())))
        self.assertEqual(response.status_code, 200, response.get_json())
        result = response.get_json()["result"]
        self.assertEqual(result["table"]["rows"], [["Synthetic Employee", "5.0", "8.0", 2]])
        self.assertIn("OB входит", result["summary"])
        self.assertEqual(before, self.business())

    def test_integrated_directory_resolution_infers_company_and_asks_ambiguous_object(self):
        self.fixture_shift()
        response = self.post("inspect", dict(plan=plan("customer_pdf", object_name="hot"),
                                             operation_id=str(uuid.uuid4())))
        self.assertEqual(response.get_json()["result"]["plan"]["company"], "Renatur")
        self.assertEqual(response.get_json()["result"]["plan"]["object_name"], "Hotel")
        with closing(self.server.get_db_connection()) as conn:
            conn.execute("INSERT INTO meta_objects(company,name,rate,transport_rate,is_active) VALUES('Renatur','Hotel Two',520,450,1)")
            conn.commit()
        question = self.post("inspect", dict(plan=plan("customer_pdf", object_name="hot"),
                                             operation_id=str(uuid.uuid4()))).get_json()["clarification"]
        self.assertEqual(question["field"], "object")
        self.assertEqual(question["plan"]["purpose"], "customer_pdf")
        self.assertEqual(len(question["choices"]), 2)
        self.assertNotIn("id", repr(question["choices"]))
        selected = question["plan"] | question["choices"][1]
        result = self.post("inspect", dict(plan=selected, operation_id=str(uuid.uuid4()))).get_json()["result"]
        self.assertEqual(result["plan"]["object_name"], "Hotel Two")

    def test_integrated_conversation_passes_only_selection_and_preserves_report_follow_up(self):
        self.fixture_shift()
        context = dict(plan=plan("customer_pdf", company="Renatur", object_name="Hotel"), pending="object")
        chosen = plan("customer_pdf", object_name="Hotel")
        with patch("assistant_api.requests.post", return_value=self.response(chosen)) as provider:
            response = self.post("command", self.request_command() | dict(text="По этому объекту за тот же период",
                                                                         context=context))
        self.assertEqual(response.status_code, 200, response.get_json())
        forwarded = json.loads(provider.call_args.kwargs["json"]["input"])
        self.assertEqual(forwarded["selection"], context)
        self.assertEqual(set(forwarded), {"request", "selection"})
        self.assertNotIn("records", repr(forwarded))
        self.assertNotIn("customer_rate", repr(forwarded))
        self.assertEqual(response.get_json()["result"]["plan"]["purpose"], "customer_pdf")
        with patch("assistant_api.requests.post") as provider:
            response = self.post("command", self.request_command() | dict(context={"messages": ["secret"]}))
            self.assertEqual(response.status_code, 400)
            provider.assert_not_called()

    def test_integrated_clarification_keeps_requested_report_and_known_period(self):
        selection = plan("customer_pdf")
        reply = self.response()
        reply.json.return_value["output"] = [dict(type="function_call", name="request_clarification",
                                                arguments=json.dumps(dict(field="object", selection=selection)))]
        with patch("assistant_api.requests.post", return_value=reply):
            response = self.post("command", self.request_command())
        question = response.get_json()["clarification"]
        self.assertEqual(question["plan"], selection)
        self.assertEqual(question["field"], "object")

    def test_integrated_transport_keeps_selection_and_explicit_all_clears_person(self):
        self.fixture_shift()
        selected = plan("customer_pdf", employee="Synthetic Employee", company="Renatur", object_name="Hotel")
        context = dict(plan=selected, pending="")
        with patch("assistant_api.requests.post") as provider:
            response = self.post("command", self.request_command() | dict(text="Проверь транспорт", context=context))
            result = response.get_json()["result"]
            self.assertEqual(result["plan"], selected | dict(purpose="transport_check"))
            response = self.post("command", self.request_command() | dict(
                text="Проверь транспорт для всех сотрудников", context=context))
            self.assertEqual(response.get_json()["result"]["plan"]["employee"], "")
            question = self.post("command", self.request_command() | dict(
                text="Проверь транспорт для одного сотрудника", context=context)).get_json()["clarification"]
            self.assertEqual(question["plan"]["date_from"], selected["date_from"])
            self.assertEqual(question["plan"]["purpose"], "transport_check")
            provider.assert_not_called()

    def test_integrated_malformed_context_never_contacts_provider(self):
        with patch("assistant_api.requests.post") as provider:
            for context in (dict(plan=[], pending=""), dict(plan=plan(), pending=[]),
                            dict(plan="not a plan", pending=""), dict(plan=plan(), pending=None)):
                with self.subTest(context=context):
                    response = self.post("command", self.request_command() | dict(context=context))
                    self.assertEqual(response.status_code, 400, response.get_json())
            provider.assert_not_called()

    def test_integrated_workflow_ui_retains_each_table_and_voice_followup_selection(self):
        from assistant_ui import AssistantDialog, RequestWorker
        from PyQt6.QtWidgets import QWidget
        self.fixture_shift()
        results = [self.post("inspect", dict(plan=plan(action), operation_id=str(uuid.uuid4()))).get_json()["result"]
                   for action in ("hours_summary", "anomalies", "transport_check")]
        with patch.object(RequestWorker, "start"):
            dialog = AssistantDialog(QWidget())
            self.addCleanup(dialog.close)
            dialog.finished()
            dialog.received(dict(results=results))
            self.assertEqual(dialog.workflow_choice.count(), 3)
            self.assertEqual(dialog.result["plan"]["purpose"], "transport_check")
            transcript = dialog.output.toPlainText()
            self.assertIn("Выполнено шагов: 3", transcript)
            self.assertNotIn("Проверено: 1;", transcript)
            dialog.workflow_choice.setCurrentIndex(0)
            self.assertEqual(dialog.table.horizontalHeaderItem(1).text(), "Отработано, ч")
            self.assertEqual(dialog.context["plan"]["purpose"], "hours_summary")
            self.assertTrue(dialog.result_heading.text().startswith("Кто сколько отработал"))
            preview = os.environ.get("SMARTREPORT_QA_ASSISTANT_PREVIEW")
            if preview:
                from pathlib import Path
                from PyQt6.QtWidgets import QApplication
                self.assertTrue(Path(preview).resolve().is_relative_to(Path(os.environ["SMARTREPORT_QA_ROOT"]).resolve()))
                dialog.show()
                QApplication.processEvents()
                self.assertTrue(dialog.grab().save(preview))
            history = dialog.output.toPlainText()
            dialog.workflow_choice.setCurrentIndex(1)
            self.assertEqual(dialog.output.toPlainText(), history)
            self.assertEqual(dialog.table.rowCount(), 0)
            self.assertTrue(dialog.result_heading.text().startswith("Дубли и смены без клиентской ставки"))
            dialog.workflow_choice.setCurrentIndex(0)
            dialog.received(dict(text="А теперь за прошлую неделю"))
            self.assertEqual(dialog.worker.path, "status")
            dialog.command()
            self.assertEqual(dialog.worker.payload["context"]["plan"]["purpose"], "hours_summary")
            self.assertEqual(dialog.worker.payload["text"], "А теперь за прошлую неделю")
            dialog.new_conversation()
            self.assertIsNone(dialog.context)
            self.assertEqual(dialog.employee.text(), "")
            self.assertEqual(dialog.object_name.text(), "")
            self.assertTrue(dialog.workflow_choice.isHidden())

    def test_integrated_native_cards_exports_and_expense_filters_use_correct_identity(self):
        from assistant_desktop import native_action, DesktopActionError
        from PyQt6.QtWidgets import QMainWindow, QTabWidget, QWidget, QLineEdit, QComboBox, QCheckBox
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        QMainWindow.__init__(window)
        self.addCleanup(window.close)
        window.tabs = QTabWidget(window)
        window.tab_expenses = QWidget()
        window.tabs.addTab(window.tab_expenses, "Expenses")
        window.exp_date_from, window.exp_date_to = QLineEdit(), QLineEdit()
        window.exp_filter_object = QComboBox()
        window.exp_filter_object.addItem("All", None)
        window.exp_filter_object.addItem("Remote object", 1937)
        window.exp_filter_unallocated = QCheckBox()
        window.exp_filter_unallocated.setChecked(True)
        result = dict(plan=plan("expense_documents", company="Renatur", object_name="Hotel"),
                      native_action="expense_documents", object=dict(id=1937))
        with patch.object(window, "load_expense_documents") as refresh:
            native_action(window, result, window)
        refresh.assert_called_once()
        self.assertEqual(window.exp_filter_object.currentData(), 1937)
        self.assertFalse(window.exp_filter_unallocated.isChecked())
        self.assertEqual(window.exp_date_from.text(), "2026-10-01")
        result["plan"]["employee"] = "Synthetic Employee"
        with self.assertRaisesRegex(DesktopActionError, "фильтр сотрудника"):
            native_action(window, result, window)
        for action in ("employee_card", "accountant_export"):
            with patch("employee_cards_ui.CardClient") as client, \
                    patch("employee_cards_ui.EmployeeCardDialog") as card_dialog, \
                    patch("employee_cards_ui.export_records") as export:
                client.return_value.resolve.return_value = dict(id=999, name="Synthetic Employee")
                native_action(window, dict(plan=plan(action, employee="Synthetic Employee"),
                                          native_action=action), window)
                client.return_value.resolve.assert_called_once()
                self.assertEqual(client.return_value.resolve.call_args.args[0], self.local_id)
                if action == "employee_card":
                    card_dialog.return_value.exec.assert_called_once()
                    export.assert_not_called()
                else:
                    export.assert_called_once_with(client.return_value, [999], window)
                    card_dialog.assert_not_called()

    def test_integrated_payroll_history_preserves_person_object_and_period_without_saving(self):
        from assistant_desktop import native_action
        from PyQt6.QtWidgets import QMainWindow, QLineEdit, QComboBox, QDateEdit
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        QMainWindow.__init__(window)
        self.addCleanup(window.close)
        window.sal_date_from, window.sal_date_to = QLineEdit(), QLineEdit()
        with closing(self.desktop.get_db_connection()) as conn:
            conn.execute("INSERT INTO employees(name) VALUES('Second Synthetic Employee')")
            conn.execute("INSERT INTO objects(company,name,rate,transport_rate) VALUES('Renatur','Assistant Site',520,450)")
            conn.commit()
            before = list(map(tuple, conn.execute("SELECT * FROM balance")))
        captured = {}

        def inspect_dialog(dialog):
            captured["employee"] = dialog.findChild(QComboBox, "payroll_employee").currentText()
            captured["object"] = tuple(dialog.findChild(QComboBox, "payroll_object").currentData())
            captured["start"] = dialog.findChild(QDateEdit, "payroll_start").text()
            captured["end"] = dialog.findChild(QDateEdit, "payroll_end").text()
            return 0

        result = dict(plan=plan("payroll_history", employee="Second Synthetic Employee",
                               company="Renatur", object_name="Assistant Site"), native_action="payroll_history")
        with patch("payroll_ui.QDialog.exec", new=inspect_dialog):
            native_action(window, result, window)
        self.assertEqual(captured, dict(employee="Second Synthetic Employee", object=("Renatur", "Assistant Site"),
                                        start="2026-10-01", end="2026-10-31"))
        with closing(self.desktop.get_db_connection()) as conn:
            self.assertEqual(before, list(map(tuple, conn.execute("SELECT * FROM balance"))))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM payroll_events").fetchone()[0], 0)

    def test_integrated_capabilities_and_native_actions_never_write_business_data(self):
        self.fixture_shift()
        before = self.business()
        response = self.server_client.get(self.base + "capabilities", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        from assistant_actions import CAPABILITIES, NATIVE_ACTIONS
        self.assertEqual({r["action"] for r in response.get_json()["capabilities"]}, set(CAPABILITIES))
        with patch("assistant_api.requests.post") as provider:
            for action in NATIVE_ACTIONS:
                with self.subTest(action=action):
                    selected = plan(action, employee="Synthetic Employee" if action in {"employee_card", "accountant_export"} else "",
                                    company="Renatur" if action == "invoice_period" else "",
                                    object_name="Hotel" if action == "invoice_period" else "")
                    result = self.post("inspect", dict(plan=selected, operation_id=str(uuid.uuid4())))
                    self.assertEqual(result.status_code, 200, result.get_json())
                    self.assertEqual(result.get_json()["result"]["native_action"], action)
            provider.assert_not_called()
        self.assertEqual(before, self.business())
        for role in ("user", "accountant"):
            self.assertEqual(self.server_client.get(self.base + "capabilities", headers=self.headers(role)).status_code, 403)

    def test_integrated_directories_have_no_private_cards_or_bank_fields(self):
        self.fixture_shift()
        for action in ("employees", "objects"):
            result = self.post("inspect", dict(plan=plan(action), operation_id=str(uuid.uuid4()))).get_json()["result"]
            self.assertTrue(result["table"]["rows"])
            text = json.dumps(result)
            for private in ("personnummer", "iban", "clearing", "bank_account", "encrypted"):
                self.assertNotIn(private, text.casefold())

    def test_integrated_workflow_reads_one_snapshot_and_cannot_chain_native_writes(self):
        self.fixture_shift()
        before = self.business()
        reply = self.response()
        reply.json.return_value["output"] = [dict(type="function_call", name="run_workflow",
            arguments=json.dumps(dict(steps=[plan("hours_summary"), plan("anomalies"), plan("transport_check")])))]
        with patch("assistant_api.requests.post", return_value=reply) as provider:
            response = self.post("command", self.request_command())
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(provider.call_count, 1)
        results = response.get_json()["results"]
        self.assertEqual([r["plan"]["purpose"] for r in results], ["hours_summary", "anomalies", "transport_check"])
        self.assertEqual(len(results[1]["records"]), 0)
        self.assertEqual(results[2]["transport"]["checked"], 1)
        self.assertEqual(before, self.business())
        reply.json.return_value["output"][0]["arguments"] = json.dumps(dict(
            steps=[plan("hours_summary"), plan("invoice_period", company="Renatur", object_name="Hotel")]))
        with patch("assistant_api.requests.post", return_value=reply):
            bad = self.post("command", self.request_command())
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(before, self.business())
        reply.json.return_value["output"][0]["arguments"] = json.dumps(dict(steps=[plan()] * 5))
        with patch("assistant_api.requests.post", return_value=reply):
            self.assertEqual(self.post("command", self.request_command()).status_code, 400)

    def test_integrated_local_balance_and_expenses_are_query_only_and_do_not_recalculate(self):
        from assistant_desktop import local_result
        from PyQt6.QtWidgets import QMainWindow
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        QMainWindow.__init__(window)
        self.addCleanup(window.close)
        with closing(self.desktop.get_db_connection()) as conn:
            conn.execute("INSERT INTO balance(date,employee,description,change_amount) VALUES('2026-09-01','Synthetic Employee','Old',100)")
            conn.execute("INSERT INTO balance(date,employee,description,change_amount) VALUES('2026-10-23','Synthetic Employee','Paid',-30)")
            conn.execute("INSERT INTO company_expenses(date,category,description,amount) VALUES('2026-10-23','Synthetic','Test',20)")
            conn.commit()
            before = {name: list(map(tuple, conn.execute("SELECT * FROM " + name))) for name in ("balance", "company_expenses")}
        for action in ("balance", "expenses"):
            remote = dict(plan=plan(action), native_action=action, source="server", records=[], notice="No writes")
            result = local_result(window, remote)
            self.assertEqual(len(result["table"]["rows"]), 2 if action == "balance" else 1)
            self.assertNotIn("native_action", result)
            if action == "balance":
                self.assertIn("170.00", result["summary"])
        with closing(self.desktop.get_db_connection()) as conn:
            after = {name: list(map(tuple, conn.execute("SELECT * FROM " + name))) for name in ("balance", "company_expenses")}
        self.assertEqual(before, after)

    def test_button_to_table_and_voice_to_editable_text_then_explicit_command(self):
        from assistant_ui import AssistantDialog, RequestWorker
        from PyQt6.QtCore import QDate
        from PyQt6.QtWidgets import QWidget, QPushButton
        self.fixture_shift()
        before = self.business()
        with patch.object(RequestWorker, "start"):
            dialog = AssistantDialog(QWidget())
            self.addCleanup(dialog.close)
            dialog.finished()
            dialog.date_from.setDate(QDate(2026, 10, 19))
            dialog.date_to.setDate(QDate(2026, 10, 25))

            def local_api(path, payload=None, audio=None, database=None):
                if audio is not None:
                    reply = self.server_client.post(self.base + path, data=audio,
                        headers=self.headers() | {"X-Operation-ID": str(uuid.uuid4())})
                else:
                    reply = self.post(path, payload)
                self.assertEqual(reply.status_code, 200, reply.get_json())
                return reply.get_json()

            with patch("assistant_ui.request_api", side_effect=local_api) as api:
                button = next(b for b in dialog.findChildren(QPushButton) if b.text() == "Проверить транспорт")
                button.click()
                dialog.worker.run()
                dialog.finished()
                self.assertEqual(dialog.table.rowCount(), 1)
                self.assertIn("требуют внимания: 0", dialog.output.toPlainText())
                self.assertEqual(dialog.table.item(0, 1).text(), "Synthetic Employee")
                self.assertNotIn("assistant-five", dialog.output.toPlainText())
                dialog.audio = Mock()
                dialog.buffer = Mock()
                dialog.buffer.data.return_value = b"\x00" * 32000
                dialog.stop_recording()
                self.assertEqual(dialog.worker.path, "transcribe")
                audio_response = Mock(status_code=200, json=Mock(return_value={
                    "text": "А теперь проверь транспорт за тот же период"}))
                with patch("assistant_api.requests.post", return_value=audio_response):
                    dialog.worker.run()
                dialog.finished()
                self.assertEqual(dialog.input.toPlainText(), "А теперь проверь транспорт за тот же период")
                self.assertEqual(api.call_count, 2)  # Transcription did not execute a command.
                previous_context = dialog.context.copy()
                dialog.command()
                self.assertEqual(dialog.worker.path, "command")
                self.assertEqual(dialog.worker.payload["context"]["plan"], previous_context["plan"])
                dialog.worker.run()
                dialog.finished()
                self.assertEqual(api.call_count, 3)
                self.assertEqual(api.call_args.args[0], "command")
                self.assertEqual(dialog.result["plan"]["purpose"], "transport_check")
                self.assertEqual(
                    (dialog.result["plan"]["date_from"], dialog.result["plan"]["date_to"]),
                    (previous_context["plan"]["date_from"], previous_context["plan"]["date_to"]),
                )
                self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(before, self.business())

    def test_actual_saved_prices_five_hours_2600_and_no_transport(self):
        self.fixture_shift()
        before = self.business()
        chosen = plan("explain", company="Renatur", object_name="Hotel")
        with closing(self.server.get_db_connection()) as conn:
            assistant.inspect(conn, chosen)
        with patch("assistant_api.requests.post", return_value=self.response(chosen)):
            response = self.post("command", self.request_command())
        self.assertEqual(response.status_code, 200, response.get_json())
        record = response.get_json()["result"]["records"][0]
        self.assertEqual((record["hours"], record["customer_rate"], record["base_only"]), (5, 520, 2600))
        self.assertEqual((record["travel_rate"], record["travel_only"]), (450, 0))
        self.assertTrue(any("времени" in w for w in record["warnings"]))
        self.assertEqual(before, self.business())
        from assistant_ui import result_text
        text = result_text(response.get_json()["result"])
        self.assertIn("2,600.00", text)
        self.assertIn("не итог", text)

    def test_catalog_preview_is_read_only_and_closed_records_are_marked(self):
        self.fixture_shift()
        with closing(self.server.get_db_connection()) as conn:
            conn.execute("UPDATE incoming_submissions SET invoiced=1")
            conn.commit()
        before = self.business()
        with patch("assistant_api.requests.post", return_value=self.response(plan("catalog_preview", company="Renatur", object_name="Hotel"))):
            response = self.post("command", self.request_command())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("защищена" in w for w in response.get_json()["result"]["records"][0]["warnings"]))
        self.assertEqual(before, self.business())

    def test_native_payroll_pdf_uses_correct_employee_and_existing_renderer(self):
        from assistant_ui import open_report
        from PyQt6.QtWidgets import QComboBox, QLineEdit, QMainWindow
        from pypdf import PdfReader
        import payroll_ui
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        QMainWindow.__init__(window)
        self.addCleanup(window.close)
        window.sal_employee_filter = QComboBox()
        window.sal_employee_filter.addItem("Everyone", None)
        window.sal_employee_filter.addItem("Synthetic Employee", "Synthetic Employee")
        window.sal_report_mode = QComboBox()
        window.sal_report_mode.addItem("management", "management")
        window.sal_date_from, window.sal_date_to = QLineEdit(), QLineEdit()
        result = dict(plan=plan("payroll_pdf", employee="Synthetic Employee"), records=[])
        with patch.object(window, "load_salary_table_template") as refresh, \
                patch("payroll_ui.preview") as export:
            open_report(window, result)
        refresh.assert_called_once()
        export.assert_called_once()
        self.assertEqual(window.sal_employee_filter.currentData(), "Synthetic Employee")
        self.assertEqual(window.sal_date_from.text(), "2026-10-01")
        # Independently exercise the existing vector PDF renderer, without changing financial rules.
        stream = BytesIO()
        import payroll_management
        with closing(self.desktop.get_db_connection()) as conn:
            conn.execute("""INSERT INTO shifts(sync_id,date,employee,company,object_name,hours,rate,transport,entry_type)
                         VALUES('payroll-five','2026-10-23','Synthetic Employee','Renatur','Hotel',5,520,0,'work')""")
            conn.commit()
            items = payroll_management.reports(conn.cursor(), "2026-10-01", "2026-10-31",
                                               self.desktop.swedish_public_holidays, self.desktop.swedish_workday_norm)
        payroll_ui.write_pdf(stream, items, self.desktop.DEFAULT_FONT, self.desktop.DEFAULT_FONT_BOLD)
        pdf = PdfReader(stream)
        self.assertEqual(len(pdf.pages), 1)
        self.assertIn("Synthetic Employee", pdf.pages[0].extract_text())

    def test_native_pdf_rejects_stale_saved_rate_even_when_hours_match(self):
        from assistant_ui import open_report, AssistantClientError
        from PyQt6.QtWidgets import QMainWindow
        from customer_pricing import row_prices
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        QMainWindow.__init__(window)
        self.addCleanup(window.close)
        with closing(self.desktop.get_db_connection()) as conn:
            conn.execute("""INSERT INTO shifts(sync_id,date,employee,company,object_name,hours,rate,transport,entry_type)
                         VALUES('stale','2026-10-23','Synthetic Employee','Renatur','Hotel',5,520,0,'work')""")
            conn.commit()
            row = dict(conn.execute("SELECT * FROM shifts WHERE sync_id='stale'").fetchone())
        record = dict(id="stale", hours=5, prices=row_prices(row), trips=0, entry_type="work",
                      start_time=row.get("start_time"), end_time=row.get("end_time"), invoiced=False, intervals=[])
        record["prices"] = record["prices"] | {"base": 300}
        with self.assertRaisesRegex(AssistantClientError, "отличаются"):
            open_report(window, dict(plan=plan("payroll_pdf"), records=[record]))

    def test_voice_truncated_wav_and_invalid_pdf_controlled_without_external_calls(self):
        audio = BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0"*1600)
        with patch("assistant_api.requests.post") as provider:
            response = self.server_client.post(self.base + "transcribe", data=audio.getvalue()[:-10],
                                               headers=self.headers() | {"X-Operation-ID": str(uuid.uuid4())})
            self.assertEqual(response.status_code, 400)
            provider.assert_not_called()
        with self.smtp_env():
            response = self.post("mail/prepare", self.mail() | {"pdf": base64.b64encode(b"%PDF-broken").decode()})
            self.assertEqual(response.status_code, 400)

    def test_client_uses_window_database_when_environment_variable_is_absent(self):
        from assistant_ui import request_api
        from sync_dataset import HEADER, file_dataset
        database = self.desktop.DB_FILE
        with closing(self.desktop.get_db_connection()) as conn:
            conn.execute("CREATE TABLE sync_dataset(id INTEGER PRIMARY KEY,dataset_id TEXT)")
            conn.execute("INSERT INTO sync_dataset VALUES(1,?)", (str(uuid.uuid4()),))
            conn.commit()
        response = Mock(status_code=200, headers={HEADER: file_dataset(database)},
                        json=Mock(return_value={"ok": True, "status": {}}))
        with patch.dict(os.environ):
            os.environ.pop("SMARTREPORT_DB_FILE", None)
            with patch("assistant_ui.desktop_auth.current_token", return_value="synthetic-token"), \
                    patch("assistant_ui.desktop_auth.server_url", return_value="http://127.0.0.1:5999"), \
                    patch("assistant_ui.requests.request", return_value=response) as provider:
                self.assertTrue(request_api("status", database=database)["ok"])
                provider.assert_called_once()
                self.assertEqual(provider.call_args.kwargs["headers"][HEADER], file_dataset(database))

    def test_mail_expired_disabled_and_mismatched_dataset_are_rejected(self):
        from sync_dataset import HEADER
        with closing(self.server.get_db_connection()) as conn:
            conn.execute("CREATE TABLE sync_dataset(id INTEGER PRIMARY KEY,dataset_id TEXT)")
            conn.execute("INSERT INTO sync_dataset VALUES(1,?)", (str(uuid.uuid4()),))
            conn.commit()
        response = self.server_client.get(self.base + "status", headers=self.headers() | {HEADER: str(uuid.uuid4())})
        self.assertEqual(response.status_code, 409)
        with closing(self.server.get_db_connection()) as conn:
            conn.execute("DROP TABLE sync_dataset")
            conn.commit()
        with self.smtp_env(), patch("assistant_api.smtplib.SMTP") as smtp:
            payload = self.mail()
            draft = self.post("mail/prepare", payload).get_json()["draft"]
            with closing(self.server.get_db_connection()) as conn:
                conn.execute("UPDATE assistant_mail_drafts SET created_at=0 WHERE id=?", (draft["draft_id"],))
                conn.commit()
            self.assertEqual(self.post("mail/confirm", dict(draft_id=draft["draft_id"], payload=payload, confirmed=True)).status_code, 400)
            self.post("status", dict(enabled=False, limit_usd=10))
            self.assertEqual(self.post("mail/prepare", payload).status_code, 400)
            smtp.assert_not_called()


class AssistantUiTests(unittest.TestCase):
    setUpClass = classmethod(cards.EmployeeCardTests.setUpClass.__func__)
    tearDownClass = classmethod(cards.EmployeeCardTests.tearDownClass.__func__)

    def test_local_database_failure_leaves_assistant_open_and_does_not_claim_pdf_success(self):
        from assistant_ui import AssistantDialog, RequestWorker
        from PyQt6.QtWidgets import QWidget
        with patch.object(RequestWorker, "start"):
            dialog = AssistantDialog(QWidget())
        self.addCleanup(dialog.close)
        dialog.result = dict(plan=plan("payroll_pdf"), records=[])
        with patch("assistant_ui.open_report", side_effect=sqlite3.OperationalError("synthetic-private-details")), \
                patch("assistant_ui.QMessageBox.warning") as notification:
            dialog.report()
        notification.assert_called_once()
        self.assertIn("PDF не сформирован", dialog.output.toPlainText())
        self.assertNotIn("synthetic-private-details", dialog.output.toPlainText())

    def test_missing_server_key_error_does_not_close_panel_and_plain_text_output(self):
        from assistant_ui import AssistantDialog, RequestWorker, result_text
        from PyQt6.QtWidgets import QWidget
        with patch.object(RequestWorker, "start"):
            dialog = AssistantDialog(QWidget())
        self.addCleanup(dialog.close)
        dialog.failed("API-ключ не настроен")
        self.assertIn("не настроен", dialog.state.text())
        self.assertFalse(dialog.pdf.isEnabled())
        self.assertFalse(dialog.confirm_mail.isEnabled())
        rendered = result_text(dict(plan=plan(), source="Test", notice="No changes", records=[]))
        self.assertIn("записей нет", rendered)

    def test_connected_candidate_blocks_mutating_and_mail_capabilities(self):
        from assistant_ui import AssistantDialog, RequestWorker
        from PyQt6.QtWidgets import QWidget

        CandidateWindow = type("CandidateWindow", (QWidget,), {"__module__": self.desktop.__name__})
        window = CandidateWindow()
        with patch.object(self.desktop, "ASSISTANT_CANDIDATE", True), patch.object(RequestWorker, "start"):
            dialog = AssistantDialog(window)
        self.addCleanup(dialog.close)
        self.addCleanup(window.close)
        self.assertTrue(dialog.assistant_candidate)
        actions = {dialog.action_choice.itemData(i) for i in range(dialog.action_choice.count())}
        self.assertIn("hours_summary", actions)
        self.assertIn("transport_check", actions)
        self.assertIn("customer_pdf", actions)
        self.assertNotIn("catalog_preview", actions)
        self.assertNotIn("directories", actions)
        self.assertFalse(dialog.mail_button.isEnabled())
        self.assertFalse(dialog.confirm_mail.isEnabled())
        self.assertFalse(dialog.native.isEnabled())
        self.assertTrue(dialog.mail_button.isHidden())
        self.assertTrue(dialog.native.isHidden())

        with patch.object(dialog, "call") as api:
            dialog.prepare_mail()
            dialog.send_mail()
        self.assertIn("отключ", dialog.state.text())
        self.assertEqual(api.call_count, 0)

        dialog.quick_action("catalog_preview")
        self.assertIn("только чтение", dialog.state.text())
        self.assertFalse(dialog.pdf.isEnabled())

    def test_dialog_is_chat_with_private_diagnostics_and_context_followup(self):
        from assistant_ui import AssistantDialog, RequestWorker
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QWidget
        window = QWidget()
        with patch.object(RequestWorker, "start"):
            dialog = AssistantDialog(window)
            self.addCleanup(dialog.close)
            dialog.finished()
            dialog.input.setPlainText("Кто сколько отработал?")
            dialog.command()
            first = dialog.worker.payload
            self.assertIn("context", first)
            self.assertIn("Кто сколько отработал?", dialog.output.toPlainText())
            self.assertTrue(dialog.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint)
            selected = plan("hours_summary", company="Renatur", object_name="Hotel")
            dialog.received(dict(result=dict(plan=selected, source="Test", notice="No writes", records=[],
                                             table=dict(headers=["Employee", "Hours"], rows=[["Synthetic Employee", "5"]]),
                                             summary="Отработано 5 часов.")))
            dialog.finished()
            dialog.input.setPlainText("А теперь по другому объекту за тот же период")
            dialog.command()
            self.assertEqual(dialog.worker.payload["context"]["plan"], selected)
            dialog.received(dict(clarification=dict(field="object", question="Какой объект?",
                             plan=plan("customer_pdf"), choices=[dict(company="Renatur", object_name="Hotel")])))
            dialog.finished()
            dialog.input.setPlainText("1")
            dialog.command()
            self.assertEqual(dialog.worker.path, "inspect")
            self.assertEqual(dialog.worker.payload["plan"]["purpose"], "customer_pdf")
            self.assertEqual(dialog.worker.payload["plan"]["object_name"], "Hotel")
            self.assertIn("Кто сколько отработал?", dialog.output.toPlainText())
            self.assertIn("Какой объект?", dialog.output.toPlainText())
            dialog.received(dict(status=dict(enabled=True, limit_usd=10, reserved_usd=0.1,
                                            key_configured=True, model="gpt-5-mini", month="2026-10", notice="Test")))
            self.assertNotIn("gpt-5-mini", dialog.state.text())
            self.assertIn("gpt-5-mini", dialog.diagnostic_state.text())
            self.assertFalse(dialog.diagnostics.isVisible())
            dialog.new_conversation()
            self.assertIsNone(dialog.context)
            self.assertEqual(dialog.output.toPlainText(), "")
