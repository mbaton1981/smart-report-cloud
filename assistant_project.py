"""Read-only helpers for the assistant's object overview."""

import calendar
from datetime import datetime
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

import assistant_attention
from assistant_api import (
    _RUSSIAN_MONTH_PATTERN, _RUSSIAN_MONTHS, _week_period, explicit_date_period,
)
from customer_report import prepare_customer_costs
from expense_documents import allocation_breakdown, parse_decimal
from fixed_pricing import report_agreements


def project_request(text):
    normalized = " ".join(text.casefold().split())
    return bool(
        re.search(r"\bчто\s+(?:сейчас\s+)?по\s+", normalized)
        or re.search(r"\b(?:состояни\w*|час\w*|расход\w*|заработал\w*)\b.*\bобъект\w*\b", normalized)
        or re.search(r"\bсколько\s+заработал\w*\b", normalized)
    )


def project_period(text, today=None):
    today = today or datetime.now(ZoneInfo("Europe/Stockholm")).date()
    explicit = explicit_date_period(text) or _week_period(text)
    if explicit:
        return explicit
    normalized = text.casefold()
    if re.search(r"\bза\s+весь\s+объект\b", normalized):
        return None
    relative = re.search(r"\bза\s+(эт\w*|текущ\w*|прошл\w*|предыдущ\w*)\s+месяц\w*\b", normalized)
    if relative:
        year, number = today.year, today.month
        if relative.group(1).startswith(("прошл", "предыдущ")):
            year, number = (year - 1, 12) if number == 1 else (year, number - 1)
        return f"{year:04d}-{number:02d}-01", f"{year:04d}-{number:02d}-{calendar.monthrange(year, number)[1]:02d}"
    month = re.search(
        rf"\bза\s+({_RUSSIAN_MONTH_PATTERN})(?:\s+(\d{{4}}))?\b",
        normalized,
    )
    if month:
        number = _RUSSIAN_MONTHS[month.group(1)]
        year = int(month.group(2)) if month.group(2) else today.year
        last_day = calendar.monthrange(year, number)[1]
        return f"{year:04d}-{number:02d}-01", f"{year:04d}-{number:02d}-{last_day:02d}"
    if re.search(r"\b(?:за|с|недел\w*|месяц\w*|год\w*)\b", normalized):
        raise ValueError("Не удалось определить период. Укажите даты или месяц.")
    return None


def project_candidates(text, objects):
    normalized = " ".join(text.casefold().split())
    identifiers = [obj for obj in objects if obj.get("sync_id") and re.search(
        r"(?<![\w-])" + re.escape(str(obj["sync_id"]).casefold()) + r"(?![\w-])", normalized)]
    if identifiers:
        return identifiers
    marking = re.search(r"(?<!\w)p\d+(?!\w)", normalized)
    if marking:
        return [obj for obj in objects if str(obj.get("markning") or "").casefold() == marking.group()]
    matches = []
    for obj in objects:
        name = " ".join(str(obj.get("name") or "").casefold().split())
        if not name:
            continue
        if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", normalized):
            matches.append(obj)
            continue
        words = re.findall(r"[\w]+", name)
        if words and re.search(r"(?<!\w)" + re.escape(words[0]) + r"(?!\w)", normalized):
            matches.append(obj)
    return matches


def expense_summary(documents, object_id, period=None):
    totals = {}
    unknown = set()
    undated = []
    linked = []
    errors = []
    for document in documents:
        parts = document.get("allocations") or []
        matching = [part for part in parts if part.get("object_id") is not None
                    and str(part["object_id"]) == str(object_id)
                    and not part.get("company_expense")
                    and part.get("destination") != "company"]
        if not matching or document.get("verification_status") in ("ignored", "not_to_account") \
                or document.get("incoming_action") == "ignore":
            continue
        day = document.get("document_date")
        if period and not day:
            undated.append(document)
            continue
        if period and not period[0] <= day <= period[1]:
            continue
        linked.append(document)
        if document.get("verification_status") != "confirmed" or document.get("sync_state") in (
                "pending", "conflict", "unverified") or document.get("sync_error"):
            continue
        calculated = None
        if document.get("allocation_basis") != "net" and all(
                parse_decimal(part.get("gross_amount")) is not None for part in parts):
            gross = parse_decimal(document.get("gross_amount"))
            if gross is not None:
                try:
                    calculated = allocation_breakdown(
                        gross, document.get("net_amount"), document.get("vat_amount"), parts,
                    )["allocations"]
                except ValueError as error:
                    errors.append("Не удалось проверить распределение документа: " + str(error))
                    calculated = None
        currency = str(document.get("currency_code") or "неизвестная валюта").upper()
        bucket = totals.setdefault(currency, {"net_amount": None, "vat_amount": None,
                                              "gross_amount": None})
        for index, part in enumerate(parts):
            if part not in matching:
                continue
            for key in bucket:
                value = parse_decimal(part.get(key))
                if value is None and calculated is not None:
                    value = calculated[index].get(key)
                if value is None:
                    unknown.add((currency, key))
                else:
                    bucket[key] = (bucket[key] or Decimal(0)) + value
    return {"totals": totals, "unknown": unknown, "undated": undated, "linked": linked,
            "errors": errors}


def shift_summary(overview, period=None):
    rows = overview["journal_rows"]
    report = overview["customer_report"]
    agreements, allocations = report_agreements(report)
    lines = [dict(line) for line in report["cost_lines"]]
    incomplete_fixed = False
    for agreement in agreements.values():
        if not (report["fixed_complete"] and (period is None or
                period[0] <= agreement["date_from"] and agreement["date_to"] <= period[1])):
            incomplete_fixed = True
            for line in lines:
                if line["label"].startswith("Fast pris") and (
                        f"{agreement['date_from']} – {agreement['date_to']}" in line["label"]):
                    line["amount"] = None
    unknown_records = 0
    for row in rows:
        if row["sync_id"] in allocations:
            complete = report["fixed_complete"] and not incomplete_fixed and all(
                line["amount"] is not None for line in lines)
        else:
            single = {"journal_rows": [row], "journal_segments": overview["journal_segments"],
                      "warnings": [], "totals": {"without_ob_hours":
                          None if row["entry_type"] == "ob" and report["totals"]["without_ob_hours"] is None
                          else 0.0}}
            prepare_customer_costs(single)
            complete = single["totals"]["invoice_total"] is not None and not any(
                agreement["date_from"] <= row["date"] <= agreement["date_to"]
                for agreement in agreements.values())
        unknown_records += not complete
    known_values = [Decimal(str(line["amount"])) for line in lines if line["amount"] is not None]
    unknown_cost = any(line["amount"] is None for line in lines)
    known_subtotal = float(sum(known_values)) if known_values or not unknown_cost else None
    def component(prefixes):
        selected = [line["amount"] for line in lines if line["label"].startswith(prefixes)]
        return sum(selected) if all(value is not None for value in selected) else None
    issues = assistant_attention.price_issues([
        {**row, "_attention_segments": overview["journal_segments"].get(row.get("sync_id"), [])}
        for row in rows
    ])
    statuses = {"В работе": 0, "Часы подтверждены": 0, "Выставлено в счёт": 0}
    for row in rows:
        status = ("Выставлено в счёт" if row.get("invoiced") else
                  "Часы подтверждены" if row.get("hours_confirmed") else "В работе")
        statuses[status] += 1
    return {
        "hours": overview["hours"],
        "employees": overview["employees"],
        "ordinary": report["totals"]["without_ob_hours"],
        "ob": (None if report["totals"]["without_ob_hours"] is None else
               overview["hours"] - report["totals"]["without_ob_hours"]),
        "statuses": statuses,
        "cost_lines": lines,
        "known_subtotal": known_subtotal,
        "total": None if unknown_cost else known_subtotal,
        "unknown_records": unknown_records,
        "work_amount": component(("Arbete", "Fast pris")),
        "ob_amount": component(("OB", "Kundtillägg")),
        "transport_amount": component(("Resa",)),
        "incomplete_fixed": incomplete_fixed,
        "issues": issues,
        "warnings": report.get("warnings", []),
    }
