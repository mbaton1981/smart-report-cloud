"""Full object report PDF assembly and confirmed-expense projections."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from pathlib import Path
import json
import re
import sys
import tempfile

from PIL import Image, ImageSequence
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from pypdfium2 import PdfiumError
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Image as ReportImage, LongTable, PageBreak, Paragraph, SimpleDocTemplate,
    Spacer, Table, TableStyle,
)

from customer_report import CATEGORY_LABELS, REPORT_ISSUER_NAME, WORK_TYPES, format_quantity
from customer_billing import allocation_fingerprint, priced_report
from document_costs import verified_original_order_allocation_proposal
from expense_documents import (
    DocumentExtractionError, extract_local_document, object_marking_matches, parse_decimal,
    parse_invoice_pages,
)
from expense_categories import (
    CATEGORY_EQUIPMENT_RENTAL, CATEGORY_MATERIALS, CATEGORY_OTHER, CATEGORY_SUBCONTRACTOR_MIXED,
    CATEGORY_SUBCONTRACTOR_WORK, CATEGORY_UNCLASSIFIED, confirmed_expense_category,
)
from fixed_pricing import signature
from object_package_swedish import swedish_category, swedish_document_type, swedish_label


IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


def expense_document_key(document):
    return str(document.get("client_id") or document.get("id") or "")


def _sum_partial(values):
    parsed = [parse_decimal(value) for value in values]
    known = [value for value in parsed if value is not None]
    complete = bool(parsed) and len(known) == len(parsed)
    return {
        "value": sum(known, Decimal(0)) if complete else None,
        "known": sum(known, Decimal(0)) if known else None,
    }


def _amount_text(value, currency=""):
    amount = parse_decimal(value)
    if amount is None:
        return "Неизвестно"
    text = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} {currency}".strip()


def collect_object_expenses(documents, object_id, date_from=None, date_to=None):
    """Return one confirmed row per document and separate related, excluded records."""
    confirmed, excluded = [], []
    seen = set()
    documents_by_id = {
        str(document.get("id")): document for document in documents
        if document.get("id") is not None
    }
    for document in documents:
        if document.get("supporting_to_document_id"):
            continue
        key = expense_document_key(document)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        source_allocations = [
            dict(allocation) for allocation in document.get("allocations") or []
        ]
        supporting_documents = list(document.get("supporting_documents") or [])
        if not supporting_documents:
            supporting_documents = [
                documents_by_id[str(value)]
                for value in document.get("supporting_document_ids") or []
                if str(value) in documents_by_id
            ]
        object_allocation_indexes = [
            index for index, allocation in enumerate(source_allocations)
            if allocation.get("object_id") is not None
            and int(allocation["object_id"]) == int(object_id)
            and not allocation.get("company_expense")
            and allocation.get("destination") != "company"
        ]
        allocations = [source_allocations[index] for index in object_allocation_indexes]
        for allocation in allocations:
            category, source = confirmed_expense_category(
                allocation.get("expense_category"),
                allocation.get("expense_category_source"),
            )
            allocation["expense_category"] = category
            allocation["expense_category_source"] = source
        if not allocations:
            continue

        document_date = str(document.get("document_date") or "").strip()
        if document_date and (
            (date_from and document_date < date_from) or
            (date_to and document_date > date_to)
        ):
            continue

        sync_state = document.get("sync_state")
        status = str(document.get("verification_status") or "")
        is_confirmed = (
            status == "confirmed"
            and sync_state not in {"pending", "conflict", "unverified"}
            and not document.get("sync_error")
        )
        reason = None
        if not document_date and date_from and date_to:
            reason = "Дата неизвестна; невозможно проверить принадлежность выбранному периоду."
        elif sync_state == "conflict":
            reason = "Есть конфликт синхронизации; локальная версия не включена."
        elif sync_state in {"pending", "unverified"} or document.get("sync_error"):
            reason = "Изменение не подтверждено сервером; документ не включён."
        elif status != "confirmed":
            reason = "Документ требует проверки или ещё не подтверждён."
        elif not is_confirmed:
            reason = "Распределение не подтверждено."

        if reason:
            excluded.append({
                **document,
                "supporting_documents": supporting_documents,
                "object_allocations": allocations,
                "object_allocation_indexes": object_allocation_indexes,
                "excluded_reason": reason,
            })
            continue

        parts = {
            field: _sum_partial(allocation.get(field) for allocation in allocations)
            for field in ("net_amount", "vat_amount", "gross_amount", "rounding_amount")
        }
        amounts = {field: part["value"] for field, part in parts.items()}
        amounts["known_amounts"] = {
            field: part["known"] for field, part in parts.items()
        }
        confirmed.append({
            **document,
            "supporting_documents": supporting_documents,
            "object_allocations": allocations,
            "source_allocations": source_allocations,
            "object_allocation_indexes": object_allocation_indexes,
            "share": amounts,
            "date_unknown": not bool(document_date),
        })
    return confirmed, excluded


def expense_totals_by_currency(documents):
    totals = defaultdict(list)
    for document in documents:
        currency = str(document.get("currency_code") or "UNKNOWN").upper()
        totals[currency].append(document["share"])
    result = {}
    for currency, shares in sorted(totals.items()):
        parts = {
            field: _sum_partial(share.get(field) for share in shares)
            for field in ("net_amount", "vat_amount", "gross_amount")
        }
        known_amounts = {}
        for field in parts:
            known_values = [
                share.get("known_amounts", {}).get(field, share.get(field))
                for share in shares
            ]
            known_values = [parse_decimal(value) for value in known_values]
            known_values = [value for value in known_values if value is not None]
            known_amounts[field] = (
                sum(known_values, Decimal(0)) if known_values else None
            )
        result[currency] = {
            **{field: part["value"] for field, part in parts.items()},
            "known_amounts": known_amounts,
        }
    return result


def _format_hours(value):
    return "Неизвестно" if value is None else format_quantity(value) + " ч"


def _format_trip_count(value):
    if value is None:
        return "Неизвестно"
    text = format_quantity(value)
    return text[:-2] if text.endswith(",0") else text


def _share_amount_text(share, field, currency=""):
    value = share.get(field)
    if value is not None:
        return _amount_text(value, currency)
    known = share.get("known_amounts", {}).get(field)
    if known is not None:
        return f"{_amount_text(known, currency)} (известная часть; неполно)"
    return "Неизвестно"


def _total_amount_text(totals, field, currency):
    value = totals.get(field)
    if value is not None:
        return _amount_text(value, currency)
    known = totals.get("known_amounts", {}).get(field)
    if known is not None:
        return f"{_amount_text(known, currency)} (известная часть; итог неполон)"
    return "Неизвестно"


def _status_text(row):
    if row.get("invoiced"):
        return "Отмечено «в счёте»"
    if row.get("hours_confirmed"):
        return "Часы подтверждены"
    return "В работе"


def _time_facts(row):
    facts = row.get("time_facts")
    if isinstance(facts, str):
        import json
        try:
            facts = json.loads(facts)
        except (TypeError, ValueError):
            facts = {}
    facts = facts if isinstance(facts, dict) else {}
    start = facts.get("start") or row.get("start_time")
    end = facts.get("end") or row.get("end_time")
    if not start or not end:
        return ""
    next_day = facts.get("next_day")
    if next_day is None:
        next_day = end <= start
    return f"{start}–{end}" + (" (+1 день)" if next_day else "")


def _row_ob_details(row, segments):
    if row.get("entry_type") != "ob":
        time_range = _time_facts(row)
        return "Обычная работа" + (f" · {time_range}" if time_range else "")
    if not segments:
        return "OB; детализация не подтверждена"
    parts = []
    for segment in segments:
        label = CATEGORY_LABELS.get(segment.get("category"), "OB")
        start = str(segment.get("start_time") or "")[-5:]
        end = str(segment.get("end_time") or "")[-5:]
        interval = f"{start}–{end}: " if start and end else ""
        parts.append(f"{interval}{format_quantity(segment.get('hours') or 0)} ч · {label}")
    return "\n".join(parts)


def _paragraph(value, style):
    from xml.sax.saxutils import escape
    return Paragraph(escape(str(value)).replace("\n", "<br/>"), style)


def _table(rows, widths, font, bold_font, *, repeat_rows=1, alignments=(), font_size=10.5):
    body = ParagraphStyle(
        "ObjectPackageBody", fontName=font, fontSize=font_size, leading=font_size + 2,
    )
    header = ParagraphStyle(
        "ObjectPackageHeader", fontName=bold_font, fontSize=font_size, leading=font_size + 2,
        textColor=colors.HexColor("#152a38"),
    )
    cells = [
        [_paragraph(value, header if row_index < repeat_rows else body) for value in row]
        for row_index, row in enumerate(rows)
    ]
    result = LongTable(
        cells, colWidths=widths, repeatRows=repeat_rows, splitByRow=1, splitInRow=1,
        hAlign="LEFT",
    )
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, repeat_rows - 1), colors.HexColor("#dbe7ef")),
        ("ROWBACKGROUNDS", (0, repeat_rows), (-1, -1),
         [colors.white, colors.HexColor("#f4f6f8")]),
        ("LINEBELOW", (0, repeat_rows - 1), (-1, repeat_rows - 1),
         0.8, colors.HexColor("#28475b")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for column, alignment in alignments:
        style.append(("ALIGN", (column, repeat_rows), (column, -1), alignment))
    result.setStyle(TableStyle(style))
    return result


def _money_parts(values):
    parsed = [parse_decimal(value) for value in values]
    known = [value for value in parsed if value is not None]
    return {
        "complete": bool(parsed) and len(known) == len(parsed),
        "known": sum(known, Decimal(0)) if known else None,
    }


def _customer_summary(report):
    totals = report.get("totals", {})
    lines = report.get("cost_lines", [])
    work_lines = [
        line for line in lines
        if str(line.get("label") or "").startswith(
            ("Arbete", "Fast pris", "OB", "Kundtillägg")
        )
    ]
    travel_lines = [
        line for line in lines if str(line.get("label") or "").startswith("Resa")
    ]
    work = _money_parts(line.get("amount") for line in work_lines)
    if work_lines:
        work_amount = work["known"] if work["complete"] else None
        known_work_amount = work["known"]
        work_unknown_rows = sum(
            parse_decimal(line.get("amount")) is None for line in work_lines
        )
    else:
        work_values = [
            parse_decimal(totals.get(key))
            for key in ("work_amount", "ob_amount")
        ]
        known_work_values = [value for value in work_values if value is not None]
        known_work_amount = (
            sum(known_work_values, Decimal(0)) if known_work_values else None
        )
        work_unknown_rows = sum(value is None for value in work_values)
        work_complete = (
            "work_amount" in totals and "ob_amount" in totals
            and all(value is not None for value in work_values)
        )
        work_amount = known_work_amount if work_complete else None
        work["complete"] = work_complete
        work["known"] = known_work_amount

    travel = _money_parts(line.get("amount") for line in travel_lines)
    if travel_lines:
        travel_amount = travel["known"] if travel["complete"] else None
        travel_unknown_rows = sum(
            parse_decimal(line.get("amount")) is None for line in travel_lines
        )
    else:
        travel_amount = parse_decimal(totals.get("transport_amount"))
        travel_unknown_rows = 0 if travel_amount is not None else 1
        if travel_amount is not None:
            travel["known"] = travel_amount
            travel["complete"] = True
    combined = parse_decimal(totals.get("invoice_total"))
    known_combined = parse_decimal(totals.get("known_subtotal"))
    trips = []
    for row in report.get("journal_rows", []):
        if row.get("entry_type") not in WORK_TYPES:
            continue
        count = parse_decimal(row.get("transport"))
        trips.append(count)
    known_trips = sum((value for value in trips if value is not None), Decimal(0))
    if (
        travel_amount is None and not travel_lines
        and trips and all(value is not None for value in trips) and known_trips == 0
    ):
        travel_amount = Decimal(0)
        travel["known"] = Decimal(0)
        travel["complete"] = True
        travel_unknown_rows = 0
    rates = defaultdict(lambda: {
        "quantity": Decimal(0), "amounts": [], "quantity_complete": True,
    })
    for line in travel_lines:
        rate = parse_decimal(line.get("rate"))
        quantity = parse_decimal(line.get("quantity"))
        values = rates[rate]
        if quantity is None:
            values["quantity_complete"] = False
        else:
            values["quantity"] += quantity
        values["amounts"].append(parse_decimal(line.get("amount")))
    trip_breakdown = []
    for rate, values in sorted(rates.items(), key=lambda item: (item[0] is None, item[0] or 0)):
        amount = _money_parts(values["amounts"])
        trip_breakdown.append({
            "rate": rate,
            "quantity": values["quantity"] if values["quantity_complete"] else None,
            "known_quantity": values["quantity"],
            "amount": amount["known"] if amount["complete"] else None,
        })
    work_hours = _money_parts(
        parse_decimal(row.get("hours"))
        for row in report.get("journal_rows", [])
        if row.get("entry_type") in WORK_TYPES
    )
    return {
        "hours": work_hours["known"] if work_hours["complete"] else None,
        "known_hours": work_hours["known"],
        "trips": known_trips if all(value is not None for value in trips) else None,
        "known_trips": known_trips,
        "unknown_trip_rows": sum(value is None for value in trips),
        "work_amount": work_amount,
        "known_work_amount": known_work_amount,
        "work_complete": work["complete"] and work_amount is not None,
        "unknown_work_rows": work_unknown_rows,
        "travel_amount": travel_amount,
        "known_travel_amount": travel["known"],
        "travel_complete": travel["complete"] and travel_amount is not None,
        "unknown_travel_rows": travel_unknown_rows,
        "combined_amount": combined,
        "known_combined_amount": known_combined,
        "trip_breakdown": trip_breakdown,
    }


_BILLING_COLUMNS = ("billed", "pending", "review")
_BILLING_COMPONENTS = ("work", "travel", "materials", "subcontractor", "other")
_BILLING_LABELS = {
    "billed": ("Выставлено", "Fakturerat"),
    "pending": ("Ожидает выставления", "Kvar att fakturera"),
    "review": ("Требует сверки", "Att kontrollera"),
}


def _billing_breakdown(data, figures, work_rows):
    """Allocate reported net amounts according to the current saved invoice membership."""
    members = data["billing_members"]
    expenses = data.get("expenses", [])
    values = defaultdict(list)
    if not data.get("expense_snapshot_available"):
        values["materials", "review"].append(None)
        values["other", "review"].append(None)
    statuses = {}
    foreign = set()
    saved_expenses = defaultdict(list)
    for member in (members or {}).values():
        if member["kind"] == "expense":
            saved_expenses[member["document_id"]].append(member)

    for document in expenses:
        doc_id = document.get("id")
        original = document.get("allocations") or []
        saved = saved_expenses.get(doc_id, [])
        changed = any(
            not 0 <= item["index"] < len(original)
            or item["fingerprint"] != allocation_fingerprint(document, item["index"])
            for item in saved
        )
        billed = {item["index"] for item in saved}
        currency = str(document.get("currency_code") or "UNKNOWN").upper()
        for index, part in zip(document["object_allocation_indexes"], document["object_allocations"]):
            status = ("review" if members is None or changed or doc_id is None else
                      "billed" if index in billed else "pending")
            statuses[doc_id, index] = status
            component = {CATEGORY_MATERIALS: "materials", CATEGORY_SUBCONTRACTOR_WORK: "subcontractor"}.get(
                part.get("expense_category"), "other",
            )
            if currency == "SEK":
                values[component, status].append(parse_decimal(part.get("net_amount")))
            else:
                foreign.add(currency)
                if currency == "UNKNOWN":
                    values[component, "review"].append(None)

    if data.get("include_customer_cost") and work_rows:
        groups = defaultdict(list)
        for row in work_rows:
            member = (members or {}).get("shift:" + str(row.get("sync_id")))
            if members is None or not row.get("sync_id") or (member and (
                    member["signature"] != signature(row)
                    or json.loads(member.get("customer_pricing") or "null") != json.loads(row.get("customer_pricing") or "null")
                    or not row.get("invoiced"))):
                status = "review"
            else:
                status = "billed" if row.get("invoiced") else "pending"
            groups[status].append(row)

        def work_values(group_figures, rows):
            travel_complete = group_figures["travel_complete"] and all(row.get("transport") is not None for row in rows)
            return {
                "work": (group_figures["known_work_amount"], group_figures["work_complete"]),
                "travel": (group_figures["known_travel_amount"], travel_complete),
            }

        report = data["customer_report"]
        whole = work_values(figures, work_rows)
        if len(groups) == 1:
            projections = {next(iter(groups)): whole}
        elif report.get("is_fast_pris"):
            projections = {"review": whole}
        else:
            projections = {
                status: work_values(_customer_summary(priced_report(
                    {"journal_segments": report.get("journal_segments", {}), "is_fast_pris": False}, rows,
                )), rows)
                for status, rows in groups.items()
            }
        # OB intervals can overlap between statuses. Keep the reconciled report amount in review.
        cents = Decimal("0.01")
        for component, (total, total_complete) in whole.items():
            by_status = {status: parts[component] for status, parts in projections.items()}
            allocated = sum((amount or Decimal(0) for amount, _ in by_status.values()), Decimal(0))
            if ((total or Decimal(0)).quantize(cents, rounding=ROUND_HALF_UP)
                    != allocated.quantize(cents, rounding=ROUND_HALF_UP)
                    or total_complete != all(complete for _, complete in by_status.values())):
                by_status = {"review": (total, total_complete)}
            for status, (amount, complete) in by_status.items():
                if amount is not None:
                    values[component, status].append(amount)
                if not complete:
                    values[component, status].append(None)

    return values, statuses, bool(foreign)


def _object_total_summary(data, figures):
    expenses = data.get("expenses", [])
    excluded = data.get("excluded_expenses", [])
    snapshot_available = bool(data.get("expense_snapshot_available"))
    expense_net = defaultdict(Decimal)
    category_net = defaultdict(lambda: {
        "known": Decimal(0), "unknown_count": 0, "allocation_count": 0,
        "document_ids": set(),
    })
    supplier_net = {}
    unknown_net_by_currency = defaultdict(int)
    foreign_share_count = 0
    unknown_currency_share_count = 0
    unknown_net_share_count = 0

    for document in expenses:
        currency = str(document.get("currency_code") or "").strip().upper() or "UNKNOWN"
        supplier = " ".join(str(document.get("supplier_name") or "").split())
        supplier_total = supplier_net.setdefault((currency, supplier.casefold()), {
            "supplier": supplier, "currency": currency, "known": None,
            "unknown_count": 0, "document_count": 0,
        })
        supplier_total["document_count"] += 1
        allocations = document.get("object_allocations") or []
        if not allocations:
            allocations = [document.get("share") or {}]
        for allocation in allocations:
            category, _source = confirmed_expense_category(
                allocation.get("expense_category"),
                allocation.get("expense_category_source"),
            )
            category_total = category_net[(currency, category)]
            category_total["allocation_count"] += 1
            category_total["document_ids"].add(
                expense_document_key(document) or str(document.get("id"))
            )
            net = parse_decimal(allocation.get("net_amount"))
            if net is None:
                unknown_net_share_count += 1
                unknown_net_by_currency[currency] += 1
                category_total["unknown_count"] += 1
                supplier_total["unknown_count"] += 1
            else:
                expense_net[currency] += net
                category_total["known"] += net
                if currency != "UNKNOWN":
                    previous_net = supplier_total["known"]
                    supplier_total["known"] = net if previous_net is None else previous_net + net
            if currency == "UNKNOWN":
                unknown_currency_share_count += 1
            elif currency != "SEK":
                foreign_share_count += 1

    unconfirmed_share_count = sum(
        max(1, len(document.get("object_allocations") or []))
        for document in excluded
    )
    if "SEK" in expense_net or any(
        str(document.get("currency_code") or "UNKNOWN").strip().upper() == "SEK"
        for document in expenses
    ):
        known_sek_expense = expense_net["SEK"]
    elif snapshot_available:
        known_sek_expense = Decimal(0)
    else:
        known_sek_expense = None

    known_components = [
        value for value in (
            figures.get("known_work_amount"),
            figures.get("known_travel_amount"),
            known_sek_expense,
        ) if value is not None
    ]
    known_total = (
        sum(known_components, Decimal(0)) if known_components else None
    )
    complete = (
        figures.get("work_complete")
        and figures.get("travel_complete")
        and snapshot_available
        and not unknown_net_share_count
        and not foreign_share_count
        and not unknown_currency_share_count
        and not unconfirmed_share_count
    )

    swedish = data.get("language") == "sv"
    reasons = []
    recovery_statuses = {
        document.get("net_recovery_status") for document in expenses
    }
    if not figures.get("work_complete"):
        count = figures.get("unknown_work_rows") or 0
        reasons.append(
            f"Okänd summa arbete/OB för {count} rader"
            if swedish and count else
            "Arbetssumman är ofullständig"
            if swedish else
            f"Неизвестна сумма работы/OB в строках: {count}"
            if count else "Сумма работы/OB неизвестна"
        )
    if not figures.get("travel_complete"):
        count = figures.get("unknown_travel_rows") or 0
        reasons.append(
            f"Okänd resesumma för {count} rader"
            if swedish and count else
            "Resesumman är ofullständig"
            if swedish else
            f"Неизвестна сумма поездок в строках: {count}"
            if count else "Сумма поездок неизвестна"
        )
    if not snapshot_available:
        reasons.append(
            "Kostnadsregistret är inte tillgängligt"
            if swedish else "Реестр расходов недоступен"
        )
    if unknown_net_share_count:
        reasons.append(
            f"netto saknas; antal andelar: {unknown_net_share_count}"
            if swedish else
            f"нетто неизвестно; количество долей: {unknown_net_share_count}"
        )
    recovery_reason = None
    if "original_not_verified" in recovery_statuses:
        recovery_reason = (
            "originalet kunde inte verifieras"
            if swedish else "оригинал не прошёл сверку"
        )
    elif recovery_statuses.intersection({"no_unique_match", "partially_recovered"}):
        recovery_reason = (
            "ingen entydig matchning mot originalordern"
            if swedish else "нет однозначного сопоставления с заказом оригинала"
        )
    elif "original_unavailable" in recovery_statuses:
        recovery_reason = (
            "originalet saknas eller är inte tillgängligt"
            if swedish else "оригинал отсутствует или недоступен"
        )
    if unknown_net_share_count and recovery_reason:
        reasons.append(recovery_reason)
    if unconfirmed_share_count:
        reasons.append(
            f"obekräftade andelar: {unconfirmed_share_count}"
            if swedish else
            f"неподтверждённые доли: {unconfirmed_share_count}"
        )
    if foreign_share_count:
        reasons.append(
            f"andelar i annan valuta, ej konverterade: {foreign_share_count}"
            if swedish else
            f"доли в другой валюте не конвертированы: {foreign_share_count}"
        )
    if unknown_currency_share_count:
        reasons.append(
            f"valuta saknas för andelar: {unknown_currency_share_count}"
            if swedish else
            f"для долей не указана валюта: {unknown_currency_share_count}"
        )

    return {
        "known_total": known_total,
        "complete": complete,
        "expense_net_by_currency": dict(expense_net),
        "supplier_net": sorted(supplier_net.values(), key=lambda row: (
            row["currency"] != "SEK", row["currency"] == "UNKNOWN", row["currency"],
            row["known"] is None, -(row["known"] or Decimal(0)), row["supplier"].casefold(),
        )),
        "category_net_by_currency": {
            currency: {
                category: {
                    "known": values["known"],
                    "unknown_count": values["unknown_count"],
                    "allocation_count": values["allocation_count"],
                    "document_count": len(values["document_ids"]),
                }
                for (entry_currency, category), values in category_net.items()
                if entry_currency == currency
            }
            for currency in {key[0] for key in category_net}
        },
        "unknown_net_by_currency": dict(unknown_net_by_currency),
        "known_sek_expense": known_sek_expense,
        "unknown_net_share_count": unknown_net_share_count,
        "unknown_net_reason": recovery_reason,
        "foreign_share_count": foreign_share_count,
        "unknown_currency_share_count": unknown_currency_share_count,
        "unconfirmed_share_count": unconfirmed_share_count,
        "reasons": reasons,
    }


def _expense_order_evidence(source_paths, marking):
    target = re.sub(r"[^A-Z0-9]", "", str(marking or "").upper())
    if not target:
        return []
    order_pattern = re.compile(r"Sälj\.?\s*ordnr\.?\s*:?\s*([A-Z0-9-]+)", re.IGNORECASE)
    project_pattern = re.compile(
        r"(?:Proj\.?\s*nr\.?|Märke\s*/\s*Ordnr\.?)\s*:?\s*([A-Z0-9-]+)",
        re.IGNORECASE,
    )
    evidence = {}
    for attachment_index, source_path in enumerate(source_paths, 1):
        if Path(source_path).suffix.casefold() != ".pdf":
            continue
        reader = PdfReader(str(source_path), strict=True)
        active_order = ""
        active_match = False
        for page_number, page in enumerate(reader.pages, 1):
            for line in (page.extract_text() or "").splitlines():
                order = order_pattern.search(line)
                if order:
                    active_order = order.group(1).strip()
                    active_match = False
                project = project_pattern.search(line)
                if project:
                    active_match = (
                        re.sub(r"[^A-Z0-9]", "", project.group(1).upper()) == target
                    )
                    if active_order and active_match:
                        entry = evidence.setdefault(
                            (attachment_index, active_order),
                            {"attachment_index": attachment_index, "order": active_order, "pages": []},
                        )
                        if page_number not in entry["pages"]:
                            entry["pages"].append(page_number)
    return list(evidence.values())


def _expense_description(document, marking):
    category = str(document.get("expense_category") or "").strip()
    comment = str(document.get("document_comment") or "").strip()
    evidence = document.get("object_order_evidence") or []
    orders = ", ".join(dict.fromkeys(item["order"] for item in evidence))
    order_description = (
        f"enligt order {orders} ({marking})" if orders else ""
    )
    if comment:
        description = swedish_category(comment) if category.casefold() == "материалы" else comment
        if re.search(r"[\u0400-\u04ff]", description):
            if category and order_description:
                return f"{swedish_category(category)} {order_description}."
            return "Objektets fördelade kostnad; beskrivningen finns i originalet."
        return description
    if category:
        category = swedish_category(category)
        if category.casefold() in {"материалы", "material", "materials"} and not order_description:
            return "Objektets fördelade kostnad; artikelspecifikation saknas."
        return f"{category} {order_description}." if order_description else category
    if order_description:
        return f"Inköp {order_description}."
    return "Objektets fördelade kostnad; artikelspecifikation saknas."


def _expense_tax_notes(expenses, language):
    totals = expense_totals_by_currency(expenses)
    notes = []
    for currency, fields in totals.items():
        net = fields["net_amount"]
        vat = fields["vat_amount"]
        if net is not None and vat is not None:
            continue
        unknown_parts = [
            document for document in expenses
            if str(document.get("currency_code") or "UNKNOWN").upper() == currency
            and (document["share"].get("net_amount") is None
                 or document["share"].get("vat_amount") is None)
        ]
        unknown_gross = _money_parts(
            document["share"].get("gross_amount") for document in unknown_parts
        )
        if unknown_gross["known"] is None:
            amount = "Okänt" if language == "sv" else "Неизвестно"
        elif unknown_gross["complete"]:
            amount = _amount_text(unknown_gross["known"], currency)
        else:
            amount = _amount_text(unknown_gross["known"], currency) + (
                " (känd del; total okänd)" if language == "sv"
                else " (известная часть; итог неизвестен)"
            )
        if language == "sv":
            known_net_value = fields.get("net_amount")
            known_net = (
                _amount_text(known_net_value, currency)
                if known_net_value is not None else
                f"{_amount_text(fields['known_amounts']['net_amount'], currency)} "
                "(känd del; totalsumman är ofullständig)"
                if fields["known_amounts"]["net_amount"] is not None else "Okänt"
            )
            notes.append(
                f"Skattefördelningen är ofullständig: netto och moms saknas för "
                f"{amount}. Känd nettodel: {known_net}; detta är inte en fullständig nettosumma."
            )
        else:
            known_net = _total_amount_text(fields, "net_amount", currency)
            notes.append(
                f"Налоговое распределение неполное: нетто и НДС неизвестны для "
                f"{amount}. Известная часть нетто: {known_net}; это не полный итог нетто."
            )
    return notes


def _brand_logo():
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    path = root / "assets" / "bygger-wordmark.png"
    if not path.is_file():
        path = Path(__file__).resolve().parent / "assets" / "bygger-wordmark.png"
    if not path.is_file():
        raise FileNotFoundError(f"Фирменный логотип не найден: {path}")
    return path


def _main_pdf_bytes(data, font, bold_font, appendix_pages):
    swedish = data.get("language") == "sv"
    raw_paragraph = _paragraph
    raw_table = _table

    def paragraph(value, style):
        return raw_paragraph(swedish_label(value) if swedish else value, style)

    class Literal(str):
        pass

    def table(rows, *args, **kwargs):
        if swedish:
            rows = [[cell if isinstance(cell, Literal) else swedish_label(cell)
                     for cell in row] for row in rows]
        return raw_table(rows, *args, **kwargs)

    width, height = landscape(A4)
    available = width - 72
    styles = {
        "title": ParagraphStyle("ObjectPackageTitle", fontName=bold_font, fontSize=18, leading=22),
        "issuer": ParagraphStyle("ObjectPackageIssuer", fontName=bold_font, fontSize=12, leading=15),
        "heading": ParagraphStyle("ObjectPackageSection", fontName=bold_font, fontSize=12, leading=15,
                                  textColor=colors.HexColor("#152a38"), spaceBefore=8, spaceAfter=5,
                                  keepWithNext=True),
        "body": ParagraphStyle("ObjectPackageBodyText", fontName=font, fontSize=9, leading=12),
        "notice": ParagraphStyle("ObjectPackageNotice", fontName=font, fontSize=8.5, leading=11),
        "bold": ParagraphStyle("ObjectPackageBold", fontName=bold_font, fontSize=11, leading=14),
    }
    report = data.get("customer_report") or {}
    work_rows = [
        row for row in report.get("journal_rows", [])
        if row.get("entry_type") in WORK_TYPES
    ]
    figures = _customer_summary(report)
    object_row = data["object"]
    object_title = str(object_row.get("name") or "Объект")
    marking = str(object_row.get("markning") or "")
    object_display = object_title + (f" · {marking}" if marking else "")
    expenses = data.get("expenses", [])
    billing_statuses = {}
    billing_values = None
    if "billing_members" in data:
        billing_values, billing_statuses, foreign_billing = _billing_breakdown(data, figures, work_rows)

    styles.update({
        "cover_object": ParagraphStyle(
            "ObjectPackageCoverObject", fontName=bold_font, fontSize=24, leading=29,
            textColor=colors.HexColor("#152a38"), spaceBefore=5, spaceAfter=2,
        ),
        "cover_meta": ParagraphStyle(
            "ObjectPackageCoverMeta", fontName=font, fontSize=11, leading=15,
            textColor=colors.HexColor("#405766"),
        ),
        "card_label": ParagraphStyle(
            "ObjectPackageCardLabel", fontName=bold_font, fontSize=11, leading=14,
            textColor=colors.HexColor("#28475b"),
        ),
        "card_value": ParagraphStyle(
            "ObjectPackageCardValue", fontName=bold_font, fontSize=19, leading=23,
            textColor=colors.HexColor("#152a38"),
        ),
        "card_note": ParagraphStyle(
            "ObjectPackageCardNote", fontName=font, fontSize=9.5, leading=12,
            textColor=colors.HexColor("#405766"),
        ),
        "section_title": ParagraphStyle(
            "ObjectPackageSectionTitle", fontName=bold_font, fontSize=16, leading=20,
            textColor=colors.HexColor("#152a38"), spaceAfter=6, keepWithNext=True,
        ),
        "detail_body": ParagraphStyle(
            "ObjectPackageDetailBody", fontName=font, fontSize=10.5, leading=13,
        ),
        "detail_note": ParagraphStyle(
            "ObjectPackageDetailNote", fontName=font, fontSize=10.5, leading=14,
        ),
    })

    def amount_or_partial(value, known, currency="SEK"):
        if value is not None:
            return _amount_text(value, currency)
        if known is not None:
            suffix = " · delbelopp, total ofullständig" if swedish else " · известная часть, итог неполон"
            return _amount_text(known, currency) + suffix
        return "Okänt" if swedish else "Неизвестно"

    def display_amount(value, currency="SEK"):
        if parse_decimal(value) is None and swedish:
            return "Okänt"
        return _amount_text(value, currency)

    def display_total(totals_by_field, field, currency):
        value = totals_by_field.get(field)
        if value is not None:
            return display_amount(value, currency)
        known = totals_by_field.get("known_amounts", {}).get(field)
        if known is not None:
            suffix = " (känd del; totalsumman är ofullständig)" if swedish else " (известная часть; итог неполон)"
            return display_amount(known, currency) + suffix
        return "Okänt" if swedish else "Неизвестно"

    def display_trip_count(value):
        if value is None:
            return "Okänt" if swedish else "Неизвестно"
        return _format_trip_count(value)

    def card(label, value, note, background, cell_width=None):
        body = [
            [raw_paragraph(label, styles["card_label"])],
            [raw_paragraph(value, styles["card_value"])],
            [raw_paragraph(note, styles["card_note"])],
        ]
        result = Table(
            body,
            colWidths=[cell_width or available * .47 - 26],
            hAlign="LEFT",
        )
        result.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), background),
            ("BOX", (0, 0), (-1, -1), .8, colors.HexColor("#c5d3dc")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        return result

    regular_logo = ReportImage(str(_brand_logo()))
    logo_scale = min(170 / regular_logo.imageWidth, 42 / regular_logo.imageHeight)
    regular_logo.drawWidth = regular_logo.imageWidth * logo_scale
    regular_logo.drawHeight = regular_logo.imageHeight * logo_scale
    issuer_title = ParagraphStyle(
        "ObjectPackageIssuerTitle", fontName=bold_font, fontSize=12, leading=15,
        alignment=2, textColor=colors.HexColor("#152a38"),
    )
    report_title = ParagraphStyle(
        "ObjectPackageReportTitle", fontName=font, fontSize=10.5, leading=14,
        alignment=2, textColor=colors.HexColor("#405766"),
    )
    issuer_details = Table(
        [
            [raw_paragraph(REPORT_ISSUER_NAME, issuer_title)],
            [raw_paragraph(
                "Fullständig objektrapport" if swedish else "Полный отчёт по объекту",
                report_title,
            )],
        ],
        colWidths=[available * .50],
    )
    issuer_details.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    header = Table(
        [[regular_logo, issuer_details]],
        colWidths=[available * .50, available * .50],
    )
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story = [header]
    story.append(raw_paragraph(object_display, styles["cover_object"]))
    customer_name = data.get("customer_name") if swedish else object_row.get("company")
    if customer_name and str(customer_name).casefold() != "privat":
        story.append(raw_paragraph(
            f"Beställare: {customer_name}" if swedish else f"Заказчик: {customer_name}",
            styles["cover_meta"],
        ))
    period = swedish_label(data.get("period_label") or "Нет данных") if swedish else (
        data.get("period_label") or "Нет данных"
    )
    story.append(raw_paragraph(
        f"{period} · {data['generated_at']}" if swedish else
        f"Период: {period} · Сформировано: {data['generated_at']}",
        styles["cover_meta"],
    ))
    if not object_row.get("is_active", 1):
        story.append(raw_paragraph(
            "Arkiverat objekt" if swedish else "Архивный объект",
            styles["cover_meta"],
        ))
    if swedish and data.get("draft"):
        story.append(raw_paragraph(
            "UTKAST · Se journalen för uppgifter som ännu inte har bekräftats.",
            styles["bold"],
        ))
    elif not swedish and data.get("draft"):
        story.append(raw_paragraph("ЧЕРНОВИК · Смотрите детали неподтверждённых данных.", styles["bold"]))

    work_value = amount_or_partial(
        figures["work_amount"], figures["known_work_amount"],
    )
    trip_value = amount_or_partial(
        figures["travel_amount"], figures["known_travel_amount"],
    )
    if figures["hours"] is not None:
        work_hours = format_quantity(figures["hours"])
        if swedish and work_hours.endswith(",0"):
            work_hours = work_hours[:-2]
        work_hours += " tim" if swedish else " ч"
    elif swedish and figures["known_hours"] is not None:
        work_hours = (
            f"{_format_trip_count(figures['known_hours'])} tim kända; totalt antal saknas"
        )
    else:
        work_hours = (
            "Okänt antal timmar" if swedish else
            _format_hours(figures["hours"])
        )
    work_note = (
        f"{work_hours} · exkl. moms enligt sparade kundvillkor"
        if swedish else f"{_format_hours(figures['hours'])} · без НДС по сохранённым условиям"
    )
    if not work_rows:
        work_note = "Inga arbetsposter i perioden." if swedish else "Нет рабочих записей за период."
    if figures["trips"] is not None:
        trip_total = display_trip_count(figures["trips"])
    elif swedish:
        trip_total = f"{display_trip_count(figures['known_trips'])} st + okänt antal"
    else:
        trip_total = f"{display_trip_count(figures['known_trips'])} поездок + число неизвестно"
    if figures["unknown_trip_rows"]:
        trip_note = (
            f"Totalt resor: {trip_total}. Summa resor: {trip_value} exkl. moms; "
            f"antal saknas för {figures['unknown_trip_rows']} arbetsposter."
            if swedish else
            f"Поездки: {trip_total}. Сумма поездок: {trip_value} без НДС; "
            f"число неизвестно для записей: {figures['unknown_trip_rows']}."
        )
    else:
        trip_note = (
            f"Totalt resor: {trip_total} st · Summa resor: {trip_value} exkl. moms."
            if swedish else f"Всего поездок: {trip_total}; сумма без НДС: {trip_value}."
        )
    if figures["trip_breakdown"]:
        breakdown = []
        for item in figures["trip_breakdown"]:
            rate = display_amount(item["rate"], "SEK") if item["rate"] is not None else (
                "okänt pris" if swedish else "цена неизвестна"
            )
            if item["quantity"] is not None:
                count = f"{display_trip_count(item['quantity'])} st"
            elif item["known_quantity"]:
                count = (
                    f"{display_trip_count(item['known_quantity'])} st + okänt antal"
                    if swedish else
                    f"{display_trip_count(item['known_quantity'])} поездок + число неизвестно"
                )
            else:
                count = "Okänt antal" if swedish else "Неизвестное число поездок"
            breakdown.append(f"{count} × {rate}")
        trip_note += (" · Priser: " if swedish else " · Ставки: ") + "; ".join(breakdown)
    trip_label = (
        "Resor · ofullständiga uppgifter · exkl. moms"
        if swedish and figures["unknown_trip_rows"] else
        "Resor · exkl. moms"
        if swedish else
        "Поездки · данные неполны · без НДС"
        if figures["unknown_trip_rows"] else
        "Поездки · без НДС"
    )

    total_summary = _object_total_summary(data, figures)
    category_specs = (
        (CATEGORY_EQUIPMENT_RENTAL, "Аренда оборудования · без НДС", "Hyra av maskiner och utrustning · exkl. moms", "#f3f5f4"),
        (CATEGORY_MATERIALS, "Материалы и закупки · без НДС", "Material och inköp · exkl. moms", "#f7f2e7"),
        (CATEGORY_SUBCONTRACTOR_WORK, "Работы подрядчиков · без НДС", "Underentreprenörers arbete · exkl. moms", "#edf3f8"),
        (CATEGORY_SUBCONTRACTOR_MIXED, "Работа и материалы подрядчика, без разбивки · без НДС",
         "Underentreprenör – arbete och material, ej specificerat · exkl. moms", "#f0eff7"),
        (CATEGORY_OTHER, "Прочие расходы · без НДС", "Övriga kostnader · exkl. moms", "#f3f5f4"),
        (CATEGORY_UNCLASSIFIED, "Не классифицировано · без НДС", "Ej kategoriserat · exkl. moms", "#fff3e7"),
    )
    category_cards = []
    category_values = total_summary["category_net_by_currency"]
    active_categories = {
        category
        for currency_values in category_values.values()
        for category, values in currency_values.items()
        if values["allocation_count"]
    }
    for category, russian_label, swedish_category_label, background in category_specs:
        category_sek = category_values.get("SEK", {}).get(category)
        if category not in active_categories:
            continue
        if category_sek is None:
            category_value = (
                "Ingår inte i SEK-totalen" if swedish
                else "Не включено в итог SEK"
            )
            unknown_count = 0
            document_count = sum(
                values[category]["document_count"]
                for values in category_values.values()
                if category in values
            )
        else:
            unknown_count = category_sek["unknown_count"]
            category_value = display_amount(
                None if unknown_count == category_sek["allocation_count"] else category_sek["known"], "SEK",
            )
            document_count = category_sek["document_count"]
        if category == CATEGORY_UNCLASSIFIED and len(active_categories) == 1:
            russian_label = "Расходы без разбивки / Ej kategoriserade kostnader"
            swedish_category_label = "Ej kategoriserade kostnader"
        category_notes = [
            "Belopp exkl. moms" if swedish else "Суммы без НДС",
            (
                f"{document_count} dokument" if swedish
                else f"{document_count} документ"
                if document_count % 10 == 1 and document_count % 100 != 11
                else f"{document_count} документа"
                if 2 <= document_count % 10 <= 4 and not 12 <= document_count % 100 <= 14
                else f"{document_count} документов"
            ),
        ]
        if unknown_count:
            category_notes.append(
                f"netto saknas för {unknown_count} andelar" if swedish
                else f"нетто неизвестно у {unknown_count} долей"
            )
        foreign_categories = [
            f"{currency} {display_amount(values[category]['known'], currency)}"
            for currency, values in category_values.items()
            if currency != "SEK" and category in values
            and values[category]["known"] != 0
        ]
        if foreign_categories:
            category_notes.append(
                ("Ingår inte i SEK-totalen: " if swedish else "Не включено в итог SEK: ")
                + ", ".join(foreign_categories)
            )
        category_cards.append(card(
            swedish_category_label if swedish else russian_label,
            category_value, ". ".join(category_notes) + ".",
            colors.HexColor(background), available / 4 - 18,
        ))
    if total_summary["unconfirmed_share_count"]:
        unconfirmed_note = (
            f"Obekräftade andelar ingår inte: {total_summary['unconfirmed_share_count']}."
            if swedish else
            f"Неподтверждённые доли не включены: {total_summary['unconfirmed_share_count']}."
        )
    else:
        unconfirmed_note = ""

    total_title = (
        "Totalt för objektet exkl. moms"
        if swedish and total_summary["complete"] else
        "Delsumma exkl. moms"
        if swedish else
        "Итого по объекту без НДС"
        if total_summary["complete"] else
        "Промежуточный итог без НДС"
    )
    total_value = (
        display_amount(total_summary["known_total"])
        if total_summary["known_total"] is not None else
        "Okänt" if swedish else "Неизвестно"
    )
    total_note = (
        "Arbete enligt sparade kundvillkor + resor + objektets kostnadsandelar, exkl. moms."
        if swedish else
        "Работа по сохранённым условиям + поездки + расходы долями объекта, без НДС."
    )
    if not total_summary["complete"]:
        total_note = " · ".join(
            swedish_label(reason) if swedish else reason
            for reason in total_summary["reasons"]
        ) or (
            "Ofullständiga uppgifter" if swedish else "Данные неполны"
        )
    if unconfirmed_note:
        total_note += " " + unconfirmed_note

    card_width = available / 4 - 18
    category_cards = [
        card(
            "Arbete · exkl. moms" if swedish else "Работа · без НДС",
            work_value, work_note, colors.HexColor("#f0f5f8"), card_width,
        ),
        card(
            trip_label,
            trip_value, trip_note, colors.HexColor("#f0f5f8"), card_width,
        ),
    ] + category_cards
    card_rows = [
        category_cards[index:index + 4]
        for index in range(0, len(category_cards), 4)
    ]
    for row in card_rows:
        row.extend([Spacer(1, 1)] * (4 - len(row)))
    cards = Table(card_rows, colWidths=[available / 4] * 4, hAlign="LEFT")
    cards.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    total_band = Table([[
        raw_paragraph(total_title, styles["card_label"]),
        raw_paragraph(total_value, ParagraphStyle(
            "ObjectPackageTotalValue", fontName=bold_font, fontSize=25, leading=29,
            textColor=colors.HexColor("#123424"),
        )),
        raw_paragraph(total_note, styles["card_note"]),
    ]], colWidths=[available * .30, available * .30, available * .40])
    total_band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#dcefe1")),
        ("BOX", (0, 0), (-1, -1), 1.2, colors.HexColor("#5b8c67")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    if billing_values is not None:
        language_index = int(swedish)
        components = _BILLING_COMPONENTS if data.get("include_customer_cost") else _BILLING_COMPONENTS[2:]
        has_subcontractor = CATEGORY_SUBCONTRACTOR_WORK in active_categories
        if not has_subcontractor:
            components = tuple(item for item in components if item != "subcontractor")
        elif data.get("expense_snapshot_available") and not active_categories.difference(
                {CATEGORY_MATERIALS, CATEGORY_SUBCONTRACTOR_WORK}):
            components = tuple(item for item in components if item != "other")
        rows = [[
            "Faktureringsstatus · exkl. moms" if swedish else "Статус выставления · без НДС",
            *(_BILLING_LABELS[column][language_index] for column in _BILLING_COLUMNS),
        ]]
        names = {
            "total": ("Итого по статусу", "Summa per status"),
            "work": ("Работа", "Arbete"),
            "travel": ("Поездки", "Resor"),
            "materials": ("Материалы и закупки", "Material och inköp"),
            "subcontractor": ("Работы подрядчиков", "Underentreprenörers arbete"),
            "other": ("Остальные расходы", "Övriga kostnader"),
        }
        for component in ("total", *components):
            row = [names[component][language_index]]
            for column in _BILLING_COLUMNS:
                if data["billing_members"] is None and column != "review":
                    row.append("—")
                    continue
                parts = ([value for item in components for value in billing_values[item, column]]
                         if component == "total" else billing_values[component, column])
                known = [value for value in parts if value is not None]
                if len(known) == len(parts):
                    row.append(_amount_text(sum(known, Decimal(0)), "SEK"))
                elif known:
                    row.append(_amount_text(sum(known, Decimal(0)), "SEK") +
                               (" · delbelopp" if swedish else " · известная часть"))
                else:
                    row.append("Okänt" if swedish else "Неизвестно")
            rows.append(row)
        billing_note = (
            "Status enligt SmartReport. Beloppen följer rapportens sparade kundvillkor och objektets kostnadsandelar."
            if swedish else
            "Статусы из SmartReport. Суммы берутся из расчёта по сохранённым условиям и долям объекта."
        )
        if data["billing_members"] is None:
            billing_note = ("Fakturasammansättningen är inte tillgänglig." if swedish else
                            "Сохранённый состав счетов недоступен.")
        if foreign_billing:
            billing_note += (" Andra valutor visas i kostnadsregistret." if swedish else
                             " Остальные валюты показаны в реестре расходов.")
        story.extend([
            Spacer(1, 6),
            raw_table(rows, [available * .31, available * .23, available * .23, available * .23],
                      font, bold_font, alignments=((1, "RIGHT"), (2, "RIGHT"), (3, "RIGHT")), font_size=9),
            Spacer(1, 3),
            raw_paragraph(billing_note, styles["notice"]),
        ])
    story.extend([
        Spacer(1, 10),
        cards,
        Spacer(1, 8),
        total_band,
        Spacer(1, 10),
        raw_paragraph(
            "Sammanställning enligt rapportens uppgifter. Inget materialpåslag har lagts till."
            if swedish else
            "Итог рассчитан по данным отчёта; наценка на материалы не добавлялась.",
            styles["detail_note"],
        ),
    ])

    if total_summary["supplier_net"]:
        supplier_rows = [[
            "Leverantör" if swedish else "Поставщик",
            "Dokument" if swedish else "Документы",
            "Objektets andel exkl. moms" if swedish else "Доля объекта без НДС",
        ]]
        for row in total_summary["supplier_net"]:
            if row["currency"] == "UNKNOWN":
                net = "Valuta saknas" if swedish else "Валюта не указана"
            else:
                known = row["known"]
                net = amount_or_partial(
                    None if row["unknown_count"] else known, known, row["currency"],
                )
                if known is None:
                    net = f"{net} · {row['currency']}"
            supplier_rows.append([
                row["supplier"] or ("Okänd leverantör" if swedish else "Неизвестный поставщик"),
                str(row["document_count"]),
                net,
            ])
        story.extend([
            raw_paragraph("Per leverantör" if swedish else "По поставщикам", styles["heading"]),
            raw_table(supplier_rows, [available * .45, available * .14, available * .41],
                      font, bold_font, alignments=((1, "CENTER"), (2, "RIGHT")), font_size=9),
        ])

    story.append(PageBreak())
    story.extend([
        raw_paragraph("Arbetsjournal" if swedish else "Журнал работ", styles["section_title"]),
        raw_paragraph(
            "Arbetsposter och OB-poster ingår oavsett status. Semester, sjukfrånvaro och annan "
            "frånvaro ingår inte. Resor anges separat från arbetstiden."
            if swedish else
            "Включены записи типов work и OB вне зависимости от статуса; статус каждой записи показан "
            "в таблице. Часы отпусков, болезни и других нерабочих категорий исключены. "
            "Поездки указаны отдельно от часов.",
            styles["detail_note"],
        ),
    ])
    journal = [["Дата", "Сотрудник", "Описание работ", "Часы", "Работа / OB", "Поездки", "Статус"]]
    segments_by_id = report.get("journal_segments", {})
    for row in work_rows:
        segments = segments_by_id.get(row.get("sync_id"), [])
        journal.append([
            str(row.get("date") or "Неизвестно"),
            Literal(str(row.get("employee") or ("Ej angiven" if swedish else "Не указан"))),
            Literal(
                str(data.get("journal_translations", {}).get(row.get("sync_id"))
                    or ("Svensk översättning saknas" if str(row.get("comment") or "").strip()
                        else "Arbetsbeskrivning saknas"))
                if swedish else str(row.get("comment") or "Описание не сохранено")
            ),
            _format_hours(row.get("hours")),
            _row_ob_details(row, segments),
            _format_trip_count(row.get("transport")),
            _status_text(row),
        ])
    if len(journal) == 1:
        journal.append(["Нет рабочих записей", "—", "—", "—", "—", "—", "—"])
    story.append(table(
        journal,
        [available * .13, available * .125, available * .18, available * .075,
         available * .21, available * .08, available * .20],
        font, bold_font, alignments=((3, "RIGHT"), (5, "RIGHT")),
    ))

    story.append(PageBreak())
    if data.get("include_customer_cost"):
        story.append(raw_paragraph(
            "Kundberäkning exklusive moms" if swedish else "Расчёт клиенту без НДС",
            styles["section_title"],
        ))
        cost_rows = [[
            "Post enligt sparade kundvillkor" if swedish else "Позиция из сохранённых клиентских условий",
            "Antal" if swedish else "Количество",
            "Pris" if swedish else "Ставка",
            "Belopp" if swedish else "Сумма",
        ]]
        for line in report.get("cost_lines", []):
            cost_rows.append([
                Literal(str(line.get("label") or ("Arbete" if swedish else "Работа"))),
                Literal(f"{format_quantity(line.get('quantity'))} {line.get('unit') or ''}".strip()),
                Literal(display_amount(line.get("rate"), "SEK")),
                Literal(display_amount(line.get("amount"), "SEK")),
            ])
        if len(cost_rows) == 1:
            cost_rows.append([
                "Inga poster för beräkning" if swedish else "Нет записей для расчёта",
                "—", "—", "—",
            ])
        story.append(table(
            cost_rows, [available * .43, available * .17, available * .19, available * .21],
            font, bold_font, alignments=((1, "RIGHT"), (2, "RIGHT"), (3, "RIGHT")),
        ))
        if work_rows:
            total_amount = figures["combined_amount"]
            known_amount = figures["known_combined_amount"]
            label = (
                "Beräknat belopp" if total_amount is not None else
                "Känd del av beräkningen; totalsumman är ofullständig"
                if swedish else "Известная часть расчёта; итог неполон"
            )
            value = display_amount(
                total_amount if total_amount is not None else known_amount, "SEK",
            )
            story.append(raw_paragraph(f"{label}: {value}", styles["bold"]))
        else:
            story.append(raw_paragraph(
                "Det finns inga arbetsposter för kundberäkningen; inget belopp har fastställts."
                if swedish else
                "Нет рабочих записей для клиентского расчёта; сумма не определялась.",
                styles["bold"],
            ))
        story.append(raw_paragraph(
            "Detta är en beräkning enligt sparade kundvillkor, inte en faktura, betalning eller vinst."
            if swedish else
            "Это расчёт по сохранённым клиентским условиям, не выставленный счёт, оплата или прибыль.",
            styles["detail_note"],
        ))
        for warning in dict.fromkeys(report.get("warnings", [])):
            story.append(raw_paragraph(
                f"Anmärkning: {swedish_label(warning)}" if swedish
                else f"Примечание: {warning}", styles["detail_note"],
            ))
        story.append(Spacer(1, 10))

    story.append(raw_paragraph(
        "Bekräftade inköp och kostnadsandelar" if swedish else
        "Подтверждённые закупки и расходы",
        styles["section_title"],
    ))
    story.append(raw_paragraph(
        "Beloppen avser endast den bekräftade andelen för detta objekt. "
        "Leverantörens original och hela fakturabelopp visas i respektive bilaga."
        if swedish else
        "Указаны только подтверждённые доли этого объекта. Оригинал и полная сумма "
        "счёта поставщика показаны в соответствующем приложении.",
        styles["detail_note"],
    ))
    if expenses:
        expense_rows = [[
            "Datum" if swedish else "Дата",
            "Leverantör / dokument" if swedish else "Поставщик / документ",
            "Beskrivning" if swedish else "Описание",
            "Andel för objektet" if swedish else "Доля объекта",
            "Original och sidor" if swedish else "Оригинал и страницы",
        ]]
        for document in expenses:
            share = document["share"]
            currency = str(document.get("currency_code") or "UNKNOWN").upper()
            app_number = str(document.get("application_number") or "—")
            source_evidence = document.get("object_order_evidence") or []
            references = []
            for item in source_evidence:
                page_list = ", ".join(str(page) for page in item["pages"])
                pdf_page = appendix_pages.get(expense_document_key(document))
                reference = (
                    f"Bilaga {app_number} · PDF s. {pdf_page} · "
                    f"original {item['attachment_index']} s. {page_list}"
                    if swedish else
                    f"Приложение {app_number} · PDF стр. {pdf_page} · "
                    f"оригинал {item['attachment_index']}, стр. {page_list}"
                )
                if item.get("order"):
                    reference += f" · order {item['order']}"
                references.append(reference)
            if not references:
                references.append(
                    (
                        f"Se original i bilaga {app_number} · PDF s. "
                        f"{appendix_pages.get(expense_document_key(document), '—')}"
                        if swedish else
                        f"См. оригинал в приложении {app_number} · PDF стр. "
                        f"{appendix_pages.get(expense_document_key(document), '—')}"
                    ) if data.get("include_copies") else
                    "Original ej bifogat" if swedish else "Оригинал не приложен"
                )
            category_lines = []
            allocations = document.get("object_allocations") or []
            indexes = document.get("object_allocation_indexes", range(len(allocations)))
            for index, allocation in zip(indexes, allocations):
                category = allocation.get("expense_category") or CATEGORY_UNCLASSIFIED
                category_label = swedish_label(category) if swedish else category
                net_value = _amount_text(allocation.get("net_amount"), currency)
                if swedish:
                    net_value = swedish_label(net_value)
                detail = f"{category_label} · {'netto' if swedish else 'нетто'} {net_value}"
                status = billing_statuses.get((document.get("id"), index))
                if status:
                    detail += " · " + _BILLING_LABELS[status][int(swedish)]
                note = str(
                    allocation.get("note") or document.get("document_comment")
                    or document.get("expense_description") or ""
                ).strip()
                if note:
                    detail += f" · {note}"
                category_lines.append(detail)
            for supporting in document.get("supporting_documents") or []:
                reference = " · ".join(filter(None, (
                    f"#{supporting.get('id')}" if supporting.get("id") is not None else "",
                    str(supporting.get("supplier_name") or ""),
                    str(supporting.get("invoice_number") or ""),
                )))
                if reference:
                    category_lines.append(
                        ("Bekräftande dokument: " if swedish else "Подтверждающий документ: ")
                        + reference
                    )
            if not category_lines:
                category_lines.append("Нет распределённых частей" if not swedish else "Inga fördelade delar")
            def localized_share_amount(field):
                amount = _share_amount_text(share, field, currency)
                return swedish_label(amount) if swedish else amount

            amount_label = "\n".join((
                f"{'Netto' if swedish else 'Нетто'}: {localized_share_amount('net_amount')}",
                f"{'Moms' if swedish else 'НДС'}: {localized_share_amount('vat_amount')}",
                f"{'Brutto' if swedish else 'Сумма с НДС'}: {localized_share_amount('gross_amount')}",
                f"{'Avrundning' if swedish else 'Округление'}: {localized_share_amount('rounding_amount')}",
            ))
            tax_unknown = (
                share.get("net_amount") is None or share.get("vat_amount") is None
            )
            if tax_unknown:
                amount_label += (
                    "\nNetto och moms är inte fullständigt kända."
                    if swedish else "\nНетто и НДС известны не полностью."
                )
            if share.get("net_recovered_from_original"):
                amount_label += (
                    "\nNetto verifierat mot original; registerposten är oförändrad."
                    if swedish else
                    "\nНетто сверено с оригиналом; запись реестра не менялась."
                )
            elif share.get("net_partially_recovered_from_original"):
                amount_label += (
                    "\nVissa andelar har verifierats mot original; registerposten är oförändrad."
                    if swedish else
                    "\nЧасть долей сверена с оригиналом; запись реестра не менялась."
                )
            expense_rows.append([
                Literal(str(document.get("document_date") or ("Datum saknas" if swedish else "Дата неизвестна"))),
                Literal(" · ".join(filter(None, (
                    str(document.get("supplier_name") or ("Okänd leverantör" if swedish else "Неизвестный поставщик")),
                    str(document.get("invoice_number") or ""),
                )))),
                Literal(
                    "\n".join(category_lines)
                ),
                Literal(amount_label),
                Literal("\n".join(references)),
            ])
        story.append(table(
            expense_rows,
            [available * value for value in (.10, .20, .25, .17, .28)],
            font, bold_font,
        ))
        for currency, totals_by_field in expense_totals_by_currency(expenses).items():
            story.append(Spacer(1, 5))
            story.append(raw_paragraph(
                f"Summa bekräftade andelar: {display_total(totals_by_field, 'gross_amount', currency)}"
                if swedish else
                f"Итого подтверждённых долей: {display_total(totals_by_field, 'gross_amount', currency)}",
                styles["card_value"],
            ))
            story.append(raw_paragraph(
                f"Netto: {display_total(totals_by_field, 'net_amount', currency)} · "
                f"moms: {display_total(totals_by_field, 'vat_amount', currency)} · "
                f"avrundning redovisas enligt respektive sparad fördelning. "
                f"Valuta: {currency}; olika valutor summeras inte."
                if swedish else
                f"Нетто: {display_total(totals_by_field, 'net_amount', currency)} · "
                f"НДС: {display_total(totals_by_field, 'vat_amount', currency)} · "
                f"округление показано по сохранённым распределениям. "
                f"Валюта: {currency}; разные валюты не складываются.",
                styles["detail_note"],
            ))
        for note in _expense_tax_notes(expenses, "sv" if swedish else "ru"):
            story.append(raw_paragraph(note, styles["detail_note"]))
    else:
        story.append(raw_paragraph(
            "Inga bekräftade kostnadsfördelningar hittades under perioden."
            if data.get("expense_snapshot_available") else
            "Kostnaderna är okända eftersom ett bekräftat registerutdrag inte är tillgängligt."
            if swedish else
            "Подтверждённых распределений на этот объект в выбранном диапазоне не найдено."
            if data.get("expense_snapshot_available") else
            "Расходы неизвестны: подтверждённый снимок реестра недоступен.",
            styles["detail_note"],
        ))

    remarks = list(dict.fromkeys(
        list(data.get("incomplete_reasons", []))
        + list(report.get("warnings", []))
    ))
    if remarks:
        story.extend([
            Spacer(1, 8),
            raw_paragraph("Anmärkningar" if swedish else "Примечания", styles["heading"]),
        ])
        for remark in remarks:
            text = swedish_label(remark) if swedish else remark
            story.append(raw_paragraph(f"• {text}", styles["detail_note"]))

    excluded = data.get("excluded_expenses", [])
    if excluded:
        story.extend([
            paragraph("Документы, не включённые в сумму расходов", styles["heading"]),
            paragraph(
                "Связанные с объектом документы перечислены отдельно; их распределения не включены "
                "в подтверждённый итог.",
                styles["notice"],
            ),
        ])
        excluded_rows = [["Поставщик / эмитент", "Тип / № / дата", "Статус / причина"]]
        for document in excluded:
            excluded_rows.append([
                Literal(str(document.get("supplier_name") or ("Okänd leverantör" if swedish else "Неизвестный поставщик"))),
                Literal(" · ".join(filter(None, (
                    swedish_document_type(document.get("document_type")) if swedish
                    else str(document.get("document_type") or ""),
                    str(document.get("invoice_number") or ""),
                    str(document.get("document_date") or ("Datum saknas" if swedish else "Дата неизвестна")),
                )))),
                str(document.get("excluded_reason") or "Не подтверждён"),
            ])
        story.append(table(
            excluded_rows, [available * .27, available * .28, available * .45], font, bold_font,
        ))

    if data.get("include_copies"):
        if not swedish:
            story.append(PageBreak())
        story.append(paragraph("Оглавление приложений", styles["heading"]))
        index_rows = [["№", "Поставщик / документ", "Доля объекта", "Страница"]]
        for document in expenses:
            currency = str(document.get("currency_code") or "UNKNOWN")
            page_number = appendix_pages.get(expense_document_key(document), "—")
            index_rows.append([
                str(document.get("application_number") or "—"),
                Literal(" · ".join(filter(None, (
                    str(document.get("supplier_name") or ("Okänd leverantör" if swedish else "Неизвестный поставщик")),
                    str(document.get("invoice_number") or ""),
                    str(document.get("document_date") or ("Datum saknas" if swedish else "Дата неизвестна")),
                )))),
                _share_amount_text(document["share"], "gross_amount", currency),
                str(page_number),
            ])
        if len(index_rows) == 1:
            index_rows.append(["—", "Нет подтверждённых документов для приложения", "—", "—"])
        story.append(table(
            index_rows, [available * .08, available * .52, available * .25, available * .15],
            font, bold_font,
        ))
        if expenses:
            story.append(paragraph(
                "Каждое приложение начинается с отдельного листа; оригинальные страницы после него "
                "сохранены без добавления отметок.",
                styles["notice"],
            ))
    elif expenses:
        story.append(paragraph("Копии документов не включены по выбору пользователя.", styles["notice"]))

    output = BytesIO()

    def page_footer(pdf_canvas, doc):
        pdf_canvas.saveState()
        pdf_canvas.setFont(font, 8)
        pdf_canvas.drawRightString(width - 36, 16, f"{'Sida' if swedish else 'Страница'} {doc.page}")
        pdf_canvas.restoreState()

    def later_page(pdf_canvas, doc):
        page_footer(pdf_canvas, doc)
        pdf_canvas.saveState()
        pdf_canvas.setFont(font, 8)
        pdf_canvas.setFillColor(colors.HexColor("#28475b"))
        pdf_canvas.drawString(
            36, height - 18,
            f"{object_title} · {swedish_label(data.get('period_label') or 'Нет данных')}"
            if swedish else f"{object_title} · {data.get('period_label') or 'Нет данных'}",
        )
        pdf_canvas.restoreState()

    document = SimpleDocTemplate(
        output, pagesize=landscape(A4), leftMargin=36, rightMargin=36,
        topMargin=36, bottomMargin=28,
        title="Fullständig objektrapport" if swedish else "Полный отчёт по объекту",
    )
    document.build(story, onFirstPage=page_footer, onLaterPages=later_page)
    return output.getvalue()


def _validate_pdf(path):
    reader = PdfReader(str(path), strict=True)
    page_count = len(reader.pages)
    if page_count < 1:
        raise ValueError("PDF не содержит страниц.")
    for page in reader.pages:
        page.mediabox
    return page_count


def _image_to_pdf(path, destination):
    image = Image.open(path)
    try:
        frames = list(ImageSequence.Iterator(image))
        if not frames:
            raise ValueError("Изображение не содержит страниц.")
        page_width, page_height = A4
        output = canvas.Canvas(str(destination), pagesize=A4)
        margin = 30
        for frame in frames:
            frame.load()
            rgba = frame.convert("RGBA")
            background = Image.new("RGB", rgba.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            source_width, source_height = background.size
            scale = min(
                (page_width - margin * 2) / source_width,
                (page_height - margin * 2) / source_height,
            )
            width, height = source_width * scale, source_height * scale
            output.drawImage(
                ImageReader(background), (page_width - width) / 2,
                (page_height - height) / 2, width=width, height=height,
            )
            output.showPage()
        output.save()
    finally:
        image.close()
    return _validate_pdf(destination)


def _prepare_sources(expenses, source_paths, source_errors, work_dir):
    prepared = []
    for document in expenses:
        key = expense_document_key(document)
        entry = {
            "document": document,
            "source_paths": [],
            "page_count": 0,
            "error": source_errors.get(key),
        }
        for index, raw_path in enumerate(source_paths.get(key, []), 1):
            path = Path(raw_path)
            try:
                if not path.is_file():
                    raise FileNotFoundError("Файл оригинала не найден в профиле.")
                if path.suffix.casefold() == ".pdf":
                    page_count = _validate_pdf(path)
                    source_path = path
                elif path.suffix.casefold() in IMAGE_SUFFIXES:
                    source_path = work_dir / f"appendix-{len(prepared) + 1}-{index}.pdf"
                    page_count = _image_to_pdf(path, source_path)
                else:
                    raise ValueError(f"Формат {path.suffix or 'без расширения'} не поддерживается.")
                entry["source_paths"].append(source_path)
                entry["page_count"] += page_count
            except Exception as error:
                detail = f"{path.name}: {error}"
                entry["error"] = "; ".join(filter(None, (entry["error"], detail)))
        if not source_paths.get(key) and not entry["error"]:
            entry["error"] = "Оригинал отсутствует или недоступен."
        prepared.append(entry)
    return prepared


def _whole_invoice_tax(pages, document, target_object):
    """Use the original invoice totals only for its sole, fully assigned object share."""
    allocations = document.get("allocations") or []
    if len(allocations) != 1 or len({p["attachment_index"] for p in pages}) != 1:
        return None
    allocation = allocations[0]
    gross = parse_decimal(document.get("gross_amount"))
    if (gross is None or parse_decimal(allocation.get("gross_amount")) != gross
            or str(allocation.get("object_id")) != str(target_object["id"])
            or allocation.get("company_expense") or allocation.get("destination") == "company"):
        return None

    fields = parse_invoice_pages(pages)

    def normalized(value):
        return " ".join(str(value or "").casefold().split())

    if (fields.get("non_invoice_document_type") or fields.get("prepaid")
            or fields.get("document_type") != "invoice"
            or any(not fields.get(key) or normalized(fields[key]) != normalized(document.get(key))
                   for key in ("invoice_number", "supplier_name", "document_date", "currency_code"))
            or fields["currency_code"] == "UNKNOWN"):
        return None
    sources = fields.get("field_sources") or {}
    if not all(sources.get(key) for key in ("net_amount", "gross_amount", "amount_due")):
        return None
    if any("Несколько различных значений поля" in reason for reason in fields.get("review_reasons", ())):
        return None
    net = parse_decimal(fields["net_amount"])
    vat = parse_decimal(fields.get("vat_amount"))
    invoice_gross = parse_decimal(fields["gross_amount"])
    due = parse_decimal(fields["amount_due"])
    rounding = parse_decimal(fields.get("rounding_amount"))
    if rounding is None and invoice_gross == due:
        rounding = Decimal(0)
    if vat is None and fields.get("reverse_charge") and net == invoice_gross:
        vat = Decimal(0)
    if (None in (net, vat, rounding, invoice_gross, due) or net + vat != invoice_gross
            or invoice_gross + rounding != due or due != gross):
        return None
    amounts = {"net_amount": net, "vat_amount": vat, "rounding_amount": rounding, "amount_due": due}
    for key, amount in amounts.items():
        saved = parse_decimal(document.get(key))
        if saved is not None and saved != amount:
            return None
    return {"object_options": [target_object], "net_amount": str(net), "vat_amount": str(vat),
            "rounding": str(rounding), "gross_amount": str(gross)}


def _recover_report_share_tax(data, source_paths, source_errors):
    expenses = data.get("expenses", [])
    target_object = data["object"]
    target_id = target_object.get("id")
    if target_id is None:
        return

    for document in expenses:
        allocations = document.get("object_allocations") or []
        missing_indexes = [
            index for index, allocation in enumerate(allocations)
            if parse_decimal(allocation.get("net_amount")) is None
        ]
        if not missing_indexes:
            continue

        key = expense_document_key(document)
        paths = list(source_paths.get(key, []))
        if source_errors.get(key) or not paths:
            document["net_recovery_status"] = "original_unavailable"
            document["net_recovery_error"] = (
                source_errors.get(key) or "В реестре нет доступного оригинала."
            )
            continue

        try:
            pages = []
            for attachment_index, raw_path in enumerate(paths):
                path = Path(raw_path)
                extraction = extract_local_document(path)
                page_texts = extraction.pages or (extraction.text,)
                for page_index, page_text in enumerate(page_texts):
                    page_elements = (
                        extraction.page_elements[page_index]
                        if page_index < len(extraction.page_elements) else ()
                    )
                    pages.append({
                        "source": path.name,
                        "page": page_index + 1,
                        "attachment_index": attachment_index,
                        "text": page_text,
                        "elements": page_elements,
                    })
            if not pages:
                raise ValueError("В оригинале нет страниц для проверки.")

            whole = _whole_invoice_tax(pages, document, target_object)
            parts = [whole] if whole is not None else verified_original_order_allocation_proposal(
                pages, document, [target_object],
            )
        except (
            DocumentExtractionError, OSError, PdfReadError, PdfiumError,
            RuntimeError, ValueError, KeyError,
        ) as error:
            document["net_recovery_status"] = "original_not_verified"
            document["net_recovery_error"] = str(error)
            continue

        remaining = list(parts)
        recovered_indexes = []
        recovery_errors = []
        for allocation_index in missing_indexes:
            allocation = allocations[allocation_index]
            saved_gross = parse_decimal(allocation.get("gross_amount"))
            if saved_gross is None:
                recovery_errors.append(
                    f"Доля {allocation_index + 1}: сохранённая сумма с НДС неизвестна."
                )
                continue
            object_parts = [
                part for part in remaining
                if len(part.get("object_options") or []) == 1
                and part["object_options"][0].get("id") == int(target_id)
            ]
            if not object_parts:
                marker = str(target_object.get("markning") or "").strip()
                matching_marker_parts = [
                    part for part in remaining
                    if marker and object_marking_matches(
                        str(part.get("marker") or ""), marker,
                    )
                ]
                original_markers = ", ".join(
                    sorted({
                        str(part.get("marker") or "без маркировки")
                        for part in remaining
                    })
                ) or "не распознаны"
                if matching_marker_parts:
                    reason = (
                        f"Маркировка «{marker}» найдена в оригинале, но не "
                        "сопоставлена с выбранным объектом по справочнику."
                    )
                elif marker:
                    reason = (
                        f"В оригинале нет однозначного заказа для маркировки "
                        f"«{marker}»; распознаны: {original_markers}."
                    )
                else:
                    reason = (
                        "В справочнике выбранного объекта не указана маркировка; "
                        f"в оригинале распознаны: {original_markers}."
                    )
                recovery_errors.append(f"Доля {allocation_index + 1}: {reason}")
                continue
            gross_matches = [
                part for part in object_parts
                if parse_decimal(part.get("gross_amount")) == saved_gross
            ]
            if not gross_matches:
                original_amounts = ", ".join(sorted({
                    str(parse_decimal(part.get("gross_amount")))
                    for part in object_parts
                    if parse_decimal(part.get("gross_amount")) is not None
                })) or "не определены"
                recovery_errors.append(
                    f"Доля {allocation_index + 1}: сумма с НДС {saved_gross} "
                    f"не совпала с суммами оригинала для маркировки "
                    f"«{target_object.get('markning') or 'не задана'}» "
                    f"({original_amounts})."
                )
                continue
            saved_vat = parse_decimal(allocation.get("vat_amount"))
            vat_matches = [
                part for part in gross_matches
                if saved_vat is None
                or saved_vat == parse_decimal(part.get("vat_amount"))
            ]
            if not vat_matches:
                original_vat = ", ".join(sorted({
                    str(parse_decimal(part.get("vat_amount")))
                    for part in gross_matches
                    if parse_decimal(part.get("vat_amount")) is not None
                })) or "не определён"
                recovery_errors.append(
                    f"Доля {allocation_index + 1}: сохранённый НДС {saved_vat} "
                    f"не совпал с НДС оригинала ({original_vat})."
                )
                continue
            saved_rounding = parse_decimal(allocation.get("rounding_amount"))
            candidates = [
                part for part in vat_matches
                if saved_rounding is None
                or saved_rounding == parse_decimal(part.get("rounding"))
            ]
            if len(candidates) != 1:
                if not candidates:
                    original_rounding = ", ".join(sorted({
                        str(parse_decimal(part.get("rounding")))
                        for part in vat_matches
                        if parse_decimal(part.get("rounding")) is not None
                    })) or "не определено"
                    reason = (
                        f"сохранённое округление {saved_rounding} не совпало "
                        f"с оригиналом ({original_rounding})"
                    )
                else:
                    reason = (
                        f"найдено {len(candidates)} одинаковых заказов; "
                        "однозначная сверка невозможна"
                    )
                recovery_errors.append(f"Доля {allocation_index + 1}: {reason}.")
                continue
            part = candidates[0]
            remaining.remove(part)
            allocation.update(
                net_amount=part["net_amount"],
                vat_amount=part["vat_amount"],
                rounding_amount=part["rounding"],
                net_recovered_from_original=True,
            )
            recovered_indexes.append(allocation_index)

        if recovered_indexes:
            document["net_recovered_allocation_indexes"] = recovered_indexes
            document["net_recovery_status"] = (
                "recovered" if len(recovered_indexes) == len(missing_indexes)
                else "partially_recovered"
            )
            allocations = document.get("object_allocations") or []
            parts_by_field = {
                field: _sum_partial(allocation.get(field) for allocation in allocations)
                for field in ("net_amount", "vat_amount", "gross_amount", "rounding_amount")
            }
            document["share"] = {
                field: part["value"] for field, part in parts_by_field.items()
            }
            document["share"]["known_amounts"] = {
                field: part["known"] for field, part in parts_by_field.items()
            }
            fully_recovered = len(recovered_indexes) == len(missing_indexes)
            document["share"]["net_recovered_from_original"] = fully_recovered
            document["share"]["net_partially_recovered_from_original"] = not fully_recovered
            if recovery_errors:
                document["net_recovery_error"] = "; ".join(recovery_errors)
            else:
                document.pop("net_recovery_error", None)
        elif missing_indexes:
            document["net_recovery_status"] = "no_unique_match"
            document["net_recovery_error"] = "; ".join(recovery_errors) or (
                "Оригинал не содержит однозначного совпадения с долей объекта."
            )


def _cover_pdf(prepared, page_number, data, font, bold_font):
    from xml.sax.saxutils import escape
    document = prepared["document"]
    swedish = data.get("language") == "sv"
    page_width, page_height = A4
    output = BytesIO()
    pdf_canvas = canvas.Canvas(output, pagesize=A4)
    left = 44
    available = page_width - left * 2
    y = page_height - 58
    title = ParagraphStyle("AppendixTitle", fontName=bold_font, fontSize=18, leading=22)
    heading = ParagraphStyle("AppendixHeading", fontName=bold_font, fontSize=11, leading=15)
    body = ParagraphStyle("AppendixBody", fontName=font, fontSize=11, leading=15)
    body_bold = ParagraphStyle("AppendixBodyBold", fontName=bold_font, fontSize=12, leading=16)
    object_amount_style = ParagraphStyle(
        "AppendixObjectAmount", fontName=bold_font, fontSize=18, leading=22,
        textColor=colors.HexColor("#152a38"),
    )

    def draw_paragraph(text, style, gap=9, translate=True):
        nonlocal y
        if swedish and translate:
            text = swedish_label(text)
        paragraph = Paragraph(escape(str(text)).replace("\n", "<br/>"), style)
        _, paragraph_height = paragraph.wrap(available, page_height)
        if y - paragraph_height < 60:
            raise ValueError("Данные листа приложения не помещаются на страницу.")
        paragraph.drawOn(pdf_canvas, left, y - paragraph_height)
        y -= paragraph_height + gap

    def share_amount(field):
        value = _share_amount_text(document["share"], field, currency)
        return swedish_label(value) if swedish else value

    def amount(value):
        return "Okänt" if swedish and parse_decimal(value) is None else _amount_text(value, currency)

    draw_paragraph(
        f"Bilaga {document.get('application_number')}" if swedish
        else f"Приложение {document.get('application_number')}",
        title, 18,
    )
    draw_paragraph(REPORT_ISSUER_NAME, body_bold)
    draw_paragraph(
        f"{'Objekt' if swedish else 'Объект'}: {data['object'].get('name')}",
        heading, translate=False,
    )
    if data["object"].get("markning"):
        draw_paragraph(
            f"Märkning: {data['object']['markning']}" if swedish
            else f"Маркировка: {data['object']['markning']}",
            heading, translate=False,
        )
    customer_name = data.get("customer_name") if swedish else (
        data["object"].get("company") or "не указан"
    )
    if customer_name:
        draw_paragraph(
            f"{'Beställare' if swedish else 'Заказчик'}: {customer_name}", body, translate=False,
        )
    draw_paragraph(
        f"{'Leverantör / utfärdare' if swedish else 'Поставщик / эмитент'}: "
        f"{document.get('supplier_name') or ('okänd' if swedish else 'неизвестен')}",
        body, translate=False,
    )
    if swedish:
        draw_paragraph(
            f"Typ: {swedish_document_type(document.get('document_type'))} · "
            f"Nummer: {document.get('invoice_number') or 'okänt'} · "
            f"Datum: {document.get('document_date') or 'okänt'}",
            body, translate=False,
        )
    else:
        draw_paragraph(
            f"Тип: {document.get('document_type') or 'не указан'} · "
            f"Номер: {document.get('invoice_number') or 'неизвестен'} · "
            f"Дата: {document.get('document_date') or 'неизвестна'}",
            body,
        )
    for supporting in document.get("supporting_documents") or []:
        draw_paragraph(
            f"Bekräftande dokument till denna kostnad: "
            f"{supporting.get('supplier_name') or 'okänd leverantör'} · "
            f"{supporting.get('invoice_number') or 'nummer saknas'}"
            if swedish else
            f"Подтверждающий документ к этому расходу: "
            f"{supporting.get('supplier_name') or 'поставщик не указан'} · "
            f"{supporting.get('invoice_number') or 'номер не указан'}",
            body, translate=False,
        )
    share = document["share"]
    currency = str(document.get("currency_code") or "UNKNOWN").upper()
    draw_paragraph(
        f"På detta objekt: {share_amount('gross_amount')}"
        if swedish else
        f"На этот объект: {share_amount('gross_amount')}",
        object_amount_style, translate=False,
    )
    supplier_gross = parse_decimal(document.get("gross_amount"))
    supplier_total = supplier_gross
    if document.get("allocation_basis") == "net":
        supplier_total = parse_decimal(document.get("amount_due"))
        if supplier_total is None:
            supplier_rounding = parse_decimal(document.get("rounding_amount"))
            if supplier_gross is not None and supplier_rounding is not None:
                supplier_total = supplier_gross + supplier_rounding
    draw_paragraph(
        f"Leverantörens fullständiga fakturabelopp: {amount(supplier_total)}"
        if swedish else
        f"Полная сумма фактуры поставщика: {amount(supplier_total)}",
        body_bold, translate=False,
    )
    draw_paragraph(
        f"Netto: {share_amount('net_amount')} · "
        f"moms: {share_amount('vat_amount')}."
        if swedish else
        f"Нетто: {share_amount('net_amount')} · "
        f"НДС: {share_amount('vat_amount')}.",
        body,
        translate=not swedish,
    )
    if share.get("net_recovered_from_original"):
        draw_paragraph(
            "Netto, moms och avrundning har verifierats mot originalet för rapporten; "
            "den sparade fördelningen har inte ändrats."
            if swedish else
            "Нетто, НДС и округление сверены с оригиналом только для отчёта; "
            "сохранённое распределение не изменялось.",
            body,
        )
    elif share.get("net_partially_recovered_from_original"):
        draw_paragraph(
            "Vissa andelar har verifierats mot originalet för rapporten; "
            "den sparade fördelningen har inte ändrats."
            if swedish else
            "Часть долей сверена с оригиналом только для отчёта; "
            "сохранённое распределение не изменялось.",
            body,
        )
    if (
        share.get("rounding_amount") is not None
        or share.get("known_amounts", {}).get("rounding_amount") is not None
    ):
        draw_paragraph(
            f"Avrundning av andelen: {share_amount('rounding_amount')}."
            if swedish else
            f"Округление доли: {share_amount('rounding_amount')}.",
            body,
            translate=not swedish,
        )
    other_allocations = [
        allocation for allocation in document.get("source_allocations", [])
        if allocation not in document.get("object_allocations", [])
        and (
            allocation.get("object_id") is not None
            or allocation.get("destination") == "company"
            or allocation.get("company_expense")
        )
    ]
    if other_allocations:
        draw_paragraph(
            "Fakturan omfattar inköp för flera objekt. Endast den bekräftade andelen för det valda "
            "objektet ingår i sammanställningen."
            if swedish else
            "Исходный документ распределён также на другие объекты или общие расходы фирмы. "
            "Указана только подтверждённая доля выбранного объекта, не полная сумма исходного документа.",
            body,
            translate=not swedish,
        )
    evidence = document.get("object_order_evidence") or []
    for item in evidence:
        pages = ", ".join(str(page) for page in item["pages"])
        draw_paragraph(
            f"Bekräftande underlag: order {item['order']} · "
            f"original {item['attachment_index']} s. {pages} · "
            f"märkning {data['object'].get('markning')}"
            if swedish else
            f"Подтверждающий источник: заказ {item['order']} · "
            f"оригинал {item['attachment_index']}, стр. {pages} · "
            f"маркировка {data['object'].get('markning')}",
            body,
            translate=False,
        )
    if prepared.get("error"):
        if swedish:
            draw_paragraph(
                "Kopian saknas eller är ofullständig; kontrollera originalfilen och dess kontrollsumma.",
                body_bold,
            )
        else:
            draw_paragraph(f"Копия отсутствует или неполна: {prepared['error']}", body_bold)
    else:
        if swedish:
            count = prepared["page_count"]
            draw_paragraph(
                f"Originalet är bifogat i sin helhet: {count} {'sida' if count == 1 else 'sidor'}. "
                "Originalets innehåll har inte ändrats.",
                body, translate=False,
            )
        else:
            draw_paragraph(
                f"Оригинал приложен целиком: {prepared['page_count']} стр. "
                "Содержимое оригинала не изменено.",
                body,
            )
    pdf_canvas.setFont(font, 8)
    pdf_canvas.drawRightString(page_width - left, 28, f"{'Sida' if swedish else 'Страница'} {page_number}")
    pdf_canvas.save()
    return output.getvalue()


def write_object_report_package(
    filepath, data, *, font="Helvetica", bold_font="Helvetica-Bold",
    source_paths=None, source_errors=None,
):
    """Create the report and append each confirmed source document once."""
    target = Path(filepath)
    target.parent.mkdir(parents=True, exist_ok=True)
    source_paths = source_paths or {}
    source_errors = source_errors or {}
    expenses = data.get("expenses", [])
    swedish = data.get("language") == "sv"
    _recover_report_share_tax(data, source_paths, source_errors)
    for document in expenses:
        status = document.get("net_recovery_status")
        if status in {"original_unavailable", "original_not_verified", "no_unique_match",
                      "partially_recovered"}:
            reason = (
                "Originalets nettoposter kunde inte verifieras för alla andelar."
                if swedish else
                "Нетто удалось подтвердить по оригиналу не для всех долей."
            )
            data.setdefault("incomplete_reasons", []).append(
                f"{reason} Фактура: {document.get('invoice_number') or 'неизвестна'}."
                if not swedish else
                f"{reason} Faktura: {document.get('invoice_number') or 'okänd'}."
            )
            detail = {
                "original_unavailable": (
                    "Original saknas eller kan inte läsas."
                    if swedish else "Оригинал отсутствует или не читается."
                ),
                "original_not_verified": (
                    "Originalet kunde inte verifieras mot dokumentnummer, belopp och orderrader."
                    if swedish else
                    "Оригинал не прошёл сверку номера, суммы и строк заказа."
                ),
                "no_unique_match": (
                    "Ingen entydig matchning mellan objektandel och originalorder."
                    if swedish else
                    "Нет однозначного сопоставления доли объекта и заказа оригинала."
                ),
                "partially_recovered": (
                    "Vissa andelar saknar entydig matchning mot originalordern."
                    if swedish else
                    "Для части долей нет однозначного сопоставления с заказами оригинала."
                ),
            }[status]
            data["incomplete_reasons"].append(
                f"{detail} Faktura: {document.get('invoice_number') or 'okänd'}."
                if swedish else
                f"{detail} Фактура: {document.get('invoice_number') or 'неизвестна'}."
            )
    if swedish:
        company = str(data["object"].get("company") or "").strip()
        customer_name = str(data.get("customer_name") or "").strip()
        if "customer_name" not in data and company.casefold() != "privat":
            customer_name = company
        data["customer_name"] = customer_name
        translations = data.get("journal_translations", {})
        missing_translations = any(
            str(row.get("comment") or "").strip()
            and not str(translations.get(row.get("sync_id")) or "").strip()
            for row in (data.get("customer_report") or {}).get("journal_rows", [])
            if row.get("entry_type") in WORK_TYPES
        )
        if missing_translations:
            data.setdefault("incomplete_reasons", []).append(
                "Vissa arbetsbeskrivningar saknar svensk översättning."
            )
        unconfirmed_hours = sum(
            1 for row in (data.get("customer_report") or {}).get("journal_rows", [])
            if row.get("entry_type") in WORK_TYPES
            and not row.get("hours_confirmed") and not row.get("invoiced")
        )
        if unconfirmed_hours:
            data.setdefault("incomplete_reasons", []).append(
                f"Antal arbetsposter med obekräftade timmar: {unconfirmed_hours}."
            )
        untranslated = [
            swedish_label(reason)
            for reason in data.get("incomplete_reasons", [])
        ] + [
            swedish_label(warning)
            for warning in (data.get("customer_report") or {}).get("warnings", [])
        ] + [
            swedish_category(document.get("expense_category") or document.get("document_comment"))
            for document in expenses
        ]
        if any(re.search(r"[\u0400-\u04ff]", text) for text in untranslated):
            data.setdefault("incomplete_reasons", []).append(
                "Vissa anmärkningar eller kostnadsbeskrivningar behöver översättas; "
                "originaltexten visas tills vidare."
            )
        if data.get("incomplete_reasons"):
            data["draft"] = True

    for index, document in enumerate(expenses, 1):
        document["application_number"] = str(index) if data.get("include_copies") else "—"

    with tempfile.TemporaryDirectory(prefix="smartreport-object-package-") as directory:
        work_dir = Path(directory)
        prepared = (
            _prepare_sources(expenses, source_paths, source_errors, work_dir)
            if data.get("include_copies") else []
        )
        for entry in prepared:
            document = entry["document"]
            document["object_order_evidence"] = _expense_order_evidence(
                entry["source_paths"], data["object"].get("markning"),
            )
            document["expense_description"] = _expense_description(
                document, data["object"].get("markning"),
            )
        for entry in prepared:
            if entry.get("error"):
                data.setdefault("incomplete_reasons", []).append(
                    (f"Bilaga {entry['document']['application_number']}: originalet saknas eller är ofullständigt."
                     if swedish else
                     f"Приложение {entry['document']['application_number']}: оригинал отсутствует или неполон.")
                )
                if swedish:
                    data["draft"] = True

        appendix_pages = {}
        main_pdf = b""
        for _attempt in range(5):
            main_pdf = _main_pdf_bytes(data, font, bold_font, appendix_pages)
            main_page_count = len(PdfReader(BytesIO(main_pdf)).pages)
            next_pages = {}
            next_page = main_page_count + 1
            for entry in prepared:
                key = expense_document_key(entry["document"])
                next_pages[key] = next_page
                next_page += 1 + entry["page_count"]
            if next_pages == appendix_pages:
                break
            appendix_pages = next_pages
        else:
            raise ValueError("Не удалось стабилизировать номера страниц приложений.")
        main_pdf = _main_pdf_bytes(data, font, bold_font, appendix_pages)

        writer = PdfWriter()
        writer.append(PdfReader(BytesIO(main_pdf)))
        for entry in prepared:
            document = entry["document"]
            first_page = len(writer.pages)
            cover = _cover_pdf(
                entry, first_page + 1, data, font, bold_font,
            )
            writer.append(PdfReader(BytesIO(cover)))
            writer.add_outline_item(
                f"{'Bilaga' if swedish else 'Приложение'} {document['application_number']} · "
                f"{document.get('supplier_name') or ('okänd leverantör' if swedish else 'неизвестный поставщик')}",
                first_page,
            )
            for source_path in entry["source_paths"]:
                writer.append(str(source_path))
        writer.add_metadata({
            "/Title": "Fullständig objektrapport" if swedish else "Полный отчёт по объекту",
            "/Author": REPORT_ISSUER_NAME,
        })
        with target.open("wb") as output:
            writer.write(output)
    return {
        "path": str(target),
        "page_count": len(PdfReader(str(target)).pages),
        "incomplete_reasons": list(dict.fromkeys(data.get("incomplete_reasons", []))),
    }
