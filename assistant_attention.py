"""Deterministic, read-only preparation of the director's attention list."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import re

from assistant_api import explicit_date_period, _week_period
from customer_pricing import require_final_prices, row_prices


def is_attention_request(text):
    text = " ".join(text.casefold().strip(" .!?").split())
    return bool(re.match(
        r"^(?:что требует моего внимания|что нужно проверить|что осталось незаверш[её]нным|"
        r"покажи проблемы)(?:\s|$)", text))


def periods(text, today=None):
    today = today or datetime.now(ZoneInfo("Europe/Stockholm")).date()
    explicit = explicit_date_period(text) or _week_period(text)
    if not explicit and re.search(r"\b(?:эт\w*|текущ\w*|прошл\w*|предыдущ\w*)\s+месяц\w*\b",
                                  text.casefold()):
        month = today.replace(day=1)
        if re.search(r"\b(?:прошл\w*|предыдущ\w*)\s+месяц\w*\b", text.casefold()):
            month = (month - timedelta(days=1)).replace(day=1)
        next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        explicit = month.isoformat(), (next_month - timedelta(days=1)).isoformat()
    if not explicit and re.search(r"\bпредыдущ\w*\s+недел\w*\b", text.casefold()):
        monday = today - timedelta(days=today.weekday())
        explicit = (monday - timedelta(days=7)).isoformat(), (monday - timedelta(days=1)).isoformat()
    if explicit:
        return {"hours": explicit, "prices": explicit, "documents": explicit}
    if re.search(r"\b(?:за|с|по|недел\w*|месяц\w*|год\w*)\b", text.casefold()):
        raise ValueError("Не удалось определить указанный период. Укажите даты с … по … или неделю.")
    monday = today - timedelta(days=today.weekday())
    next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    return {
        "hours": ((monday - timedelta(days=7)).isoformat(), (monday - timedelta(days=1)).isoformat()),
        "prices": (today.replace(day=1).isoformat(), (next_month - timedelta(days=1)).isoformat()),
        "documents": None,
    }


def scope_for_text(text, objects):
    normalized = " ".join(text.casefold().split())

    def contains(value):
        value = " ".join(str(value or "").casefold().split())
        return bool(value and re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", normalized))

    companies = {str(item["company"]) for item in objects if contains(item["company"])}
    if len(companies) > 1:
        raise ValueError("Указано несколько фирм; выберите одну область проверки.")
    company = next(iter(companies), "")
    matches = [item for item in objects if contains(item["name"])
               and (not company or item["company"] == company)]
    if matches:
        longest = max(len(item["name"]) for item in matches)
        matches = [item for item in matches if len(item["name"]) == longest]
        identities = {(item["company"], item["name"]) for item in matches}
        if len(identities) != 1:
            raise ValueError("Название объекта неоднозначно; укажите также фирму.")
        company, object_name = next(iter(identities))
        return company, object_name
    if re.search(r"\bобъект\w*", normalized):
        raise ValueError("Указанный объект не найден однозначно в локальном справочнике.")
    if not company and re.search(r"\b(?:фирм\w*|компан\w*)", normalized):
        raise ValueError("Указанная фирма не найдена однозначно в локальном справочнике.")
    return company, ""


def price_issues(rows):
    issues = []
    for row in rows:
        if row.get("entry_type") not in ("work", "ob"):
            continue
        identity = row.get("sync_id")
        if not identity and row.get("id") is not None:
            identity = f"local:{row['id']}"
        try:
            prices = row_prices(row)
        except (ValueError, TypeError, KeyError):
            issues.append({
                "kind": "shift", "id": identity,
                "date": row.get("date"), "employee": row.get("employee"),
                "company": row.get("company"), "object_name": row.get("object_name"),
                "reason": "сохранённые клиентские условия не удалось прочитать", "unverified": True,
            })
            continue
        conditions = []
        if prices["base"] is None and not prices.get("fixed"):
            conditions.append("неизвестна клиентская почасовая цена")
        if row.get("transport") and prices["travel"] is None and not prices.get("fixed"):
            conditions.append("неизвестна клиентская цена транспорта")
        try:
            require_final_prices(row, row.get("_attention_segments", []))
        except (ValueError, KeyError, TypeError) as error:
            if not conditions or row["entry_type"] == "ob" or prices.get("fixed") or prices.get("interval_terms"):
                conditions.append(str(error) or "клиентские условия не удалось проверить")
        if conditions:
            issues.append({
                "kind": "shift", "id": identity,
                "date": row.get("date"), "employee": row.get("employee"),
                "company": row.get("company"), "object_name": row.get("object_name"),
                "reason": "; ".join(conditions),
            })
    return issues


def remote_price_issues(rows):
    issues, unverified = [], []
    for row in rows:
        if row.get("entry_type") not in ("work", "ob"):
            continue
        conditions = []
        if row.get("customer_rate") is None and not row.get("fixed"):
            conditions.append("неизвестна клиентская почасовая цена")
        if row.get("trips") and row.get("travel_rate") is None and not row.get("fixed"):
            conditions.append("неизвестна клиентская цена транспорта")
        if row.get("entry_type") == "ob" or row.get("prices", {}).get("interval_terms"):
            unverified.append(row.get("id"))
        if conditions:
            issues.append({
                "kind": "shift", "id": row.get("id"), "date": row.get("date"),
                "employee": row.get("employee"), "company": row.get("company"),
                "object_name": row.get("object_name"), "reason": "; ".join(conditions),
            })
    return issues, unverified


def missing_hours(employees, rows, period, company="", object_name=""):
    from assistant_api import submission_hours_summary
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.create_function("normalize_employee_name", 1, lambda value: " ".join(
        str(value or "").split()).casefold())
    try:
        conn.executescript("""
            CREATE TABLE meta_employees(id INTEGER, name TEXT, is_active INTEGER);
            CREATE TABLE meta_objects(id INTEGER, company TEXT, name TEXT);
            CREATE TABLE incoming_submissions(id INTEGER, employee TEXT, submission_type TEXT,
                hours REAL, date TEXT, company TEXT, object_name TEXT, is_deleted INTEGER);
        """)
        conn.executemany("INSERT INTO meta_employees VALUES (?,?,?)", [
            (index, item["name"], int(bool(item["is_active"])))
            for index, item in enumerate(employees, 1)
        ])
        conn.executemany("INSERT INTO incoming_submissions VALUES (?,?,?,?,?,?,?,0)", [
            (index, row.get("employee"), row.get("entry_type"), row.get("hours"),
             row.get("date"), row.get("company"), row.get("object_name"))
            for index, row in enumerate(rows, 1)
        ])
        result = submission_hours_summary(conn, {
            "date_from": period[0], "date_to": period[1], "employee": "",
            "company": company, "object_name": object_name,
        }, director_status=True)
        missing = [{"kind": "employee", "employee": row[1],
                    "reason": "Нет записей часов за период"}
                   for row in result["table"]["rows"] if row[0] == "Без внесённых часов"]
        warnings = list(result["warnings"])
        if result.get("employees_with_ambiguous_records"):
            warnings.append("Неоднозначные сотрудники: проверка часов неполная.")
        return missing, warnings
    finally:
        conn.close()


def document_issues(documents, status_key, unallocated, remaining, period=None):
    review, allocation = [], []
    for doc in documents:
        if status_key(doc) == "ignored":
            continue
        date = doc.get("document_date")
        if period and date and not period[0] <= date <= period[1]:
            continue
        record = {
            "kind": "document", "id": doc.get("id"), "client_id": doc.get("client_id"),
            "incoming_document_id": doc.get("incoming_document_id"),
            "date": date, "supplier": doc.get("supplier_name"),
            "number": doc.get("invoice_number") or doc.get("source_filename"),
        }
        reasons = []
        if not doc.get("supplier_name"):
            reasons.append("поставщик неизвестен")
        if doc.get("gross_amount") is None:
            reasons.append("сумма неизвестна")
        if not doc.get("currency_code"):
            reasons.append("валюта неизвестна")
        if doc.get("sync_error"):
            reasons.append(str(doc["sync_error"]))
        extra = doc.get("review_reasons")
        if isinstance(extra, str) and extra.strip():
            reasons.append(extra.strip())
        elif isinstance(extra, list):
            reasons.extend(str(value) for value in extra if value)
        if status_key(doc) != "accounted":
            review.append({**record, "reason": "; ".join(reasons) or "проверка не завершена"})
        if unallocated(doc):
            value = remaining(doc)
            currency = doc.get("currency_code") or "валюта неизвестна"
            basis = "без НДС" if doc.get("allocation_basis") == "net" else "с НДС"
            allocation.append({**record, "reason": (
                f"остаток {basis} неизвестен" if value is None else
                f"не распределено {basis}: {value} {currency}"
            )})
    return review, allocation
