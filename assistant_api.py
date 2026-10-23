"""Allowlisted director assistant. No SQL, credentials or financial writes from a model."""
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
from email.message import EmailMessage
from email.utils import make_msgid
import base64
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import smtplib
import ssl
import uuid
import wave

import requests
from flask import jsonify, request
from pypdf.errors import PyPdfError

from customer_pricing import row_prices
from customer_report import money

MODEL = "gpt-5-mini"
MAX_OUTPUT = 1024
INPUT_MICRO_USD = Decimal("0.25")
OUTPUT_MICRO_USD = Decimal("2")
TOOLS = [{
    "type": "function", "name": "inspect_records",
    "description": "Read SmartReport records or prepare an existing desktop report. Never change records or send mail.",
    "strict": True,
    "parameters": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "purpose": {"type": "string", "enum": ["search", "check", "explain", "payroll_pdf", "customer_pdf", "catalog_preview"]},
            "date_from": {"type": "string", "description": "YYYY-MM-DD; ask if the period is unspecified"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
            "employee": {"type": "string", "description": "Exact employee name or empty for all"},
            "company": {"type": "string", "description": "Exact company name or empty"},
            "object_name": {"type": "string", "description": "Exact object name or empty"},
        },
        "required": ["purpose", "date_from", "date_to", "employee", "company", "object_name"],
    },
}]


class AssistantError(ValueError):
    pass


def ensure_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS assistant_settings (
            id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL, limit_micro INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO assistant_settings VALUES(1,0,10000000);
        CREATE TABLE IF NOT EXISTS assistant_requests (
            operation_id TEXT PRIMARY KEY, actor_id INTEGER NOT NULL, month TEXT NOT NULL,
            kind TEXT NOT NULL, charged_micro INTEGER NOT NULL, state TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS assistant_audit (
            id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL, action TEXT NOT NULL,
            outcome TEXT NOT NULL, operation_id TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS assistant_mail_drafts (
            id TEXT PRIMARY KEY, actor_id INTEGER NOT NULL, payload_hash TEXT NOT NULL,
            created_at REAL NOT NULL, state TEXT NOT NULL, message_id TEXT NOT NULL
        );
    """)


def audit(conn, actor, action, outcome, operation=None):
    conn.execute("INSERT INTO assistant_audit(actor_id,action,outcome,operation_id,created_at) VALUES(?,?,?,?,?)",
                 (actor["user_id"], action, outcome, operation, datetime.now(timezone.utc).isoformat()))


def status(conn):
    row = conn.execute("SELECT enabled,limit_micro FROM assistant_settings WHERE id=1").fetchone()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    spent = conn.execute("SELECT COALESCE(SUM(charged_micro),0) FROM assistant_requests WHERE month=?", (month,)).fetchone()[0]
    return dict(enabled=bool(row[0]), limit_usd=row[1] / 1000000, reserved_usd=spent / 1000000,
                month=month, key_configured=bool(os.environ.get("OPENAI_API_KEY")),
                model=MODEL, voice=True, financial_writes=False,
                email=all(os.environ.get(n) for n in ("SMARTREPORT_SMTP_HOST", "SMARTREPORT_SMTP_USER",
                                                     "SMARTREPORT_SMTP_PASSWORD", "SMARTREPORT_SMTP_FROM")),
                notice="Лимит по консервативным резервам; не счёт OpenAI. Неудачные запросы тоже расходуют резерв.")


def reserve(conn, actor, operation, kind, micro):
    try:
        uuid.UUID(operation)
    except (ValueError, TypeError, AttributeError):
        raise AssistantError("Нужен уникальный идентификатор операции.") from None
    conn.execute("BEGIN IMMEDIATE")
    state = status(conn)
    if not state["enabled"]:
        raise AssistantError("Помощник выключен. Обычная программа продолжает работать.")
    if not state["key_configured"]:
        raise AssistantError("API-ключ на сервере не настроен. Подписка ChatGPT не подключает API.")
    if conn.execute("SELECT 1 FROM assistant_requests WHERE operation_id=?", (operation,)).fetchone():
        raise AssistantError("Операция уже отправлялась. Автоматический повтор заблокирован.")
    limit = conn.execute("SELECT limit_micro FROM assistant_settings WHERE id=1").fetchone()[0]
    spent = conn.execute("SELECT COALESCE(SUM(charged_micro),0) FROM assistant_requests WHERE month=?",
                         (state["month"],)).fetchone()[0]
    if spent + micro > limit:
        raise AssistantError("Лимит помощника достигнут: новый запрос не отправлен в OpenAI.")
    conn.execute("INSERT INTO assistant_requests VALUES(?,?,?,?,?,'reserved',?)",
                 (operation, actor["user_id"], state["month"], kind, micro, datetime.now(timezone.utc).isoformat()))
    audit(conn, actor, kind, "reserved", operation)
    conn.commit()


def finish(conn, actor, operation, kind, outcome):
    conn.execute("UPDATE assistant_requests SET state=? WHERE operation_id=?", (outcome, operation))
    audit(conn, actor, kind, outcome, operation)
    conn.commit()


def validate_plan(plan):
    required = set(TOOLS[0]["parameters"]["required"])
    if not isinstance(plan, dict) or set(plan) != required:
        raise AssistantError("Команда не распознана однозначно. Укажите период и объект явно.")
    if plan["purpose"] not in TOOLS[0]["parameters"]["properties"]["purpose"]["enum"]:
        raise AssistantError("Эта операция не разрешена помощнику.")
    for name in required:
        if not isinstance(plan[name], str) or len(plan[name]) > 200:
            raise AssistantError("Неверные параметры команды.")
    try:
        if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", plan[key]) for key in ("date_from", "date_to")):
            raise ValueError()
        start, end = date.fromisoformat(plan["date_from"]), date.fromisoformat(plan["date_to"])
    except ValueError:
        raise AssistantError("Укажите даты YYYY-MM-DD.") from None
    if start > end or (end - start).days > 366:
        raise AssistantError("Период должен быть от одного дня до 367 дней.")
    return plan


def select_plan(text, conn, actor, operation, post=None):
    post = post or requests.post
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 8000:
        raise AssistantError("Введите команду длиной не более 8000 байт.")
    # A byte per token is a conservative input bound; output includes reasoning tokens.
    instructions = (
        "You are a SmartReport command router. Call inspect_records once only if all parameters are clear. "
        "Do not calculate money or invent records. catalog_preview prepares native price-review only; it never changes data. "
        "Do not change data, close periods or send mail. "
        "If unclear, do not call a tool; the application will ask for clarification. "
        f"Today is {date.today().isoformat()}; timezone Europe/Stockholm. "
        "Employee/object/company names must be copied from the user, not guessed. Empty filters mean all."
    )
    body = dict(model=MODEL, store=False, instructions=instructions, input=text, reasoning={"effort": "minimal"},
                tools=TOOLS, parallel_tool_calls=False, max_output_tokens=MAX_OUTPUT)
    input_bound = len(json.dumps(body, ensure_ascii=False).encode("utf-8")) + 4096
    micro = math.ceil(input_bound * INPUT_MICRO_USD + MAX_OUTPUT * OUTPUT_MICRO_USD)
    reserve(conn, actor, operation, "text", micro)
    try:
        response = post("https://api.openai.com/v1/responses", json=body,
                        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                        timeout=(10, 60), allow_redirects=False)
        if response.status_code != 200:
            raise AssistantError("OpenAI не принял запрос. Проверьте ключ, оплату и квоту API. Запрос не повторён.")
        data = response.json()
        if not isinstance(data, dict):
            raise AssistantError("Некорректный ответ OpenAI.")
        calls = [item for item in data.get("output", []) if isinstance(item, dict) and item.get("type") == "function_call"]
        if data.get("status") == "incomplete":
            raise AssistantError("Модель не завершила разбор команды. Сократите или уточните её.")
        if len(calls) != 1 or calls[0].get("name") != "inspect_records":
            raise AssistantError("Уточните команду: действие, сотрудник/объект и даты. Изменения и отправка писем пока недоступны.")
        plan = validate_plan(json.loads(calls[0]["arguments"]))
        finish(conn, actor, operation, "text", "routed")
        return plan
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
        finish(conn, actor, operation, "text", "failed")
        if isinstance(error, AssistantError):
            raise
        raise AssistantError("API недоступен или вернул неверный ответ. Данные не изменены; резерв запроса сохранён.") from None


def inspect(conn, plan):
    validate_plan(plan)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    employee = None
    if plan["employee"]:
        matches = conn.execute("SELECT id,name FROM meta_employees WHERE name=? COLLATE NOCASE",
                               (plan["employee"],)).fetchall()
        if len(matches) != 1:
            raise AssistantError("Сотрудник не найден однозначно. Введите точное ФИО из справочника.")
        employee = dict(matches[0])
    obj = None
    if plan["object_name"]:
        if not plan["company"]:
            raise AssistantError("Для объекта укажите фирму: одинаковые названия могут встречаться у разных фирм.")
        matches = conn.execute("SELECT id,name,company,rate,transport_rate FROM meta_objects WHERE name=? AND company=?",
                               (plan["object_name"], plan["company"])).fetchall()
        if len(matches) != 1:
            raise AssistantError("Объект не найден однозначно в выбранной фирме.")
        obj = dict(matches[0])
    filters, values = ["is_deleted=0", "date>=?", "date<=?"], [plan["date_from"], plan["date_to"]]
    for key, value in (("employee", employee["name"] if employee else ""),
                       ("company", plan["company"]), ("object_name", plan["object_name"])):
        if value:
            filters.append(key + "=?")
            values.append(value)
    rows = conn.execute("SELECT * FROM incoming_submissions WHERE " + " AND ".join(filters) +
                        " ORDER BY date,id LIMIT 501", values).fetchall()
    if len(rows) > 500:
        raise AssistantError("Найдено больше 500 записей. Сузьте период или фильтр.")
    records, duplicates = [], {}
    has_segments = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ob_segments'").fetchone()
    for raw in rows:
        row = dict(raw)
        row["entry_type"] = row["submission_type"]
        prices = row_prices(row)
        warnings = []
        if row["entry_type"] in ("work", "ob"):
            if prices["base"] is None:
                warnings.append("Клиентская ставка не задана")
            times = conn.execute("SELECT start_time,end_time,hours FROM ob_segments WHERE shift_id=? ORDER BY start_time,end_time,hours",
                                 (row["sync_id"],)).fetchall() if has_segments else []
            if not has_segments:
                warnings.append("На сервере нет таблицы сохранённых OB-сегментов; надбавка здесь не проверена")
            if not times and not (row.get("start_time") and row.get("end_time")):
                warnings.append("Нет сохранённого времени: OB по часам не определить")
            objects = conn.execute("SELECT rate,transport_rate FROM meta_objects WHERE company=? AND name=?",
                                   (row["company"], row["object_name"])).fetchall()
            if len(objects) != 1:
                warnings.append("Нет однозначной связи фирмы и объекта")
            else:
                if prices["base"] != objects[0]["rate"]:
                    warnings.append("Сохранённая ставка отличается от текущего справочника; это не обязательно ошибка")
                if prices["travel"] != objects[0]["transport_rate"]:
                    warnings.append("Сохранённая цена выезда отличается от справочника")
            if row["invoiced"]:
                warnings.append("Выставлена/закрыта: защищена от изменения")
        else:
            times = []
        signature = tuple(row[k] for k in ("date", "employee", "company", "object_name", "hours", "submission_type"))
        duplicates.setdefault(signature, []).append(row["sync_id"])
        work = row["entry_type"] in ("work", "ob")
        records.append(dict(id=row["sync_id"], date=row["date"], employee=row["employee"],
                            company=row["company"], object_name=row["object_name"],
                            entry_type=row["entry_type"], hours=row["hours"],
                            customer_rate=prices["base"], travel_rate=prices["travel"],
                            trips=row["transport"], price_source=prices.get("source") or "Не указано",
                            fixed=bool(prices.get("fixed")), intervals=[dict(t) for t in times],
                            prices=prices, start_time=row.get("start_time"), end_time=row.get("end_time"),
                            invoiced=bool(row["invoiced"]),
                            base_only=money(row["hours"], prices["base"]) if work and not prices.get("fixed") else None,
                            travel_only=money(row["transport"], prices["travel"]) if work and not prices.get("fixed") else None,
                            warnings=warnings))
    repeated = {identifier for group in duplicates.values() if len(group) > 1 for identifier in group}
    for record in records:
        if record["id"] in repeated:
            record["warnings"].append("Возможный дубликат: совпадают день, сотрудник, объект, вид записи и часы; не удалять автоматически")
    # No stored comments, card fields or calculation results are sent back to OpenAI.
    return dict(plan=plan, employee=employee, object=obj, records=records,
                source="Сервер SmartReport; локальные несинхронизированные записи не включены",
                notice="Суммы зарплаты, OB и фактуры рассчитываются только существующими отчётами. "
                       "Количество выездов и цена выезда — разные поля. Ничего не изменено.")


def transcribe(audio, conn, actor, operation, post=None):
    post = post or requests.post
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            frames = wav.getnframes()
            duration = frames / wav.getframerate()
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000 or not 0 < duration <= 60:
                raise AssistantError("Голос: нужен WAV PCM mono 16 kHz, 16 bit, до 60 секунд.")
            if len(wav.readframes(frames + 1)) != frames * 2:
                raise AssistantError("Голосовая запись повреждена или обрезана. Запрос не отправлен.")
    except (wave.Error, EOFError, ZeroDivisionError):
        raise AssistantError("Неверный формат голосовой записи.") from None
    reserve(conn, actor, operation, "voice", 100000)
    try:
        response = post("https://api.openai.com/v1/audio/transcriptions",
                        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                        data={"model": "gpt-4o-mini-transcribe"},
                        files={"file": ("command.wav", audio, "audio/wav")},
                        timeout=(10, 60), allow_redirects=False)
        if response.status_code != 200:
            raise AssistantError("Распознавание недоступно. Проверьте оплату API или введите команду текстом.")
        text = response.json()["text"]
        if not isinstance(text, str) or len(text.encode()) > 8000:
            raise AssistantError("Ответ распознавания слишком длинный.")
        finish(conn, actor, operation, "voice", "transcribed")
        return text
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
        finish(conn, actor, operation, "voice", "failed")
        if isinstance(error, AssistantError):
            raise
        raise AssistantError("Распознавание не завершено. Запрос не повторён; можно ввести текст.") from None


def mail_payload(data):
    if not isinstance(data, dict) or set(data) != {"to", "subject", "body", "pdf"}:
        raise AssistantError("Нужны адресат, тема, текст и PDF.")
    if any(not isinstance(data[k], str) for k in data):
        raise AssistantError("Неверные поля письма.")
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", data["to"]):
        raise AssistantError("Укажите один адрес электронной почты без заголовков и переводов строк.")
    if not 1 <= len(data["subject"]) <= 200 or "\n" in data["subject"] or "\r" in data["subject"] or len(data["body"]) > 10000:
        raise AssistantError("Проверьте тему и длину письма.")
    try:
        content = base64.b64decode(data["pdf"], validate=True)
        if not content.startswith(b"%PDF-") or len(content) > 1000000:
            raise ValueError()
        from pypdf import PdfReader
        pdf = PdfReader(io.BytesIO(content), strict=True)
        if pdf.is_encrypted or not len(pdf.pages):
            raise ValueError()
    except (ValueError, TypeError, PyPdfError):
        raise AssistantError("Вложите незашифрованный PDF до 1 МБ.") from None
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(), content


def prepare_mail(conn, actor, data):
    state = status(conn)
    if not state["enabled"] or not state["email"]:
        raise AssistantError("Отправка выключена или SMTP на сервере не настроен.")
    fingerprint, _ = mail_payload(data)
    identifier = str(uuid.uuid4())
    message_id = make_msgid(domain="smartreport.local")
    conn.execute("INSERT INTO assistant_mail_drafts VALUES(?,?,?,?,?,?)",
                 (identifier, actor["user_id"], fingerprint, datetime.now(timezone.utc).timestamp(), "draft", message_id))
    audit(conn, actor, "mail_prepare", "draft", identifier)
    conn.commit()
    return dict(draft_id=identifier, sha256=fingerprint, message_id=message_id,
                notice="Черновик не отправлен. Подтвердите адресата, текст и конкретный PDF. Действует 10 минут.")


def send_mail(conn, actor, data, smtp_factory=None):
    smtp_factory = smtp_factory or smtplib.SMTP
    if not isinstance(data, dict) or set(data) != {"draft_id", "payload", "confirmed"} or data["confirmed"] is not True:
        raise AssistantError("Отправка требует явного подтверждения конкретного черновика.")
    if not isinstance(data["draft_id"], str):
        raise AssistantError("Неверный черновик.")
    fingerprint, pdf = mail_payload(data["payload"])
    conn.execute("BEGIN IMMEDIATE")
    state = status(conn)
    if not state["enabled"] or not state["email"]:
        raise AssistantError("Помощник или отправка выключены.")
    draft = conn.execute("SELECT * FROM assistant_mail_drafts WHERE id=? AND actor_id=?",
                         (data["draft_id"], actor["user_id"])).fetchone()
    if (not draft or draft["state"] != "draft" or draft["payload_hash"] != fingerprint
            or datetime.now(timezone.utc).timestamp() - draft["created_at"] > 600):
        raise AssistantError("Черновик изменён, устарел или уже отправлялся. Повторная отправка запрещена.")
    sender = os.environ["SMARTREPORT_SMTP_FROM"]
    if not re.fullmatch(r"[^@\s\r\n]+@[^@\s\r\n]+", sender):
        raise AssistantError("Неверный серверный адрес отправителя.")
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, data["payload"]["to"], data["payload"]["subject"]
    message["Message-ID"] = draft["message_id"]
    message.set_content(data["payload"]["body"])
    message.add_attachment(pdf, maintype="application", subtype="pdf", filename="SmartReport.pdf")
    conn.execute("UPDATE assistant_mail_drafts SET state='sending' WHERE id=?", (draft["id"],))
    audit(conn, actor, "mail_confirm", "sending", draft["id"])
    conn.commit()
    try:
        with smtp_factory(os.environ["SMARTREPORT_SMTP_HOST"], 587, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(os.environ["SMARTREPORT_SMTP_USER"], os.environ["SMARTREPORT_SMTP_PASSWORD"])
            refused = smtp.send_message(message)
            if refused:
                raise smtplib.SMTPException("Recipient refused")
    except (OSError, smtplib.SMTPException):
        conn.execute("UPDATE assistant_mail_drafts SET state='unknown' WHERE id=?", (draft["id"],))
        audit(conn, actor, "mail_send", "unknown_no_retry", draft["id"])
        conn.commit()
        raise AssistantError("Почтовый сервис не подтвердил завершение. Проверьте «Отправленные»/журнал провайдера; повтор заблокирован.") from None
    conn.execute("UPDATE assistant_mail_drafts SET state='accepted' WHERE id=?", (draft["id"],))
    audit(conn, actor, "mail_send", "accepted_by_smtp", draft["id"])
    conn.commit()
    return dict(message_id=draft["message_id"], notice="Принято SMTP-сервером. Это не подтверждение доставки или прочтения.")


def register_assistant(app, connect, authenticate):
    @app.before_request
    def assistant_boundary():
        if not request.path.startswith("/api/assistant/"):
            return None
        local = request.remote_addr in {"127.0.0.1", "::1"} and request.host.split(":")[0] in {"localhost", "127.0.0.1"}
        if not request.is_secure and not local:
            return jsonify(ok=False, error="Помощнику требуется HTTPS."), 403
        user = authenticate()
        if not user or user["role"] != "admin":
            return jsonify(ok=False, error="Помощник доступен только личному входу администратора."), 403
        if request.content_length is None and request.method == "POST":
            return jsonify(ok=False, error="Не указан размер запроса."), 400
        if request.content_length and request.content_length > 2000000:
            return jsonify(ok=False, error="Превышен размер запроса."), 413

    @app.after_request
    def assistant_private(response):
        if request.path.startswith("/api/assistant/"):
            response.headers["Cache-Control"] = "no-store, private"
        return response

    def handle(action):
        conn = connect()
        try:
            ensure_schema(conn)
            actor = authenticate()
            return action(conn, actor)
        except AssistantError as error:
            conn.rollback()
            return jsonify(ok=False, error=str(error)), 400
        except (sqlite3.Error, requests.RequestException):
            conn.rollback()
            app.logger.error("Assistant operation failed; no request content or credentials logged")
            return jsonify(ok=False, error="Помощник временно недоступен. Обычный учёт работает."), 503
        finally:
            conn.close()

    @app.route("/api/assistant/status", methods=["GET", "POST"])
    def assistant_status():
        def action(conn, actor):
            if request.method == "POST":
                data = request.get_json(silent=True)
                if not isinstance(data, dict) or set(data) != {"enabled", "limit_usd"} or type(data["enabled"]) is not bool:
                    raise AssistantError("Укажите выключатель и лимит USD.")
                try:
                    amount = Decimal(str(data["limit_usd"]))
                    if not amount.is_finite() or not Decimal("0") <= amount <= Decimal("1000"):
                        raise ValueError()
                    micro = int(amount * 1000000)
                except (ValueError, ArithmeticError):
                    raise AssistantError("Лимит: число от 0 до 1000 USD.") from None
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE assistant_settings SET enabled=?,limit_micro=? WHERE id=1", (data["enabled"], micro))
                audit(conn, actor, "settings", "enabled" if data["enabled"] else "disabled")
                conn.commit()
            return jsonify(ok=True, status=status(conn))
        return handle(action)

    @app.route("/api/assistant/command", methods=["POST"])
    def assistant_command():
        def action(conn, actor):
            data = request.get_json(silent=True)
            if not isinstance(data, dict) or set(data) != {"text", "operation_id"}:
                raise AssistantError("Ожидаются текст и идентификатор операции.")
            plan = select_plan(data["text"], conn, actor, data["operation_id"])
            result = inspect(conn, plan)
            conn.commit()
            conn.execute("PRAGMA query_only=OFF")
            audit(conn, actor, plan["purpose"], "read_completed", data["operation_id"])
            conn.commit()
            return jsonify(ok=True, result=result)
        return handle(action)

    @app.route("/api/assistant/transcribe", methods=["POST"])
    def assistant_transcribe():
        return handle(lambda conn, actor: jsonify(ok=True, text=transcribe(
            request.get_data(), conn, actor, request.headers.get("X-Operation-ID"))))

    @app.route("/api/assistant/audit", methods=["GET"])
    def assistant_audit():
        def action(conn, actor):
            rows = conn.execute("SELECT actor_id,action,outcome,operation_id,created_at FROM assistant_audit ORDER BY id DESC LIMIT 100").fetchall()
            return jsonify(ok=True, actions=[dict(r) for r in rows])
        return handle(action)

    @app.route("/api/assistant/mail/prepare", methods=["POST"])
    def assistant_mail_prepare():
        return handle(lambda conn, actor: jsonify(ok=True, draft=prepare_mail(conn, actor, request.get_json(silent=True))))

    @app.route("/api/assistant/mail/confirm", methods=["POST"])
    def assistant_mail_confirm():
        return handle(lambda conn, actor: jsonify(ok=True, mail=send_mail(conn, actor, request.get_json(silent=True))))
