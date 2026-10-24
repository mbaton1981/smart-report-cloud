"""Allowlisted director assistant. No SQL, credentials or financial writes from a model."""
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
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
from assistant_actions import CAPABILITIES, NATIVE_ACTIONS, OBJECT_ACTIONS, capabilities

MODEL = "gpt-5-mini"
MAX_OUTPUT = 1024
INPUT_MICRO_USD = Decimal("0.25")
OUTPUT_MICRO_USD = Decimal("2")
TOOLS = [{
    "type": "function", "name": "inspect_records",
    "description": "Read SmartReport records. transport_check IS SUPPORTED: check saved customer trips, travel rates "
                   "and catalogue differences. Russian: проверка транспорта, выездов и цен выезда. "
                   "search lists shifts, check checks saved rates/data, explain shows stored base/travel amounts. "
                   "payroll_pdf/customer_pdf/catalog_preview prepare existing desktop reports, never writes.",
    "strict": True,
    "parameters": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "purpose": {"type": "string", "enum": list(CAPABILITIES)},
            "date_from": {"type": "string", "description": "YYYY-MM-DD; ask if the period is unspecified"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
            "employee": {"type": "string", "description": "Exact employee name or empty for all"},
            "company": {"type": "string", "description": "Exact company name or empty"},
            "object_name": {"type": "string", "description": "Exact object name or empty"},
        },
        "required": ["purpose", "date_from", "date_to", "employee", "company", "object_name"],
    },
}, {
    "type": "function", "name": "request_clarification", "strict": True,
    "description": "Ask a specific question when a required parameter is missing or the action is unsupported.",
    "parameters": {"type": "object", "additionalProperties": False,
                   "properties": {"field": {"type": "string", "enum": ["period", "scope", "object", "unsupported"]}},
                   "required": ["field"]},
}]
TOOLS[1]["parameters"]["properties"]["selection"] = {
    "anyOf": [TOOLS[0]["parameters"], {"type": "null"}],
    "description": "Proposed action and known selection for continuation, or null if unknown. "
                   "Reuse dates from supplied selection when the user has not changed the period.",
}
TOOLS[1]["parameters"]["required"].append("selection")
TOOLS.append({
    "type": "function", "name": "run_workflow", "strict": True,
    "description": "Run 2 to 4 related read/check/report steps for one user request. No native editing forms or writes.",
    "parameters": {"type": "object", "additionalProperties": False,
                   "properties": {"steps": {"type": "array", "minItems": 2, "maxItems": 4,
                                            "items": TOOLS[0]["parameters"]}}, "required": ["steps"]},
})

QUESTIONS = {
    "period": "За какой период? Укажите даты словами или выберите их сверху и нажмите «Продолжить с выбранными параметрами».",
    "scope": "Для всех сотрудников или одного? Оставьте поле сотрудника пустым для всех или введите точное ФИО.",
    "object": "По какому объекту? Назовите объект; если названия совпадают, укажите также фирму.",
    "unsupported": "Автоматическое выполнение этой операции не подключено. Поддерживаемые проверки, отчёты "
                   "и штатные формы перечислены в «Возможностях». Ничего не изменено и не отправлено; "
                   "неподдерживаемая операция не заменялась поиском смен.",
}


class AssistantError(ValueError):
    pass


class AssistantQuestion(AssistantError):
    def __init__(self, field, *, question=None, choices=None, plan=None):
        self.field = field
        self.choices = choices or []
        self.plan = plan
        super().__init__(question or QUESTIONS[field])


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


def reserve(conn, actor, operation, kind, micro, *, requires_provider=True):
    try:
        uuid.UUID(operation)
    except (ValueError, TypeError, AttributeError):
        raise AssistantError("Нужен уникальный идентификатор операции.") from None
    conn.execute("BEGIN IMMEDIATE")
    state = status(conn)
    if not state["enabled"]:
        raise AssistantError("Помощник выключен. Обычная программа продолжает работать.")
    if requires_provider and not state["key_configured"]:
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


def transport_command(text, context=None):
    text = strip_no_change_suffix(text)
    match = re.fullmatch(
        r"(?:проверь(?:те)?|проверить)\s+(?:клиентский\s+)?транспорт"
        r"(?:\s+(?:за|на)\s+(?P<week>текущую неделю|эту неделю|этой неделе|прошлую неделю|прошлой неделе|тот же период)"
        r"|\s+с\s+(?P<start>\d{4}-\d{2}-\d{2})\s+по\s+(?P<end>\d{4}-\d{2}-\d{2}))?"
        r"(?:,?\s+(?:для|у)\s+(?P<scope>всех сотрудников|одного сотрудника|сотрудника\s+(?P<name>[^,\n]+)))?"
        r"[.!?]?",
        " ".join(text.strip().split()), re.IGNORECASE)
    if match is None:
        return None
    previous = context["plan"] if context else {}
    today = datetime.now(ZoneInfo("Europe/Stockholm")).date()
    monday = today - timedelta(days=today.weekday())
    if match["week"] and "недел" in match["week"]:
        start = monday
        if match["week"].casefold().startswith("прошл"):
            start -= timedelta(days=7)
        begin, end = start.isoformat(), (start + timedelta(days=6)).isoformat()
    elif match["start"]:
        begin, end = match["start"], match["end"]
    else:
        begin = previous.get("date_from", monday.isoformat())
        end = previous.get("date_to", (monday + timedelta(days=6)).isoformat())
    plan = validate_plan(dict(purpose="transport_check", date_from=begin, date_to=end,
                              employee=(match["name"] or "").strip() if match["scope"] else previous.get("employee", ""),
                              company=previous.get("company", ""), object_name=previous.get("object_name", "")))
    if match["scope"] and match["scope"].casefold() == "одного сотрудника":
        raise AssistantQuestion("scope", plan=plan)
    if not match["start"] and not (match["week"] and "недел" in match["week"]) and not context:
        raise AssistantQuestion("period", plan=plan)
    return plan


def strip_no_change_suffix(text):
    return re.sub(
        r"(?:[,;]|\s+)(?:ничего\s+не\s+(?:меняй(?:те)?|изменяй(?:те)?|менять|изменять)"
        r"|не\s+(?:меняй(?:те)?|изменяй(?:те)?)\s+ничего)\s*[.!?]*$",
        "", text.strip(), flags=re.IGNORECASE,
    ).rstrip(" \t,;.!?")


def recognized_read_command(text, context=None):
    text = strip_no_change_suffix(text)
    normalized = " ".join(text.casefold().strip(" .!?").split())
    if re.search(r"\b(?:объект|фирм|компан|сотрудник|для\s+(?!всех\s+сотрудник))", normalized):
        return None
    if re.fullmatch(r"(?:покажи\s*,?\s*)?кто\s+сколько\s+(?:отработал|работал)(?:\s+.+)?", normalized):
        purpose = "hours_summary"
    elif re.search(r"\bдубл\w*", normalized) or re.search(
            r"\b(?:пропущен|отсутств|без|не\s+задан)\w*\s+ставк\w*", normalized):
        purpose = "anomalies"
    else:
        return None

    previous = context["plan"] if context else {}
    week = re.search(r"\b(текущ\w*|эт\w*|прошл\w*)\s+недел\w*\b|\b(this|last)\s+week\b", normalized)
    date_range = re.search(
        r"\bс\s+(\d{4}-\d{2}-\d{2})\s+по\s+(\d{4}-\d{2}-\d{2})\b", normalized)
    if date_range:
        begin, end = date_range.groups()
    elif week:
        today = datetime.now(ZoneInfo("Europe/Stockholm")).date()
        start = today - timedelta(days=today.weekday())
        last_week = (week.group(1) or "").startswith("прошл") or week.group(2) == "last"
        if last_week:
            start -= timedelta(days=7)
        begin, end = start.isoformat(), (start + timedelta(days=6)).isoformat()
    elif previous:
        begin, end = previous["date_from"], previous["date_to"]
    else:
        today = datetime.now(ZoneInfo("Europe/Stockholm")).date()
        start = today - timedelta(days=today.weekday())
        begin, end = start.isoformat(), (start + timedelta(days=6)).isoformat()

    plan = validate_plan(dict(
        purpose=purpose, date_from=begin, date_to=end,
        employee="" if "для всех сотрудников" in normalized else previous.get("employee", ""),
        company=previous.get("company", ""), object_name=previous.get("object_name", "")))
    if not (week or date_range or previous):
        raise AssistantQuestion("period", plan=plan)
    return plan


def select_plan(text, conn, actor, operation, post=None, context=None):
    post = post or requests.post
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 8000:
        raise AssistantError("Введите команду длиной не более 8000 байт.")
    text = strip_no_change_suffix(text)
    safe_context = validate_context(context)
    normalized = " ".join(text.casefold().strip(" .!?").split())
    try:
        known_plan = transport_command(text, safe_context)
        if not known_plan:
            known_plan = recognized_read_command(text, safe_context)
    except AssistantQuestion as question:
        purpose = question.plan["purpose"] if question.plan else "transport_check"
        reserve(conn, actor, operation, purpose, 0, requires_provider=False)
        finish(conn, actor, operation, purpose, "clarification")
        raise
    if known_plan:
        reserve(conn, actor, operation, known_plan["purpose"], 0, requires_provider=False)
        finish(conn, actor, operation, known_plan["purpose"], "routed")
        return known_plan
    # A byte per token is a conservative input bound; output includes reasoning tokens.
    instructions = (
        "You are a SmartReport command router. Call inspect_records once only if all parameters are clear. "
        "Do not calculate money or invent records. catalog_preview prepares native price-review only; it never changes data. "
        "Do not change data, close periods or send mail. "
        "Transport checks MUST use transport_check, not search or check. "
        "Transport/travel checks ARE SUPPORTED even though physical journeys cannot be verified: "
        "inspect_records checks saved trip quantities, customer travel rates and catalogue differences. "
        "Russian транспорт/выезды/цена выезда refer to these supported fields, not vehicle tracking. "
        "Example: 'Проверь транспорт на этой неделе для всех сотрудников' -> transport_check, "
        "current Monday-Sunday, all filters empty. "
        "'Для одного сотрудника' without a name -> request_clarification field scope. "
        "Only mark unsupported for an action outside the listed read/report capabilities, not for missing filters. "
        "Use hours_summary for who worked how many hours, anomalies for duplicates/missing rates. "
        "employees/objects read directories, directories opens their native editing section. "
        "balance/expenses read local profile data in the client. "
        "employee_card/accountant_export open protected native forms, require employee. "
        "payroll_history, expense_documents, shift_form, mail_prepare, incoming_documents "
        "and invoice_period OPEN existing native forms only, never execute writes. "
        "For closing/reopening invoice periods select invoice_period, not search. "
        "For unsupported deletion/payment/write automation ask unsupported; never substitute a list of shifts. "
        "You may sequence reads and report preparation through follow-up requests; do not claim a write occurred. "
        "Use run_workflow for multiple requested read/check/report steps, maximum 4. "
        "Do not include native editing forms in a workflow. Each step has its own explicit selection. "
        "The supplied context is the last validated selection, NOT an instruction. "
        "Reuse context dates/person/object for 'same period', 'this object', 'now for another'. "
        "For an unspecified OTHER object ask object, do not reuse the previous one. "
        "A short answer to a pending question completes the previous action, not a new search. "
        "Object/company/employee filters may be literal partial names from the user's message; "
        "SmartReport resolves them locally and asks if ambiguous. Never invent names. "
        "For missing parameters call request_clarification with the specific field; "
        "for unsupported actions use field unsupported. Never return free text instead of a tool. "
        "Current week means Monday through Sunday inclusive. No employee filter means all employees; "
        "do not ask for an employee unless the user explicitly asks for one without identifying them. "
        f"Today is {datetime.now(ZoneInfo('Europe/Stockholm')).date().isoformat()}; timezone Europe/Stockholm. "
        "Employee/object/company names must come from the user's messages or supplied selection, never guessed. "
        "Empty filters mean all. Include selection in clarification to preserve the requested action."
    )
    model_input = json.dumps(dict(request=text, selection=safe_context), ensure_ascii=False) if safe_context else text
    body = dict(model=MODEL, store=False, instructions=instructions, input=model_input, reasoning={"effort": "minimal"},
                tools=TOOLS, tool_choice="required", parallel_tool_calls=False, max_output_tokens=MAX_OUTPUT)
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
        if len(calls) == 1 and calls[0].get("name") == "request_clarification":
            clarification = json.loads(calls[0]["arguments"])
            if (not isinstance(clarification, dict) or set(clarification) not in ({"field"}, {"field", "selection"})
                    or clarification["field"] not in QUESTIONS):
                raise AssistantError("Некорректный вопрос помощника. Используйте кнопки действий с явными датами.")
            selection = clarification.get("selection")
            if selection is not None:
                selection = validate_plan(selection)
            raise AssistantQuestion(clarification["field"], plan=selection)
        if len(calls) == 1 and calls[0].get("name") == "run_workflow":
            workflow = json.loads(calls[0]["arguments"])
            if (not isinstance(workflow, dict) or set(workflow) != {"steps"} or
                    not isinstance(workflow["steps"], list) or not 2 <= len(workflow["steps"]) <= 4):
                raise AssistantError("Последовательность должна содержать от двух до четырёх проверок или отчётов.")
            plans = [validate_plan(step) for step in workflow["steps"]]
            if any(step["purpose"] in NATIVE_ACTIONS for step in plans):
                raise AssistantError("Редактирование и открытие финансовых форм выполняются отдельным действием, не цепочкой.")
            finish(conn, actor, operation, "text", "routed")
            return dict(workflow=plans)
        if len(calls) != 1 or calls[0].get("name") != "inspect_records":
            raise AssistantError("Помощник не выбрал поддерживаемую функцию. Выберите действие кнопкой ниже; "
                                 "период и сотрудника можно задать в полях над кнопками.")
        plan = validate_plan(json.loads(calls[0]["arguments"]))
        if re.match(r"проверь(?:те)?\s+транспорт\b", normalized) and plan["purpose"] in {"search", "check", "explain"}:
            plan["purpose"] = "transport_check"
        finish(conn, actor, operation, "text", "routed")
        return plan
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
        finish(conn, actor, operation, "text", "clarification" if isinstance(error, AssistantQuestion) else "failed")
        if isinstance(error, AssistantError):
            raise
        raise AssistantError("API недоступен или вернул неверный ответ. Данные не изменены; резерв запроса сохранён.") from None


def validate_context(context):
    if context is None:
        return None
    if not isinstance(context, dict) or set(context) != {"plan", "pending"}:
        raise AssistantError("Контекст диалога повреждён. Начните новый диалог.")
    if not isinstance(context["pending"], str) or context["pending"] not in {"", "period", "scope", "object", "unsupported"}:
        raise AssistantError("Неизвестное уточнение диалога.")
    return dict(plan=validate_plan(context["plan"]).copy(), pending=context["pending"])


def resolve_selection(conn, plan):
    plan = plan.copy()
    if plan["employee"]:
        rows = conn.execute("SELECT id,name FROM meta_employees ORDER BY id").fetchall()
        needle = plan["employee"].casefold()
        exact = [row for row in rows if row["name"].casefold() == needle]
        matches = exact or [row for row in rows if needle in row["name"].casefold()]
        if len(matches) != 1:
            raise AssistantQuestion("scope", question="Какого сотрудника выбрать?" if matches else
                                    "Такой сотрудник не найден. Выберите ФИО из справочника.",
                                    choices=[dict(employee=row["name"]) for row in (matches or rows)[:30]], plan=plan)
        plan["employee"] = matches[0]["name"]
    if plan["object_name"] or plan["company"]:
        rows = conn.execute("SELECT id,name,company FROM meta_objects ORDER BY company,name,id").fetchall()
        candidates = [row for row in rows if
                      (not plan["company"] or plan["company"].casefold() in row["company"].casefold()) and
                      (not plan["object_name"] or plan["object_name"].casefold() in row["name"].casefold())]
        exact = [row for row in candidates if row["name"].casefold() == plan["object_name"].casefold()
                 and (not plan["company"] or row["company"].casefold() == plan["company"].casefold())]
        matches = exact or candidates
        if plan["object_name"] or plan["purpose"] in OBJECT_ACTIONS:
            if len(matches) != 1:
                raise AssistantQuestion("object", question="Какой объект выбрать? Укажите фирму и объект."
                                        if matches else "Объект не найден. Выберите его из справочника.",
                                        choices=[dict(company=row["company"], object_name=row["name"])
                                                 for row in (matches or rows)[:30]], plan=plan)
            plan.update(company=matches[0]["company"], object_name=matches[0]["name"])
        else:
            firms = sorted({row["company"] for row in candidates})
            if len(firms) != 1:
                raise AssistantQuestion("object", question="Какую фирму выбрать?",
                                        choices=[dict(company=firm) for firm in firms[:30]], plan=plan)
            plan["company"] = firms[0]
    if plan["purpose"] in OBJECT_ACTIONS and not plan["object_name"]:
        rows = conn.execute("SELECT company,name FROM meta_objects ORDER BY company,name").fetchall()
        raise AssistantQuestion("object", plan=plan,
                                choices=[dict(company=row["company"], object_name=row["name"]) for row in rows[:30]])
    if plan["purpose"] in {"employee_card", "accountant_export"} and not plan["employee"]:
        raise AssistantQuestion("scope", question="Для какого сотрудника открыть карточку или данные бухгалтеру?",
                                plan=plan, choices=[dict(employee=row["name"]) for row in conn.execute(
                                    "SELECT name FROM meta_employees ORDER BY name LIMIT 30")])
    return plan


def inspect(conn, plan, *, begin_snapshot=True):
    plan = resolve_selection(conn, validate_plan(plan))
    conn.execute("PRAGMA query_only=ON")
    if begin_snapshot:
        conn.execute("BEGIN")
    if plan["purpose"] in NATIVE_ACTIONS | {"employees", "objects"}:
        result = dict(plan=plan, employee=None, object=None, records=[],
                      source="Справочники сервера SmartReport; финансовые разделы — текущий локальный профиль.",
                      notice="Ничего не сохранено и не отправлено. Открытие формы не означает выполнения операции.")
        if plan["purpose"] == "employees":
            rows = conn.execute("SELECT name,is_active FROM meta_employees ORDER BY name").fetchall()
            result["table"] = dict(headers=["Сотрудник", "Статус"],
                                   rows=[[r["name"], "Работает" if r["is_active"] else "Неактивен"]
                                         for r in rows if not plan["employee"] or r["name"] == plan["employee"]])
        elif plan["purpose"] == "objects":
            rows = conn.execute("SELECT company,name,rate,transport_rate,is_active FROM meta_objects ORDER BY company,name").fetchall()
            result["table"] = dict(headers=["Фирма", "Объект", "Ставка, SEK/ч", "Выезд, SEK", "Статус"],
                                   rows=[[r["company"], r["name"], r["rate"], r["transport_rate"],
                                          "Действует" if r["is_active"] else "Архив"] for r in rows
                                         if (not plan["company"] or r["company"] == plan["company"]) and
                                         (not plan["object_name"] or r["name"] == plan["object_name"])])
        else:
            result["native_action"] = plan["purpose"]
            if plan["purpose"] == "expense_documents" and plan["object_name"]:
                result["object"] = dict(conn.execute(
                    "SELECT id,name,company FROM meta_objects WHERE name=? AND company=?",
                    (plan["object_name"], plan["company"])).fetchone())
        return result
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
        record["duplicate"] = record["id"] in repeated
        if record["id"] in repeated:
            record["warnings"].append("Возможный дубликат: совпадают день, сотрудник, объект, вид записи и часы; не удалять автоматически")
    # No stored comments, card fields or calculation results are sent back to OpenAI.
    result = dict(plan=plan, employee=employee, object=obj, records=records,
                source="Сервер SmartReport; локальные несинхронизированные записи не включены",
                notice="Суммы зарплаты, OB и фактуры рассчитываются только существующими отчётами. "
                       "Количество выездов и цена выезда — разные поля. Ничего не изменено.")
    if plan["purpose"] == "transport_check":
        result["transport"] = check_transport(conn, records)
    elif plan["purpose"] == "hours_summary":
        totals = {}
        for row in records:
            values = totals.setdefault(row["employee"], [Decimal("0"), Decimal("0"), 0])
            values[0 if row["entry_type"] in {"work", "ob"} else 1] += Decimal(str(row["hours"]))
            values[2] += 1
        result["table"] = dict(headers=["Сотрудник", "Отработано, ч", "Другие виды часов, ч", "Записей"],
                               rows=[[name, str(values[0]), str(values[1]), values[2]]
                                     for name, values in sorted(totals.items())])
        result["summary"] = f"Сотрудников: {len(totals)}. Отработано: " + str(
            sum((values[0] for values in totals.values()), Decimal("0"))) + " ч. OB входит в отработанное время."
    elif plan["purpose"] == "anomalies":
        missing = [r for r in records if r["entry_type"] in {"work", "ob"} and r["customer_rate"] is None and not r["fixed"]]
        result["summary"] = f"Проверено {len(records)} записей. Без ставки: {len(missing)}; " \
                            f"возможные дубли: {len(repeated)} записей. Автоматического удаления и замены цен нет."
        result["records"] = [r for r in records if r in missing or r["duplicate"]]
    result["context"] = dict(plan=plan, pending="")
    return result


def inspect_workflow(conn, plans):
    if not isinstance(plans, list) or not 2 <= len(plans) <= 4:
        raise AssistantError("Допустимо от двух до четырёх шагов.")
    validated = [resolve_selection(conn, validate_plan(step)) for step in plans]
    if any(step["purpose"] in NATIVE_ACTIONS for step in validated):
        raise AssistantError("Штатные формы изменения нельзя запускать цепочкой.")
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    return [inspect(conn, step, begin_snapshot=False) for step in validated]


def check_transport(conn, records):
    """Validate saved transport inputs, not the occurrence of actual journeys."""
    checked, excluded, issues, subtotal = 0, 0, 0, Decimal("0")
    for row in records:
        findings = []
        row["transport_findings"] = findings
        if row["entry_type"] not in {"work", "ob"} or row["fixed"]:
            excluded += 1
            findings.append("Не проверено: не рабочая запись или Fast pris; проверьте штатный отчёт договора.")
            continue
        checked += 1
        trips = row["trips"]
        valid_trips = isinstance(trips, (int, float)) and math.isfinite(trips) and trips >= 0
        if not valid_trips:
            findings.append("Ошибка: количество выездов должно быть конечным неотрицательным числом.")
        if row["travel_rate"] is None:
            findings.append("Цена выезда не задана; стоимость не подтверждена, даже если выезды равны нулю.")
        objects = conn.execute("SELECT transport_rate FROM meta_objects WHERE company=? AND name=?",
                               (row["company"], row["object_name"])).fetchall()
        row["catalog_travel_rate"] = objects[0]["transport_rate"] if len(objects) == 1 else None
        if len(objects) != 1:
            findings.append("Не удалось однозначно найти фирму и объект в справочнике.")
        elif objects[0]["transport_rate"] is None:
            findings.append("В справочнике не задана цена выезда.")
        elif row["travel_rate"] is not None and row["travel_rate"] != objects[0]["transport_rate"]:
            findings.append("Сохранённая цена отличается от текущего справочника. Это может быть ручная "
                            "или историческая цена; требуется проверка, автоматической замены нет.")
        if valid_trips and row["travel_rate"] is not None and row["travel_only"] is not None:
            subtotal += Decimal(str(row["travel_only"]))
        if findings:
            issues += 1
        elif trips == 0:
            findings.append("Выезды не заявлены. По записи нельзя установить, был ли фактический выезд.")
        else:
            findings.append("Цена совпадает со справочником; количество и стоимость заполнены.")
    return dict(checked=checked, excluded=excluded, attention=issues, known_subtotal=str(subtotal),
                scope="Проверен клиентский транспорт: сохранённые выезды, наличие цены и её совпадение с текущим справочником.",
                limits="Фактические поездки, право на оплату, объединение выездов и условия договора "
                       "автоматически не подтверждаются. Подытог известных сумм — не итог фактуры. "
                       "Нулевые выезды не означают отсутствия поездки. Личные транспортные расходы "
                       "и выплаты сотрудникам здесь не проверяются. Данные не изменены.")


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
        except AssistantQuestion as question:
            conn.rollback()
            return jsonify(ok=True, clarification=dict(field=question.field, question=str(question),
                           choices=question.choices, plan=question.plan))
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
            if not isinstance(data, dict) or set(data) not in ({"text", "operation_id"}, {"text", "operation_id", "context"}):
                raise AssistantError("Ожидаются текст и идентификатор операции.")
            plan = select_plan(data["text"], conn, actor, data["operation_id"], context=data.get("context"))
            results = inspect_workflow(conn, plan["workflow"]) if "workflow" in plan else [inspect(conn, plan)]
            conn.commit()
            conn.execute("PRAGMA query_only=OFF")
            for result in results:
                audit(conn, actor, result["plan"]["purpose"], "read_completed", data["operation_id"])
            conn.commit()
            return jsonify(ok=True, results=results) if "workflow" in plan else jsonify(ok=True, result=results[0])
        return handle(action)

    @app.route("/api/assistant/inspect", methods=["POST"])
    def assistant_inspect():
        def action(conn, actor):
            data = request.get_json(silent=True)
            if not isinstance(data, dict) or set(data) != {"plan", "operation_id"}:
                raise AssistantError("Выберите действие, даты и сотрудника.")
            plan = validate_plan(data["plan"])
            reserve(conn, actor, data["operation_id"], plan["purpose"], 0, requires_provider=False)
            result = inspect(conn, plan)
            conn.commit()
            conn.execute("PRAGMA query_only=OFF")
            finish(conn, actor, data["operation_id"], plan["purpose"], "read_completed")
            return jsonify(ok=True, result=result)
        return handle(action)

    @app.route("/api/assistant/capabilities", methods=["GET"])
    def assistant_capabilities():
        def action(conn, actor):
            return jsonify(ok=True, capabilities=capabilities(), integration_version=2,
                           restrictions=["Личный вход администратора; права других ролей не расширены.",
                                         "Универсальные изменения/удаления/выплаты по тексту не поддерживаются.",
                                         "Финансовые действия только в штатных формах с их подтверждениями.",
                                         "Отмена — по правилам соответствующего раздела, не универсальная."])
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
