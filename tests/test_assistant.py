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
        self.assertEqual(response.status_code, 400)
        self.assertIn("однозначно", response.get_json()["error"])

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
        self.assertEqual(self.post("inspect", dict(plan=plan("catalog_preview"),
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
                    "text": "Проверь транспорт за текущую неделю"}))
                with patch("assistant_api.requests.post", return_value=audio_response):
                    dialog.worker.run()
                dialog.finished()
                self.assertEqual(dialog.input.toPlainText(), "Проверь транспорт за текущую неделю")
                self.assertEqual(api.call_count, 2)  # Transcription did not execute a command.
                with patch("assistant_api.datetime", wraps=datetime) as clock:
                    clock.now.return_value = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
                    dialog.send.click()
                    dialog.worker.run()
                dialog.finished()
                self.assertEqual(api.call_count, 3)
                self.assertEqual(dialog.result["plan"]["purpose"], "transport_check")
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
