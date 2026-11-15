"""Full object report PDF assembly and confirmed-expense projections."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import re
import sys
import tempfile

from PIL import Image, ImageSequence
from pypdf import PdfReader, PdfWriter
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
from expense_documents import parse_decimal
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
    for document in documents:
        key = expense_document_key(document)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        allocations = [
            allocation for allocation in document.get("allocations", [])
            if allocation.get("object_id") is not None
            and int(allocation["object_id"]) == int(object_id)
            and not allocation.get("company_expense")
            and allocation.get("destination") != "company"
        ]
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
            excluded.append({**document, "object_allocations": allocations, "excluded_reason": reason})
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
            "object_allocations": allocations,
            "source_allocations": list(document.get("allocations", [])),
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
    work_amounts = [
        line.get("amount") for line in lines
        if not str(line.get("label") or "").startswith("Resa")
    ]
    travel_lines = [
        line for line in lines if str(line.get("label") or "").startswith("Resa")
    ]
    work = _money_parts(work_amounts)
    travel = _money_parts(line.get("amount") for line in travel_lines)
    work_total = parse_decimal(totals.get("work_amount"))
    ob_total = parse_decimal(totals.get("ob_amount"))
    if work_total is not None or ob_total is not None:
        work_amount = (
            work_total + ob_total
            if work_total is not None and ob_total is not None else None
        )
    else:
        work_amount = work["known"] if work["complete"] else None
    travel_amount = parse_decimal(totals.get("transport_amount"))
    if travel_amount is None and travel["complete"]:
        travel_amount = travel["known"] or Decimal(0)
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
        and all(value is not None for value in trips) and known_trips == 0
    ):
        travel_amount = Decimal(0)
        travel["known"] = Decimal(0)
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
        "known_work_amount": work["known"],
        "work_complete": work["complete"] and work_amount is not None,
        "travel_amount": travel_amount,
        "known_travel_amount": travel["known"],
        "travel_complete": travel["complete"] and travel_amount is not None,
        "combined_amount": combined,
        "known_combined_amount": known_combined,
        "trip_breakdown": trip_breakdown,
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
    expense_totals = expense_totals_by_currency(expenses)

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
        if not work_rows:
            return "Ej beräknat" if swedish else "Не рассчитывалось"
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

    def card(label, value, note, background):
        body = [
            [raw_paragraph(label, styles["card_label"])],
            [raw_paragraph(value, styles["card_value"])],
            [raw_paragraph(note, styles["card_note"])],
        ]
        result = Table(body, colWidths=[available * .47 - 26], hAlign="LEFT")
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
    combined_value = amount_or_partial(
        figures["combined_amount"], figures["known_combined_amount"],
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
    if figures["unknown_trip_rows"] == 0:
        trip_label = "Resor" if swedish else "Поездки"
    else:
        trip_label = "Resor · ofullständiga uppgifter" if swedish else "Поездки · данные неполны"

    if expense_totals:
        expense_values = []
        for currency, amounts in expense_totals.items():
            value = display_total(amounts, "gross_amount", currency)
            expense_values.append(
                value if f" {currency}" in value else f"{currency}: {value}"
            )
        expense_value = "\n".join(expense_values)
        tax_notes = _expense_tax_notes(expenses, "sv" if swedish else "ru")
        if tax_notes:
            expense_note = (
                "Beloppen är inklusive moms enligt bekräftade objektandelar. "
                if swedish else
                "Суммы с НДС по подтверждённым долям объекта. "
            ) + " ".join(tax_notes)
        else:
            expense_note = (
                "Beloppen är inklusive moms; netto och moms är kända för samtliga bekräftade andelar."
                if swedish else
                "Суммы с НДС; нетто и НДС известны для всех подтверждённых долей."
            )
        if any(amounts.get("gross_amount") is None for amounts in expense_totals.values()):
            expense_note += (
                " En del av bruttoandelarna saknas; visat belopp är endast den kända delen."
                if swedish else
                " Часть сумм долей неизвестна; указана только известная часть."
            )
    else:
        expense_value = "Inga bekräftade andelar" if swedish else "Нет подтверждённых долей"
        expense_note = (
            "Inga bekräftade inköps- eller kostnadsfördelningar i perioden."
            if swedish else "Нет подтверждённых закупок/расходов в периоде."
        )
    labor_trip_note = (
        "Arbete och resor enligt sparade kundvillkor, exkl. moms. Inköp ingår inte; "
        "detta är inte en faktura eller ett belopp att betala."
        if swedish else
        "Работа и поездки по сохранённым условиям, без НДС. Закупки не включены; "
        "это не счёт и не сумма к оплате."
    )
    cards = Table([
        [
            card("Arbete" if swedish else "Работа", work_value, work_note,
                 colors.HexColor("#f0f5f8")),
            card(trip_label, trip_value, trip_note, colors.HexColor("#f0f5f8")),
        ],
        [
            card("Arbete och resor tillsammans" if swedish else "Работа и поездки вместе",
                 combined_value, labor_trip_note, colors.HexColor("#e5f0ea")),
            card("Bekräftade inköp och kostnader" if swedish else "Подтверждённые закупки и расходы",
                 expense_value, expense_note, colors.HexColor("#f7f2e7")),
        ],
    ], colWidths=[available * .50, available * .50], hAlign="LEFT")
    cards.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.extend([
        Spacer(1, 8),
        cards,
        Spacer(1, 8),
        raw_paragraph(
            "Underlaget visar registrerade uppgifter och beräkningar enligt sparade kundvillkor. "
            "Det är inte en slutlig faktura och anger inte betalning eller vinst."
            if swedish else
            "Отчёт показывает учётные данные и расчёты по сохранённым условиям. "
            "Это не окончательный счёт, не подтверждение оплаты и не прибыль.",
            styles["detail_note"],
        ),
        PageBreak(),
    ])

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
                    or ("Arbetsbeskrivning behöver förtydligas" if str(row.get("comment") or "").strip()
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
            amount_label = swedish_label(
                _share_amount_text(share, "gross_amount", currency),
            )
            tax_unknown = (
                share.get("net_amount") is None or share.get("vat_amount") is None
            )
            if tax_unknown:
                amount_label += (
                    "\nNetto och moms är inte fullständigt kända."
                    if swedish else "\nНетто и НДС известны не полностью."
                )
            expense_rows.append([
                Literal(str(document.get("document_date") or ("Datum saknas" if swedish else "Дата неизвестна"))),
                Literal(" · ".join(filter(None, (
                    str(document.get("supplier_name") or ("Okänd leverantör" if swedish else "Неизвестный поставщик")),
                    str(document.get("invoice_number") or ""),
                )))),
                Literal(
                    document.get("expense_description")
                    or _expense_description(document, marking)
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
                "Vissa arbetsbeskrivningar behöver översättas eller förtydligas."
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
