"""Read-only helpers for the assistant's object overview."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
import re
from zoneinfo import ZoneInfo

import assistant_attention
from assistant_api import (
    _normalized_name_tokens, _week_period, explicit_date_period,
)
from customer_report import prepare_customer_costs
from expense_documents import allocation_breakdown, parse_decimal
from fixed_pricing import report_agreements


def project_request(text, objects=None):
    normalized = " ".join(text.casefold().split())
    if re.search(r"\bсотрудник\w*", normalized):
        return False
    if objects is not None and re.match(r"^что\s+у\s+", normalized):
        tokens, companies, _ = _project_query(text, objects)
        if tokens and (companies or project_candidates(text, objects)):
            return True
    return bool(
        re.search(r"\bчто\s+(?:у\s+нас\s+)?(?:сейчас\s+)?по\s+", normalized)
        or re.match(r"^что\s+у\s+нас\b", normalized)
        or re.search(r"\b(?:сводк\w*|состояни\w*|ситуаци\w*)\s+по\s+(?!всем\b|фирм\w*|компан\w*)", normalized)
        or re.search(r"\b(?:сводк\w*|состояни\w*|час\w*|расход\w*|заработал\w*)\b.*\b(?:объект|проект)\w*\b", normalized)
        or re.search(r"\bкто\b.*\b(?:объект|проект)\w*\b", normalized)
        or re.search(r"\bсколько\s+заработал\w*\b", normalized)
        or re.match(r"^теперь\s+по\s+(?!всем\b).+", normalized)
    )


def project_overview_scope(text, objects, *, allow_company_only=False):
    normalized = " ".join(text.casefold().split())
    if re.search(r"\b(?:сотрудник\w*|час(?:ы|ов|а))\b", normalized):
        return None
    explicit_all = bool(re.search(
        r"\b(?:все\w*|кажд\w*)\s+объект\w*\b|"
        r"\bобъект\w*\s+(?:все\w*|кажд\w*)\b",
        normalized,
    ))
    overview_query = bool(
        re.search(r"\b(?:обзор\w*|сводк\w*|ситуаци\w*)\s+(?:по\s+)?объект\w*", normalized)
        or re.search(r"\bчто\s+(?:у\s+нас\s+)?по\s+объект\w*", normalized)
        or re.search(r"\bпо\s+всем\s+объект\w*", normalized)
    )
    company_only = allow_company_only and bool(re.match(r"^только\s+", normalized))
    if not explicit_all and not overview_query and not company_only:
        return None

    tokens, companies, _ = _project_query(text, objects)
    if len(companies or ()) > 1:
        raise ValueError("Укажите одну фирму для обзора объектов.")
    if not explicit_all and tokens:
        return None
    if not explicit_all and not overview_query and not company_only:
        return None
    return {"company": companies[0] if companies else "", "all_objects": True}


def project_period(text, today=None):
    today = today or datetime.now(ZoneInfo("Europe/Stockholm")).date()
    explicit = explicit_date_period(text, today=today) or _week_period(text)
    if explicit:
        return explicit
    normalized = text.casefold()
    if re.search(r"\bза\s+весь\s+(?:объект|проект)\b", normalized):
        return None
    if re.search(r"\b(?:за|с|недел\w*|месяц\w*|год\w*)\b", normalized):
        raise ValueError("Не удалось определить период. Укажите даты или месяц.")
    return None


def project_overview_period(text, today=None, fallback=None):
    today = today or datetime.now(ZoneInfo("Europe/Stockholm")).date()
    explicit = explicit_date_period(text, today=today)
    if explicit:
        return explicit
    week = re.search(r"\b(текущ\w*|эт\w*|прошл\w*|предыдущ\w*)\s+недел\w*\b", text.casefold())
    if week:
        start = today - timedelta(days=today.weekday())
        if week.group(1).startswith(("прошл", "предыдущ")):
            start -= timedelta(days=7)
        return start.isoformat(), (start + timedelta(days=6)).isoformat()
    if re.search(r"\b(?:за|с|недел\w*|месяц\w*|год\w*)\b", text.casefold()):
        raise ValueError("Не удалось определить период. Укажите даты, месяц или неделю.")
    if fallback:
        start, end = (date.fromisoformat(value) for value in fallback)
        if start > end:
            raise ValueError("Начало периода должно быть не позже окончания.")
        return start.isoformat(), end.isoformat()
    start = today.replace(day=1)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start.isoformat(), (next_month - timedelta(days=1)).isoformat()


def project_marking(text):
    match = re.search(r"(?<!\w)(?:p|пэ|п)\s*[-–]?\s*(\d+(?:[-–]\d+)*)(?!\w)", text.casefold())
    return "p" + re.sub(r"[-–]", "", match.group(1)) if match else None


def _tokens_match(query, candidate, approximate=True):
    if not query:
        return False
    if all(any(
        word == token or (approximate and min(len(word), len(token)) >= 4
                          and SequenceMatcher(None, word, token).ratio() >= 0.78)
        or (approximate and min(len(word), len(token)) >= 3
            and re.sub(r"[aeouy]", "a", word) == re.sub(r"[aeouy]", "a", token))
        for token in candidate) for word in query):
        return True
    if not approximate:
        return False
    joined = "".join(query)
    # Speech can split names and attach the preposition "по" to their first syllable.
    spoken = (joined, joined[2:]) if joined.startswith("po") else (joined,)
    return any(
        min(len(value), len(name)) >= 6 and SequenceMatcher(None, value, name).ratio() >= 0.82
        for start in range(len(candidate))
        for end in range(start + 1, len(candidate) + 1)
        for name in ("".join(candidate[start:end]),)
        for value in spoken
    )


def _project_query(text, objects):
    normalized = " ".join(text.casefold().split())
    normalized = re.split(r"\b(?:за|с)\s+", normalized, maxsplit=1)[0]
    normalized = re.sub(
        r"\b(?:что|у\s+нас|сейчас|хочу|дай|мне|пожалуйста|покажи|сводк\w*|состояни\w*|ситуаци\w*|сколько|"
        r"час\w*|расход\w*|заработал\w*|кто|там|работал\w*|теперь|по|"
        r"объект\w*|проект\w*|обзор\w*|этому|этот|этого|все\w*|кажд\w*|только|"
        r"фирм\w*|компан\w*|а|и)\b",
        " ", normalized)
    normalized = re.sub(r"^\s*у\s+", " ", normalized)
    company_phrase = re.search(r"\bу\s+(.+)$", normalized)
    company_query = _normalized_name_tokens(company_phrase.group(1)) if company_phrase else []
    name_tokens = _normalized_name_tokens(normalized[:company_phrase.start()]
                                         if company_phrase else normalized)
    companies = sorted({str(obj.get("company") or "") for obj in objects})
    matched = []
    exact_company = True
    for company in companies:
        tokens = _normalized_name_tokens(company)
        if company_query:
            found = _tokens_match(company_query, tokens)
            exact = _tokens_match(company_query, tokens, False)
        else:
            found = bool(tokens) and all(any(_tokens_match([word], [token])
                                            for word in name_tokens) for token in tokens)
            exact = all(token in name_tokens for token in tokens)
        if found:
            matched.append(company)
            exact_company = exact_company and exact
    if not company_phrase and matched:
        company_tokens = {token for company in matched for token in _normalized_name_tokens(company)}
        name_tokens = [word for word in name_tokens
                       if not any(_tokens_match([word], [token]) for token in company_tokens)]
    return name_tokens, matched if company_phrase or matched else None, exact_company


def project_has_name(text, objects):
    tokens, _companies, _exact = _project_query(text, objects)
    return bool(tokens or project_marking(text))


def project_exact_match(text, obj, objects):
    marking = project_marking(text)
    if marking:
        return marking == project_marking(str(obj.get("markning") or ""))
    tokens, _companies, exact_company = _project_query(text, objects)
    return exact_company and _tokens_match(tokens, _normalized_name_tokens(obj["name"]), False)


def project_candidates(text, objects):
    normalized = " ".join(text.casefold().split())
    identifiers = [obj for obj in objects if obj.get("sync_id") and re.search(
        r"(?<![\w-])" + re.escape(str(obj["sync_id"]).casefold()) + r"(?![\w-])", normalized)]
    if identifiers:
        return identifiers
    marking = project_marking(text)
    if marking:
        return [obj for obj in objects if project_marking(str(obj.get("markning") or "")) == marking]
    tokens, companies, _exact = _project_query(text, objects)
    return [obj for obj in objects if (companies is None or obj.get("company") in companies)
            and (not tokens or _tokens_match(tokens, _normalized_name_tokens(obj.get("name") or "")))]


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
