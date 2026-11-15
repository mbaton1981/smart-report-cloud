"""Full object report PDF assembly and confirmed-expense projections."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import re
import tempfile

from PIL import Image, ImageSequence
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
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
    return "Неизвестно" if value is None else format_quantity(value)


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


def _employee_totals(report):
    trips_by_employee = defaultdict(list)
    hours_by_employee = defaultdict(float)
    for row in report.get("journal_rows", []):
        if row.get("entry_type") not in WORK_TYPES:
            continue
        employee = str(row.get("employee") or "Не указан")
        hours_by_employee[employee] += float(row.get("hours") or 0)
        trips_by_employee[employee].append(row.get("transport"))
    prepared = {row["employee"]: row for row in report.get("prepared_rows", [])}
    result = []
    for employee in sorted(hours_by_employee, key=str.casefold):
        hours = hours_by_employee[employee]
        prepared_row = prepared.get(employee, {})
        ordinary = prepared_row.get("without_ob_hours")
        ob = hours - ordinary if ordinary is not None else None
        trips = trips_by_employee[employee]
        trip_total = (
            None if any(value is None for value in trips)
            else sum(float(value) for value in trips)
        )
        result.append((employee, hours, ordinary, ob, trip_total))
    return result


def _paragraph(value, style):
    from xml.sax.saxutils import escape
    return Paragraph(escape(str(value)).replace("\n", "<br/>"), style)


def _table(rows, widths, font, bold_font, *, repeat_rows=1, alignments=()):
    body = ParagraphStyle("ObjectPackageBody", fontName=font, fontSize=8.5, leading=10.5)
    header = ParagraphStyle(
        "ObjectPackageHeader", fontName=bold_font, fontSize=8.5, leading=10.5,
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
        "bold": ParagraphStyle("ObjectPackageBold", fontName=bold_font, fontSize=9, leading=12),
    }
    story = [
        paragraph(REPORT_ISSUER_NAME, styles["issuer"]),
        paragraph("Полный отчёт по объекту", styles["title"]),
        Spacer(1, 7),
    ]
    if swedish and data.get("draft"):
        story.append(paragraph("UTKAST", styles["bold"]))
    object_row = data["object"]
    object_title = str(object_row.get("name") or "Объект")
    if object_row.get("markning"):
        object_title += f" · {object_row['markning']}"
    story.extend([
        raw_paragraph(
            f"Beställare: {data.get('customer_name') or 'Ej angiven'}" if swedish
            else f"Заказчик: {object_row.get('company') or 'не указан'}",
            styles["body"],
        ),
        raw_paragraph(f"{'Objekt' if swedish else 'Объект'}: {object_title}", styles["bold"]),
        paragraph(
            f"Период: {data.get('period_label') or 'Нет данных'} · "
            f"Сформировано: {data['generated_at']}",
            styles["body"],
        ),
        paragraph(
            "Архивный объект" if not object_row.get("is_active", 1) else "Объект активен",
            styles["body"],
        ),
        Spacer(1, 6),
        paragraph(
            "Пакет предназначен для проверки выполненных работ и подготовки расчёта. "
            "Он не является финальной фактурой и не фиксирует оплату или прибыль; "
            "формирование не меняет записи учёта.",
            styles["notice"],
        ),
    ])
    if data.get("incomplete_reasons"):
        reasons = "; ".join(
            swedish_label(reason) if swedish else reason
            for reason in dict.fromkeys(data["incomplete_reasons"])
        )
        story.extend([
            Spacer(1, 5),
            raw_paragraph(
                f"{'Underlaget är ofullständigt' if swedish else 'Пакет неполный'}: {reasons}",
                styles["bold"],
            ),
        ])

    report = data.get("customer_report") or {}
    work_rows = [
        row for row in report.get("journal_rows", [])
        if row.get("entry_type") in WORK_TYPES
    ]
    totals = report.get("totals", {})
    hours_total = sum(float(row.get("hours") or 0) for row in work_rows)
    ordinary = totals.get("without_ob_hours")
    ob_hours = hours_total - ordinary if ordinary is not None else None
    trip_values = [row.get("transport") for row in work_rows]
    trips = (
        None if any(value is None for value in trip_values)
        else sum(float(value) for value in trip_values)
    )
    employees = {str(row.get("employee") or "Не указан") for row in work_rows}
    summary = [
        ["Показатель", "Значение"],
        ["Рабочие часы, включая OB один раз", _format_hours(hours_total)],
        ["Обычные часы", _format_hours(ordinary)],
        ["OB-часы (часть общего количества)", _format_hours(ob_hours)],
        ["Сотрудники", str(len(employees))],
        ["Поездки", _format_trip_count(trips)],
    ]
    story.extend([
        paragraph("Сводка работ", styles["heading"]),
        table(summary, [available * .66, available * .34], font, bold_font),
    ])

    employees_rows = [["Сотрудник", "Всего часов", "Обычные", "OB", "Поездки"]]
    for employee, hours, regular, ob, employee_trips in _employee_totals(report):
        employees_rows.append([
            Literal("Ej angiven" if swedish and employee == "Не указан" else employee),
            _format_hours(hours), _format_hours(regular), _format_hours(ob),
            _format_trip_count(employee_trips),
        ])
    if len(employees_rows) > 1:
        story.extend([
            paragraph("Итоги по сотрудникам", styles["heading"]),
            table(
                employees_rows,
                [available * .33, available * .17, available * .17, available * .17, available * .16],
                font, bold_font,
            ),
        ])

    if data.get("include_customer_cost"):
        story.append(paragraph("Расчёт клиенту без НДС", styles["heading"]))
        cost_rows = [["Позиция из сохранённых клиентских условий", "Количество", "Ставка", "Сумма"]]
        for line in report.get("cost_lines", []):
            cost_rows.append([
                line.get("label") or "Работа",
                f"{format_quantity(line.get('quantity'))} {line.get('unit') or ''}".strip(),
                _amount_text(line.get("rate"), "SEK"),
                _amount_text(line.get("amount"), "SEK"),
            ])
        if len(cost_rows) == 1:
            cost_rows.append(["Нет записей для расчёта", "—", "—", "—"])
        story.append(table(
            cost_rows, [available * .43, available * .17, available * .19, available * .21],
            font, bold_font, alignments=((1, "RIGHT"), (2, "RIGHT"), (3, "RIGHT")),
        ))
        if work_rows:
            known_amount = totals.get("known_subtotal")
            total_amount = totals.get("invoice_total")
            label = "Расчётная сумма" if total_amount is not None else "Известная часть расчёта; итог неполон"
            value = _amount_text(total_amount if total_amount is not None else known_amount, "SEK")
            story.append(Spacer(1, 4))
            story.append(paragraph(f"{label}: {value}", styles["bold"]))
        else:
            story.append(paragraph(
                "Нет рабочих записей для клиентского расчёта; сумма не определялась.",
                styles["bold"],
            ))
        story.append(paragraph(
            "Это расчёт по сохранённым клиентским условиям, не выставленный счёт, оплата или прибыль.",
            styles["notice"],
        ))
        for warning in dict.fromkeys(report.get("warnings", [])):
            story.append(raw_paragraph(
                f"Anmärkning: {swedish_label(warning)}" if swedish
                else f"Примечание: {warning}", styles["notice"],
            ))

    story.extend([
        paragraph("Подробные рабочие записи", styles["heading"]),
        paragraph(
            "Включены записи типов work и OB вне зависимости от статуса; статус каждой записи показан "
            "в таблице. Часы отпусков, болезни и других нерабочих категорий исключены. "
            "Поездки указаны отдельно от часов.",
            styles["notice"],
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
        [available * .085, available * .13, available * .22, available * .075,
         available * .225, available * .08, available * .185],
        font, bold_font, alignments=((3, "RIGHT"), (5, "RIGHT")),
    ))

    expenses = data.get("expenses", [])
    story.append(paragraph("Реестр подтверждённых расходов", styles["heading"]))
    if expenses:
        expense_rows = [[
            "Приложение", "Поставщик / эмитент", "Тип / № / дата",
            "Нетто", "НДС", "Сумма доли", "Округление", "Валюта",
        ]]
        for document in expenses:
            share = document["share"]
            no_tax_split = share.get("net_amount") is None and share.get("vat_amount") is None
            amount_label = _share_amount_text(share, "gross_amount")
            if no_tax_split and (
                share.get("gross_amount") is not None
                or share.get("known_amounts", {}).get("gross_amount") is not None
            ):
                amount_label += " · сумма"
            rounding = (
                _share_amount_text(share, "rounding_amount")
                if (
                    share.get("rounding_amount") is not None
                    or share.get("known_amounts", {}).get("rounding_amount") is not None
                )
                else "Не сохранено"
            )
            category = document.get("expense_category") or document.get("document_comment") or ""
            if swedish:
                category = swedish_category(category)
            expense_description = " · ".join(filter(None, (
                swedish_document_type(document.get("document_type")) if swedish
                else str(document.get("document_type") or ""),
                str(document.get("invoice_number") or ""),
                str(document.get("document_date") or ("Datum saknas" if swedish else "Дата неизвестна")),
                str(category),
            )))
            expense_rows.append([
                str(document.get("application_number") or "—"),
                Literal(str(document.get("supplier_name") or ("Okänd leverantör" if swedish else "Неизвестный поставщик"))),
                Literal(expense_description),
                _share_amount_text(share, "net_amount"),
                _share_amount_text(share, "vat_amount"),
                amount_label,
                rounding,
                str(document.get("currency_code") or "UNKNOWN"),
            ])
        story.append(table(
            expense_rows,
            [available * value for value in (.07, .16, .17, .11, .10, .13, .12, .14)],
            font, bold_font,
        ))
        for currency, totals_by_field in expense_totals_by_currency(expenses).items():
            story.append(paragraph(
                f"Итого долей, {currency}: нетто "
                f"{_total_amount_text(totals_by_field, 'net_amount', currency)}; "
                f"НДС {_total_amount_text(totals_by_field, 'vat_amount', currency)}; "
                f"сумма {_total_amount_text(totals_by_field, 'gross_amount', currency)}. "
                "Разные валюты не складываются.",
                styles["body"],
            ))
    else:
        story.append(paragraph(
            "Подтверждённых распределений на этот объект в выбранном диапазоне не найдено."
            if data.get("expense_snapshot_available") else
            "Расходы неизвестны: подтверждённый снимок реестра недоступен.",
            styles["body"],
        ))

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
    body = ParagraphStyle("AppendixBody", fontName=font, fontSize=10, leading=14)
    body_bold = ParagraphStyle("AppendixBodyBold", fontName=bold_font, fontSize=10, leading=14)

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

    draw_paragraph(f"Приложение {document.get('application_number')}", title, 18)
    draw_paragraph(REPORT_ISSUER_NAME, body_bold)
    draw_paragraph(
        f"{'Objekt' if swedish else 'Объект'}: {data['object'].get('name')}",
        heading, translate=False,
    )
    customer_name = data.get("customer_name") or "Ej angiven" if swedish else (
        data["object"].get("company") or "не указан"
    )
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
    currency = str(document.get("currency_code") or "UNKNOWN")
    draw_paragraph(
        f"Доля объекта: нетто {_share_amount_text(share, 'net_amount')}; "
        f"НДС {_share_amount_text(share, 'vat_amount')}; "
        f"сумма {_share_amount_text(share, 'gross_amount', currency)}.",
        body_bold,
    )
    if (
        share.get("rounding_amount") is not None
        or share.get("known_amounts", {}).get("rounding_amount") is not None
    ):
        draw_paragraph(
            f"Округление доли: {_share_amount_text(share, 'rounding_amount', currency)}.",
            body,
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
            "Исходный документ распределён также на другие объекты или общие расходы фирмы. "
            "Указана только подтверждённая доля выбранного объекта, не полная сумма исходного документа.",
            body,
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
        if not customer_name:
            data.setdefault("incomplete_reasons", []).append("Beställarens namn saknas.")
        if missing_translations:
            data.setdefault("incomplete_reasons", []).append(
                "Vissa arbetsbeskrivningar behöver översättas eller förtydligas."
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
