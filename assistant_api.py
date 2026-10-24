"""Allowlisted director assistant. No SQL, credentials or financial writes from a model."""
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
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
                   "properties": {"field": {"type": "string", "enum": [
                       "period", "scope", "object", "report_type", "unsupported"
                   ]}},
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
    "report_type": "Какой отчёт подготовить? Выберите один из вариантов ниже.",
    "unsupported": "Автоматическое выполнение этой операции не подключено. Поддерживаемые проверки, отчёты "
                   "и штатные формы перечислены в «Возможностях». Ничего не изменено и не отправлено; "
                   "неподдерживаемая операция не заменялась поиском смен.",
}

DATE_TOKEN = r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{2,4})"
_RUSSIAN_MONTH_PATTERN = (
    r"январ(?:я|ь)|феврал(?:я|ь)|март(?:а)?|апрел(?:я|ь)|мая|июн(?:я|ь)|"
    r"июл(?:я|ь)|август(?:а)?|сентябр(?:я|ь)|октябр(?:я|ь)|ноябр(?:я|ь)|декабр(?:я|ь)"
)
_RUSSIAN_MONTHS = {
    "января": 1, "январь": 1, "февраля": 2, "февраль": 2, "марта": 3, "март": 3,
    "апреля": 4, "апрель": 4, "мая": 5, "июня": 6, "июнь": 6, "июля": 7, "июль": 7,
    "августа": 8, "август": 8, "сентября": 9, "сентябрь": 9, "октября": 10, "октябрь": 10,
    "ноября": 11, "ноябрь": 11, "декабря": 12, "декабрь": 12,
}
REPORT_TYPE_CHOICES = (
    dict(label="Отчёт по стоимости объекта (часы, OB и транспорт)", purpose="customer_pdf"),
    dict(label="Журнал работ по объекту (смены и интервалы)", purpose="journal_pdf"),
    dict(label="Зарплатный отчёт (сводка по сотрудникам)", purpose="payroll_pdf"),
)


def _parse_date_token(value):
    try:
        if "-" in value:
            return date.fromisoformat(value)
        day, month, year = (int(part) for part in value.split("."))
        if year < 100:
            year += 2000 if year < 70 else 1900
        return date(year, month, day)
    except ValueError:
        raise AssistantError("Укажите корректную дату или период.") from None


def _parse_russian_date(day, month, year=None):
    if year is None:
        year = datetime.now(ZoneInfo("Europe/Stockholm")).year
    year = int(year)
    if year < 100:
        year += 2000 if year < 70 else 1900
    try:
        return date(year, _RUSSIAN_MONTHS[month.casefold()], int(day))
    except (KeyError, ValueError):
        raise AssistantError("Укажите корректную дату или период.") from None


def explicit_date_period(text):
    normalized = " ".join(strip_no_change_suffix(text).casefold().split())
    date_range = re.search(rf"\bс\s+({DATE_TOKEN})\s+(?:по|до)\s+({DATE_TOKEN})\b", normalized)
    if date_range:
        start, end = (_parse_date_token(value) for value in date_range.groups())
        if start > end:
            raise AssistantError("Начало периода должно быть не позже окончания.")
        return start.isoformat(), end.isoformat()
    compact_russian_range = re.search(
        rf"\bс\s+(?P<start_day>\d{{1,2}})\s+(?:по|до)\s+(?P<end_day>\d{{1,2}})\s+"
        rf"(?P<month>{_RUSSIAN_MONTH_PATTERN})(?:\s+(?P<year>\d{{2,4}}))?"
        r"(?:\s*(?:года?|г\.))?\b", normalized)
    if compact_russian_range:
        match = compact_russian_range.groupdict()
        start = _parse_russian_date(match["start_day"], match["month"], match["year"])
        end = _parse_russian_date(match["end_day"], match["month"], match["year"])
        if start > end:
            raise AssistantError("Начало периода должно быть не позже окончания.")
        return start.isoformat(), end.isoformat()
    russian_range = re.search(
        rf"\bс\s+(?P<start_day>\d{{1,2}})\s+(?P<start_month>{_RUSSIAN_MONTH_PATTERN})"
        rf"(?:\s+(?P<start_year>\d{{2,4}})(?:\s*(?:года?|г\.))?)?\s+(?:по|до)\s+"
        rf"(?P<end_day>\d{{1,2}})\s+(?P<end_month>{_RUSSIAN_MONTH_PATTERN})"
        rf"(?:\s+(?P<end_year>\d{{2,4}}))?(?:\s*(?:года?|г\.))?\b", normalized)
    if russian_range:
        match = russian_range.groupdict()
        year = match["end_year"] or match["start_year"]
        start = _parse_russian_date(match["start_day"], match["start_month"], match["start_year"] or year)
        end = _parse_russian_date(match["end_day"], match["end_month"], year)
        if start > end:
            raise AssistantError("Начало периода должно быть не позже окончания.")
        return start.isoformat(), end.isoformat()
    single = re.search(rf"\b(?:за|на)\s+({DATE_TOKEN})\b", normalized)
    if single:
        selected = _parse_date_token(single.group(1)).isoformat()
        return selected, selected
    russian_single = re.search(
        rf"\b(?:за|на)\s+(?P<day>\d{{1,2}})\s+(?P<month>{_RUSSIAN_MONTH_PATTERN})"
        rf"(?:\s+(?P<year>\d{{2,4}}))?(?:\s*(?:года?|г\.))?\b", normalized)
    if russian_single:
        match = russian_single.groupdict()
        selected = _parse_russian_date(match["day"], match["month"], match["year"]).isoformat()
        return selected, selected
    return None


def _week_period(text):
    normalized = " ".join(strip_no_change_suffix(text).casefold().split())
    week = re.search(r"\b(текущ\w*|эт\w*|прошл\w*)\s+недел\w*\b|\b(this|last)\s+week\b", normalized)
    if not week:
        return None
    today = datetime.now(ZoneInfo("Europe/Stockholm")).date()
    start = today - timedelta(days=today.weekday())
    if (week.group(1) or "").startswith("прошл") or week.group(2) == "last":
        start -= timedelta(days=7)
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


def _report_type_from_text(text):
    normalized = " ".join(strip_no_change_suffix(text).casefold().split())
    if re.search(r"\b(?:зарплат\w*|payroll|l[oö]ne\w*)", normalized):
        return "payroll_pdf"
    if re.search(r"\b(?:журнал\w*|дневник\w*|arbetsjournal)\b", normalized):
        return "journal_pdf"
    if re.search(r"\b(?:клиентск\w*|фактур\w*|сч[её]т\w*|стоимост\w*)", normalized):
        return "customer_pdf"
    return None


def _report_choice_from_answer(text, choices):
    normalized = " ".join(text.casefold().strip(" .!?").split())
    if normalized.isdecimal() and 1 <= int(normalized) <= len(choices):
        return choices[int(normalized) - 1]["purpose"]
    return _report_type_from_text(normalized)


def report_command(text, context, conn):
    normalized = " ".join(strip_no_change_suffix(text).casefold().strip(" .!?").split())
    if not re.search(r"\b(?:отч[её]т\w*|pdf)\b", normalized) and not (
        context and context["pending"] == "report_type"
    ):
        return None

    previous = context["plan"] if context else {}
    selected_dates = explicit_date_period(text) or _week_period(text)
    if selected_dates is None and previous:
        selected_dates = (previous["date_from"], previous["date_to"])
    if selected_dates is None:
        today = datetime.now(ZoneInfo("Europe/Stockholm")).date()
        start = today - timedelta(days=today.weekday())
        selected_dates = (start.isoformat(), (start + timedelta(days=6)).isoformat())
    base = dict(
        purpose=previous.get("purpose", "search"),
        date_from=selected_dates[0], date_to=selected_dates[1],
        employee=previous.get("employee", ""), company=previous.get("company", ""),
        object_name=previous.get("object_name", ""),
    )

    purpose = _report_choice_from_answer(text, REPORT_TYPE_CHOICES) if (
        context and context["pending"] == "report_type"
    ) else _report_type_from_text(text)
    if purpose:
        base["purpose"] = purpose

    object_scope = re.search(
        r"\bобъект\w*\s+(?P<name>.+?)(?=\s+(?:за|на)\s+|$)", normalized)
    if object_scope and re.match(r"^(?:за|на)\s+", object_scope["name"]):
        object_scope = None
    objects = conn.execute("SELECT company,name FROM meta_objects ORDER BY company,name").fetchall()
    mentioned_objects = [] if object_scope else [
        row for row in objects if row["name"].casefold() in normalized
    ]
    explicit_object_scope = False
    if mentioned_objects:
        explicit_object_scope = True
        longest_name = max(len(row["name"]) for row in mentioned_objects)
        mentioned_objects = [row for row in mentioned_objects if len(row["name"]) == longest_name]
        if len(mentioned_objects) == 1:
            base.update(company=mentioned_objects[0]["company"], object_name=mentioned_objects[0]["name"])
        else:
            raise AssistantQuestion(
                "object", question="Какой объект выбрать? Укажите фирму и объект.",
                choices=[dict(company=row["company"], object_name=row["name"])
                         for row in mentioned_objects[:30]],
                plan=base,
            )
    elif object_scope:
        explicit_object_scope = True
        object_query = object_scope["name"].strip()
        base.update(company="", object_name="")
        query_tokens = {
            token for token in _normalized_name_tokens(object_query)
            if token not in {"za", "na", "s", "po", "v", "dlya", "drugoi", "drugogo", "drugim"}
        }
        object_matches = []
        if query_tokens:
            for row in objects:
                object_tokens = set(_normalized_name_tokens(row["name"]))
                if query_tokens.issubset(object_tokens):
                    object_matches.append((len(object_tokens - query_tokens), row))
        if object_matches:
            best_extra = min(extra for extra, _ in object_matches)
            best_matches = [row for extra, row in object_matches if extra == best_extra]
            if len(best_matches) == 1:
                base.update(company=best_matches[0]["company"], object_name=best_matches[0]["name"])
            else:
                raise AssistantQuestion(
                    "object", question="Какой объект выбрать?",
                    choices=[dict(company=row["company"], object_name=row["name"])
                             for row in best_matches[:10]],
                    plan=base,
                )
        elif query_tokens:
            overlaps = [
                (len(query_tokens & set(_normalized_name_tokens(row["name"]))), row)
                for row in objects
            ]
            best_overlap = max((count for count, _ in overlaps), default=0)
            if best_overlap:
                best_matches = [row for count, row in overlaps if count == best_overlap]
                raise AssistantQuestion(
                    "object", question="Какой объект вы имели в виду?",
                    choices=[dict(company=row["company"], object_name=row["name"])
                             for row in best_matches[:5]],
                    plan=base,
                )
            raise AssistantQuestion(
                "object", question=f"Не нашёл объект «{object_query}». Уточните его название или фирму.",
                plan=base,
            )
        else:
            raise AssistantQuestion("object", question="Назовите объект; при необходимости укажите фирму.",
                                    plan=base)
    elif re.search(r"\bдруг\w*\s+объект\w*\b", normalized):
        explicit_object_scope = True
        base.update(company="", object_name="")
    elif re.search(r"\b(?:фирм\w*|компан\w*)\b", normalized):
        explicit_object_scope = True
        companies = sorted({row["company"] for row in objects
                            if row["company"].casefold() in normalized})
        if companies:
            longest_name = max(len(company) for company in companies)
            companies = [company for company in companies if len(company) == longest_name]
            if len(companies) == 1:
                base["company"] = companies[0]
                if base["object_name"] and not any(
                    row["company"] == base["company"] and row["name"] == base["object_name"]
                    for row in objects
                ):
                    base["object_name"] = ""
            else:
                raise AssistantQuestion(
                    "object", question="Какую фирму выбрать?",
                    choices=[dict(company=company) for company in companies[:30]],
                    plan=base,
                )
        else:
            base.update(company="", object_name="")

    employees = conn.execute("SELECT name FROM meta_employees ORDER BY name").fetchall()
    mentioned_employees = [row["name"] for row in employees if row["name"].casefold() in normalized]
    explicit_employee_scope = False
    if mentioned_employees:
        explicit_employee_scope = True
        longest_name = max(len(name) for name in mentioned_employees)
        mentioned_employees = [name for name in mentioned_employees if len(name) == longest_name]
        if len(mentioned_employees) == 1:
            base["employee"] = mentioned_employees[0]
        else:
            raise AssistantQuestion(
                "scope", question="Какого сотрудника выбрать?",
                choices=[dict(employee=name) for name in mentioned_employees[:30]],
                plan=base,
            )
    elif re.search(r"\bдруг\w*\s+сотрудник\w*\b", normalized):
        explicit_employee_scope = True
        base["employee"] = ""

    if purpose is None:
        object_report = bool(base["object_name"] or re.search(r"\bобъект\w*\b", normalized))
        choices = REPORT_TYPE_CHOICES[:2] if object_report else REPORT_TYPE_CHOICES
        raise AssistantQuestion(
            "report_type",
            question="Какой вариант нужен? Выберите отчёт по стоимости, журнал работ или зарплатную сводку.",
            choices=list(choices),
            plan=validate_plan(base),
        )
    if purpose in {"customer_pdf", "journal_pdf", "catalog_preview"} and not explicit_employee_scope:
        base["employee"] = ""
    if purpose == "payroll_pdf" and not explicit_object_scope:
        base.update(company="", object_name="")
    base["purpose"] = purpose
    return validate_plan(base)


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
    text = " ".join(strip_no_change_suffix(text).split())
    match = re.fullmatch(
        rf"(?:(?:а\s+)?теперь\s+)?(?:(?:проверь(?:те)?|проверить)\s+)?"
        rf"(?:клиентский\s+)?транспорт"
        rf"(?:\s+(?:за|на)\s+(?P<period>текущ\w*\s+недел\w*|эт\w*\s+недел\w*|"
        rf"прошл\w*\s+недел\w*|тот\s+же\s+период|этот\s+же\s+период|этот\s+период)"
        rf"|\s+(?:за|на)\s+(?P<single_date>{DATE_TOKEN})"
        rf"|\s+с\s+(?P<start>{DATE_TOKEN})\s+(?:по|до)\s+(?P<end>{DATE_TOKEN}))?"
        rf"(?:,?\s+(?:для|у)\s+(?P<scope>всех\s+сотрудников|каждого\s+сотрудника|"
        rf"одного\s+сотрудника|сотрудника\s+(?P<name>[^,\n]+)))?"
        rf"[.!?]?",
        text.strip(), re.IGNORECASE)
    if match is None:
        return None
    previous = context["plan"] if context else {}
    today = datetime.now(ZoneInfo("Europe/Stockholm")).date()
    monday = today - timedelta(days=today.weekday())
    if match["period"] and "недел" in match["period"].casefold():
        start = monday
        if match["period"].casefold().startswith("прошл"):
            start -= timedelta(days=7)
        begin, end = start.isoformat(), (start + timedelta(days=6)).isoformat()
    elif match["single_date"]:
        selected = _parse_date_token(match["single_date"]).isoformat()
        begin = end = selected
    elif match["start"]:
        try:
            begin, end = (
                _parse_date_token(value).isoformat()
                for value in (match["start"], match["end"])
            )
        except ValueError:
            raise AssistantError("Укажите корректный период.") from None
    else:
        begin = previous.get("date_from", monday.isoformat())
        end = previous.get("date_to", (monday + timedelta(days=6)).isoformat())
    plan = validate_plan(dict(purpose="transport_check", date_from=begin, date_to=end,
                              employee=(match["name"] or "").strip() if match["name"] else
                              "" if match["scope"] else previous.get("employee", ""),
                              company=previous.get("company", ""), object_name=previous.get("object_name", "")))
    if match["scope"] and match["scope"].casefold() == "одного сотрудника":
        raise AssistantQuestion("scope", plan=plan)
    if (not match["start"] and not match["single_date"]
            and not (match["period"] and "недел" in match["period"].casefold())
            and not context):
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
    submission_check = bool(re.search(
        r"\b(?:сдач\w*\s+час\w*|контрол\w*\s+сдач\w*)\b", normalized
    ))
    hours_request = re.fullmatch(
        r"(?:(?:а\s+)?(?:теперь\s+)?)?(?:покажи\s*,?\s*)?"
        r"(?:кто\s+сколько\s+(?:отработал|работал)"
        r"|сколько\s+час(?:ов|а)?\s+(?:отработал|работал)\s+"
        r"(?:каждый\s+сотрудник|по\s+каждому\s+сотруднику|сотрудники)"
        r"|(?:отработанн\w*\s+)?час(?:ы|ов|а)\s+(?:по\s+каждому\s+сотруднику|"
        r"каждого\s+сотрудника|всех\s+сотрудников))"
        r"(?:\s+.+)?",
        normalized,
    )
    hours_request = bool(submission_check or hours_request or (
        re.search(r"\bчас(?:ы|ов|а)\b", normalized) and
        re.search(r"\b(?:по\s+каждому\s+сотруднику|каждого\s+сотрудника|всех\s+сотрудников)\b", normalized)
    ) or re.match(r"^(?:(?:покажи|посчитай)\s+)?часы(?:\s|$)", normalized))
    if hours_request:
        purpose = "hours_summary"
    elif (re.search(r"\bдубл\w*", normalized) or re.search(
            r"\b(?:пропущен|отсутств|без|не\s+задан)\w*\s+ставк\w*", normalized) or
          re.search(r"\b(?:проблем\w*|ошибк\w*)\s+(?:со?\s+)?ставк\w*", normalized)):
        purpose = "anomalies"
    else:
        return None

    if re.search(r"\b(?:объект|фирм|компан)\b", normalized) or (
            not hours_request and re.search(
                r"\b(?:сотрудник|для\s+(?!всех\s+сотрудник))", normalized)):
        return None

    previous = context["plan"] if context else {}
    date_period = explicit_date_period(normalized)
    week_period = _week_period(normalized)
    if date_period:
        begin, end = date_period
    elif week_period:
        begin, end = week_period
    elif previous:
        begin, end = previous["date_from"], previous["date_to"]
    else:
        today = datetime.now(ZoneInfo("Europe/Stockholm")).date()
        start = today - timedelta(days=today.weekday())
        begin, end = start.isoformat(), (start + timedelta(days=6)).isoformat()

    employee_text = ""
    if submission_check:
        explicit_employee = re.search(
            r"\b(?:для\s+)?сотрудник\w*\s+(.+?)(?=\s+(?:за|на|с|прошл\w*\s+недел\w*|"
            r"текущ\w*\s+недел\w*)\b|$)",
            text.strip(" .!?"), re.IGNORECASE)
        if explicit_employee:
            employee_text = explicit_employee.group(1).strip(" ,.")
    elif hours_request and not re.search(
            r"\b(?:каждый\s+сотрудник|по\s+каждому\s+сотруднику|все\s+сотрудники)\b", normalized):
        employee_match = re.match(
            r"^(?:(?:а\s+)?теперь\s+)?(?:покажи\s*,?\s*)?часы(?:\s+(?P<name>.*))?$",
            text.strip(" .!?"), re.IGNORECASE)
        if employee_match and employee_match["name"]:
            employee_text = re.split(
                r"\s+(?:за|на|с)\s+", employee_match["name"], maxsplit=1, flags=re.IGNORECASE)[0]
            employee_text = re.sub(
                r"^по\s+(?:каждому\s+)?сотрудник\w*\s*", "", employee_text, flags=re.IGNORECASE).strip()
            if not re.search(r"[a-zа-яё]", employee_text, re.IGNORECASE):
                employee_text = ""

    plan = validate_plan(dict(
        purpose=purpose, date_from=begin, date_to=end,
        employee=employee_text if submission_check else "" if re.search(
            r"\b(?:каждый\s+сотрудник|по\s+каждому\s+сотруднику|все\s+сотрудники)\b", normalized
        ) else employee_text or previous.get("employee", ""),
        company="" if submission_check else previous.get("company", ""),
        object_name="" if submission_check else previous.get("object_name", "")))
    if submission_check and not (date_period or week_period or previous):
        raise AssistantQuestion("period", plan=plan)
    if not (date_period or week_period or previous):
        raise AssistantQuestion("period", plan=plan)
    return plan


def wants_hours_summary_only(text):
    normalized = " ".join(strip_no_change_suffix(text).casefold().split())
    return bool(re.search(r"\bтолько\s+итог\w*\b", normalized) and
                re.search(r"\bбез\s+(?:списка\s+)?(?:всех\s+)?смен\w*\b", normalized))


def compact_hours_summary(result):
    table = result.get("table")
    if not isinstance(table, dict):
        return
    if "processed_records" in result:
        result["summary_only"] = True
        return
    rows = [[row[0], str(row[1])] for row in table["rows"] if row[0] != "Общий итог"]
    total = sum((Decimal(row[1]) for row in rows), Decimal("0"))
    total_text = format(total.normalize(), "f")
    rows.append(["Общий итог", total_text])
    result["table"] = dict(headers=["Сотрудник", "Отработано, ч"], rows=rows)
    result["summary"] = f"Сотрудников: {len(rows) - 1}. Общий итог отработанного времени: {total_text} ч."
    result["summary_only"] = True


def submission_hours_summary(conn, plan):
    profiles_by_key = {}
    for profile in conn.execute("SELECT id,name,is_active FROM meta_employees"):
        key = " ".join(str(profile["name"] or "").split()).casefold()
        profiles_by_key.setdefault(key, []).append(profile)

    filters = ["s.is_deleted=0", "s.date>=?", "s.date<=?"]
    values = [plan["date_from"], plan["date_to"]]
    employee_key = ""
    if plan["employee"]:
        employee_key = " ".join(plan["employee"].split()).casefold()
        filters.append("normalize_employee_name(s.employee)=?")
        values.append(employee_key)
    if plan["company"]:
        filters.append("s.company=?")
        values.append(plan["company"])
    if plan["object_name"]:
        filters.append("s.object_name=?")
        values.append(plan["object_name"])

    totals = {}
    processed = 0
    query = (
        "SELECT s.employee,s.submission_type,s.hours,s.date,s.company,s.object_name "
        "FROM incoming_submissions s WHERE " + " AND ".join(filters) + " ORDER BY s.date,s.id"
    )
    cursor = conn.execute(query, values)
    for raw in cursor:
        processed += 1
        name = str(raw["employee"] or "").strip()
        key = " ".join(name.split()).casefold()
        matches = profiles_by_key.get(key, [])
        profile = matches[0] if len(matches) == 1 else None
        if profile:
            group_key = f"profile:{profile['id']}"
            display_name = profile["name"]
            link_status = "Неактивен в справочнике" if not profile["is_active"] else "Связан"
            employee_id = profile["id"]
        else:
            group_key = f"unlinked:{key or '<имя отсутствует>'}"
            display_name = name or "Имя сотрудника отсутствует"
            link_status = ("Неоднозначная связь со справочником"
                           if len(matches) > 1 else "Нет связи со справочником")
            employee_id = None
        employee_totals = totals.setdefault(group_key, {
            "employee": display_name,
            "employee_id": employee_id,
            "link_status": link_status,
            "work": Decimal("0"),
            "ob": Decimal("0"),
            "atf": Decimal("0"),
            "sick": Decimal("0"),
            "vacation": Decimal("0"),
            "holiday": Decimal("0"),
            "unknown": Decimal("0"),
            "records": 0,
        })
        kind = str(raw["submission_type"] or "")
        hours = Decimal(str(raw["hours"] or 0))
        employee_totals["records"] += 1
        if kind in {"work", "ob"}:
            employee_totals[kind] += hours
        elif kind in {"atf", "sick", "vacation", "holiday"}:
            employee_totals[kind] += hours
        else:
            employee_totals["unknown"] += hours

    duplicate_query = (
        "SELECT s.date,s.employee,s.company,s.object_name,s.hours,s.submission_type,COUNT(*) AS copies "
        "FROM incoming_submissions s WHERE " + " AND ".join(filters) +
        " GROUP BY s.date,normalize_employee_name(s.employee),s.company,s.object_name,"
        "s.hours,s.submission_type HAVING COUNT(*)>1 ORDER BY s.date,s.employee,s.company,s.object_name"
    )
    duplicate_groups = []
    duplicate_records = 0
    for raw in conn.execute(duplicate_query, values):
        key = " ".join(str(raw["employee"] or "").split()).casefold()
        matches = profiles_by_key.get(key, [])
        employee = matches[0]["name"] if len(matches) == 1 else str(raw["employee"] or "Имя отсутствует")
        copies = int(raw["copies"])
        duplicate_records += copies
        duplicate_groups.append({
            "date": raw["date"],
            "employee": employee,
            "company": raw["company"] or "",
            "object_name": raw["object_name"] or "",
            "hours": str(raw["hours"] or 0),
            "submission_type": raw["submission_type"] or "не указан",
            "copies": copies,
        })

    totals_by_kind = {kind: sum((entry[kind] for entry in totals.values()), Decimal("0"))
                      for kind in ("work", "ob", "atf", "sick", "vacation", "holiday", "unknown")}
    overall_work = totals_by_kind["work"] + totals_by_kind["ob"]

    def display(value):
        return format(value.normalize(), "f") if value else "0"

    rows = []
    for entry in sorted(totals.values(), key=lambda item: (item["employee"].casefold(), item["employee_id"] or 0)):
        rows.append([
            entry["employee"], entry["link_status"],
            display(entry["work"] + entry["ob"]), display(entry["work"]), display(entry["ob"]),
            display(entry["atf"]), display(entry["sick"]), display(entry["vacation"]),
            display(entry["holiday"]), display(entry["unknown"]), entry["records"],
        ])
    rows.append([
        "Общий итог", "", display(overall_work), display(totals_by_kind["work"]),
        display(totals_by_kind["ob"]), display(totals_by_kind["atf"]),
        display(totals_by_kind["sick"]), display(totals_by_kind["vacation"]),
        display(totals_by_kind["holiday"]), display(totals_by_kind["unknown"]), processed,
    ])
    employee_count = len(totals)
    linked_count = sum(entry["employee_id"] is not None for entry in totals.values())
    unlinked_names = sorted({entry["employee"] for entry in totals.values()
                             if entry["employee_id"] is None}, key=str.casefold)
    unknown_types = sorted({
        str(row[0] or "не указан")
        for row in conn.execute(
            "SELECT DISTINCT s.submission_type FROM incoming_submissions s WHERE " +
            " AND ".join(filters) + " AND COALESCE(s.submission_type,'') NOT IN "
            "('work','ob','atf','sick','vacation','holiday')",
            values,
        )
    }, key=str.casefold)
    warnings = [
        "Без достоверного графика или назначений нельзя определить, кто должен был сдать часы; "
        "отсутствие записи не означает несдачу."
    ]
    if unlinked_names:
        warnings.append("Нет однозначной связи со справочником сотрудников: " + ", ".join(unlinked_names) + ".")
    if unknown_types:
        warnings.append("Записи неизвестных типов включены в число обработанных, но не в рабочие часы: " +
                        ", ".join(unknown_types) + ".")
    if duplicate_groups:
        warnings.append(
            f"Возможные дубли: {len(duplicate_groups)} групп, затрагивают {duplicate_records} записей. "
            "Они показаны отдельно и не вычтены из сумм."
        )
        warnings.extend(
            f'{group["date"]}: {group["employee"]}, {group["company"]} / {group["object_name"]}, '
            f'{group["submission_type"]}, {group["hours"]} ч — {group["copies"]} совпадающих записей.'
            for group in duplicate_groups
        )
    summary = (
        f'Период {plan["date_from"]} — {plan["date_to"]}: обработано записей {processed}; '
        f'сотрудников/отдельных групп {employee_count} (связано со справочником {linked_count}); '
        f'отработано {display(overall_work)} ч, включая OB {display(totals_by_kind["ob"])} ч. '
        f'Остальные категории отдельно: ATF {display(totals_by_kind["atf"])}, '
        f'больничные {display(totals_by_kind["sick"])}, отпуск {display(totals_by_kind["vacation"])}, '
        f'праздничные {display(totals_by_kind["holiday"])} ч. '
        'OB входит в отработанное время и не прибавляется повторно.'
    )
    return {
        "table": {
            "headers": [
                "Сотрудник", "Связь со справочником", "Отработано, ч (OB включён)",
                "Обычные часы, ч", "OB, ч (включены)", "ATF, ч", "Больничные, ч",
                "Отпуск, ч", "Праздничные, ч", "Неизвестные типы, ч", "Записей",
            ],
            "rows": rows,
        },
        "summary": summary,
        "processed_records": processed,
        "employee_count": employee_count,
        "linked_employee_count": linked_count,
        "duplicate_groups": duplicate_groups,
        "duplicate_record_count": duplicate_records,
        "warnings": warnings,
    }


def select_plan(text, conn, actor, operation, post=None, context=None):
    post = post or requests.post
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 8000:
        raise AssistantError("Введите команду длиной не более 8000 байт.")
    text = strip_no_change_suffix(text)
    safe_context = validate_context(context)
    normalized = " ".join(text.casefold().strip(" .!?").split())
    date_period = explicit_date_period(text)
    try:
        known_plan = report_command(text, safe_context, conn)
        if not known_plan:
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
        "For an ambiguous report request use report_type and offer the available human-readable report choices. "
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
                if date_period:
                    selection["date_from"], selection["date_to"] = date_period
            choices = list(REPORT_TYPE_CHOICES if clarification["field"] == "report_type" else ())
            raise AssistantQuestion(clarification["field"], choices=choices, plan=selection)
        if len(calls) == 1 and calls[0].get("name") == "run_workflow":
            workflow = json.loads(calls[0]["arguments"])
            if (not isinstance(workflow, dict) or set(workflow) != {"steps"} or
                    not isinstance(workflow["steps"], list) or not 2 <= len(workflow["steps"]) <= 4):
                raise AssistantError("Последовательность должна содержать от двух до четырёх проверок или отчётов.")
            plans = [validate_plan(step) for step in workflow["steps"]]
            if date_period:
                for step in plans:
                    step["date_from"], step["date_to"] = date_period
            if any(step["purpose"] in NATIVE_ACTIONS for step in plans):
                raise AssistantError("Редактирование и открытие финансовых форм выполняются отдельным действием, не цепочкой.")
            finish(conn, actor, operation, "text", "routed")
            return dict(workflow=plans)
        if len(calls) != 1 or calls[0].get("name") != "inspect_records":
            raise AssistantError("Помощник не выбрал поддерживаемую функцию. Выберите действие кнопкой ниже; "
                                 "период и сотрудника можно задать в полях над кнопками.")
        plan = validate_plan(json.loads(calls[0]["arguments"]))
        if date_period:
            plan["date_from"], plan["date_to"] = date_period
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
    if not isinstance(context["pending"], str) or context["pending"] not in {
        "", "period", "scope", "object", "report_type", "unsupported"
    }:
        raise AssistantError("Неизвестное уточнение диалога.")
    return dict(plan=validate_plan(context["plan"]).copy(), pending=context["pending"])


_CYRILLIC_NAME_TRANSLATION = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})
_EMPLOYEE_GIVEN_NAME_ALIASES = {
    "andrei": "andrii",
    "andrey": "andrii",
    "andriy": "andrii",
}


def _normalized_name_tokens(value):
    transliterated = value.casefold().translate(_CYRILLIC_NAME_TRANSLATION)
    tokens = re.findall(r"[a-z0-9]+", transliterated)
    if tokens:
        tokens[0] = _EMPLOYEE_GIVEN_NAME_ALIASES.get(tokens[0], tokens[0])
    return tokens


def _employee_name_similarity(requested, candidate):
    requested_tokens = _normalized_name_tokens(requested)
    candidate_tokens = _normalized_name_tokens(candidate)
    if (len(requested_tokens) < 2 or len(requested_tokens) != len(candidate_tokens) or
            requested_tokens[0] != candidate_tokens[0]):
        return 0.0
    similarities = [
        SequenceMatcher(None, requested_token, candidate_token).ratio()
        for requested_token, candidate_token in zip(requested_tokens, candidate_tokens)
    ]
    if any(similarity < 0.8 for similarity in similarities):
        return 0.0
    return min(similarities)


def _report_object_choices(conn, plan):
    rows = conn.execute(
        """SELECT DISTINCT o.company, o.name
           FROM meta_objects AS o
           WHERE LOWER(COALESCE(o.markning, '')) NOT IN ('p1000', 'p1001', 'p1002', 'p1004')
             AND (
                 COALESCE(o.is_active, 1) = 1
                 OR EXISTS (
                     SELECT 1
                     FROM incoming_submissions AS s
                     WHERE s.company = o.company
                       AND s.object_name = o.name
                       AND s.date BETWEEN ? AND ?
                       AND s.submission_type IN ('work', 'ob')
                       AND COALESCE(s.is_deleted, 0) = 0
                 )
             )
             AND (? = '' OR o.company = ?)
           ORDER BY o.company, o.name""",
        (plan["date_from"], plan["date_to"], plan["company"], plan["company"]),
    ).fetchall()
    return [dict(company=row["company"], object_name=row["name"]) for row in rows]


def resolve_selection(conn, plan):
    plan = plan.copy()
    if plan["employee"]:
        rows = conn.execute("SELECT id,name FROM meta_employees ORDER BY id").fetchall()
        needle = plan["employee"].casefold()
        exact = [row for row in rows if row["name"].casefold() == needle]
        matches = exact or [row for row in rows if needle in row["name"].casefold()]
        fuzzy_needs_confirmation = False
        if not matches:
            scored = [
                (similarity, row) for row in rows
                if (similarity := _employee_name_similarity(plan["employee"], row["name"])) > 0
            ]
            if scored:
                best_score = max(score for score, _ in scored)
                matches = [row for score, row in scored if best_score - score < 0.08]
                fuzzy_needs_confirmation = best_score < 0.9 or len(matches) > 1
        if len(matches) != 1 or fuzzy_needs_confirmation:
            if matches:
                question = "Уточните, какого сотрудника выбрать:"
                choices = [dict(employee=row["name"]) for row in matches[:5]]
            else:
                question = ("Не удалось сопоставить распознанное имя «" + plan["employee"] +
                            "» со справочником. Проверьте ФИО или произнесите его ещё раз.")
                choices = []
            raise AssistantQuestion("scope", question=question, choices=choices, plan=plan)
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
        choices = _report_object_choices(conn, plan)
        count = len(choices)
        question = (
            f"Выберите объект из списка: {count} рабочих объектов. "
            "Архивные объекты с отработанными часами за этот период включены."
            if count else
            "Нет доступных рабочих объектов с учётом выбранного периода. Проверьте период."
        )
        raise AssistantQuestion("object", plan=plan,
                                question=question, choices=choices)
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
    if plan["purpose"] == "hours_summary":
        result = submission_hours_summary(conn, plan)
        employee = None
        if plan["employee"]:
            employee_row = conn.execute(
                "SELECT id,name FROM meta_employees WHERE name=? COLLATE NOCASE",
                (plan["employee"],),
            ).fetchone()
            employee = dict(employee_row) if employee_row else None
        result.update({
            "plan": plan,
            "employee": employee,
            "object": None,
            "records": [],
            "source": (
                "Источник: серверная база SmartReport. Обработаны все неудалённые записи "
                "выбранного периода без лимита 500; транспорт и расходы в часы не включаются."
            ),
            "notice": "\n".join(result.pop("warnings")),
            "context": dict(plan=plan, pending=""),
        })
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
        catalog_rate = None
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
                catalog_rate = objects[0]["rate"]
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
                            customer_rate=prices["base"], catalog_rate=catalog_rate,
                            travel_rate=prices["travel"],
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
    elif plan["purpose"] == "anomalies":
        result["checked_count"] = len(records)
        missing = [r for r in records if r["entry_type"] in {"work", "ob"} and r["customer_rate"] is None and not r["fixed"]]
        changed_rates = [r for r in records if r["entry_type"] in {"work", "ob"} and not r["fixed"]
                         and r["customer_rate"] is not None and r["catalog_rate"] is not None
                         and r["customer_rate"] != r["catalog_rate"]]
        result["summary"] = (f"Проверено {len(records)} записей. Без клиентской ставки: {len(missing)}; "
                             f"ставка отличается от справочника: {len(changed_rates)}; "
                             f"возможные дубли: {len(repeated)} записей. "
                             "Отличие ставки требует проверки; автоматического удаления и замены цен нет.")
        result["records"] = [r for r in records if r in missing or r in changed_rates or r["duplicate"]]
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
            if plan.get("purpose") == "hours_summary" and wants_hours_summary_only(data["text"]):
                compact_hours_summary(results[0])
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
