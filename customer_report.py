"""Customer-only projection and PDF shared by the report and journal views."""

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
import math
import os
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    KeepTogether, LongTable, PageBreak, Paragraph, SimpleDocTemplate,
    Spacer, Table, TableStyle,
)


WORK_TYPES = {"work", "ob"}
CATEGORY_LABELS = {
    "weekday_early_evening": "OB1",
    "weekday_late_evening_night": "OB2",
    "weekend": "OB helg",
    "holiday": "OB helgdag",
    "saturday": "OB lördag",
    "sunday": "OB söndag",
}
TYPE_LABELS = {
    "work": "Arbete", "ob": "Arbete med OB", "atf": "Arbetstidsförkortning",
    "sick": "Sjukfrånvaro", "vacation": "Semester", "holiday": "Röda dagar",
}


def positive_rate(value):
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def money(quantity, rate):
    if not quantity:
        return 0.0
    if rate is None:
        return None
    return float((Decimal(str(quantity)) * Decimal(str(rate))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    ))


def format_number(value, decimals=2):
    if value is None:
        return "Ej fastställt"
    return f"{value:,.{decimals}f}".replace(",", " ").replace(".", ",")


def format_quantity(value, decimal_separator=","):
    if value is None:
        return format_number(value)
    text = format_number(value, 6).rstrip("0")
    if text.endswith(","):
        text += "0"
    return text.replace(",", decimal_separator)


def prepare_customer_costs(report):
    """Use recorded customer prices, falling back only to the object's price."""
    grouped = defaultdict(float)
    trips = 0.0
    for row in report["journal_rows"]:
        if row["entry_type"] not in WORK_TYPES:
            continue
        saved_rate = row["rate"] if row["entry_type"] == "work" else row.get("work_rate_cache")
        rate = positive_rate(saved_rate) or positive_rate(report["rate"])
        grouped[rate] += float(row["hours"])
        trips += float(row["transport"] or 0)
    lines = []
    if report["is_fast_pris"]:
        lines.append({
            "label": "Fast pris (inklusive resa och OB)", "quantity": 1,
            "unit": "st", "rate": report["fast_pris_amount"],
            "amount": report["fast_pris_amount"],
        })
    else:
        for rate, hours in sorted(grouped.items(), key=lambda item: (item[0] is None, item[0] or 0)):
            lines.append({
                "label": "Arbete", "quantity": hours, "unit": "h",
                "rate": rate, "amount": money(hours, rate),
            })
        for category, hours in sorted(report["totals"]["ob_categories"].items()):
            if hours:
                lines.append({
                    "label": CATEGORY_LABELS.get(category, "Övrig OB") + " – tillägg",
                    "quantity": hours, "unit": "h", "rate": None, "amount": None,
                })
        if report["totals"]["without_ob_hours"] is None:
            lines.append({
                "label": "OB – fördelning ej verifierad", "quantity": None,
                "unit": "h", "rate": None, "amount": None,
            })
        if trips:
            travel_rate = positive_rate(report["transport_rate"])
            lines.append({
                "label": "Resa", "quantity": trips, "unit": "st",
                "rate": travel_rate, "amount": money(trips, travel_rate),
            })
    totals = report["totals"]
    totals["work_amount"] = sum(
        line["amount"] or 0 for line in lines
        if line["label"] == "Arbete" or report["is_fast_pris"]
    )
    if any(line["amount"] is None for line in lines if line["label"] == "Arbete"):
        totals["work_amount"] = None
    totals["transport_amount"] = (
        money(trips, positive_rate(report["transport_rate"]))
        if not report["is_fast_pris"] else 0.0
    )
    known = float(sum(Decimal(str(line["amount"])) for line in lines if line["amount"] is not None))
    unknown = any(line["amount"] is None for line in lines)
    totals["known_subtotal"] = known
    totals["invoice_total"] = None if unknown else known
    report["cost_lines"] = lines
    report["recorded_trips"] = trips
    return report


def write_customer_pdf(report, filepath, font, bold_font, translate, *, journal_only=False):
    styles = {
        "body": ParagraphStyle("CustomerBody", fontName=font, fontSize=10, leading=13),
        "bold": ParagraphStyle("CustomerBold", fontName=bold_font, fontSize=10, leading=13),
        "header": ParagraphStyle(
            "CustomerHeader", fontName=bold_font, fontSize=10, leading=12,
            textColor=colors.HexColor("#152a38"),
        ),
        "title": ParagraphStyle("CustomerTitle", fontName=bold_font, fontSize=17, leading=21),
        "total": ParagraphStyle("CustomerTotal", fontName=bold_font, fontSize=12, leading=16),
    }
    for name in ("body", "header", "total"):
        styles[name + "_number"] = ParagraphStyle(
            "Customer" + name + "Number", parent=styles[name], alignment=TA_RIGHT,
        )
    width, _height = landscape(A4)
    available = width - 72

    def paragraph(value, style="body"):
        return Paragraph(escape(str(value)).replace("\n", "<br/>"), styles[style])

    def heading(title):
        return [
            paragraph(title, "title"),
            paragraph(
                f"Beställare: {report['company']}   |   Objekt: {report['object']}\n"
                f"Period: {report['date_from']} – {report['date_to']}",
            ),
            Spacer(1, 10),
        ]

    def table(data, widths, *, total=False, numeric=()):
        rows = [
            [
                paragraph(
                    value,
                    ("header" if index == 0 else "total" if total and index == len(data) - 1 else "body")
                    + ("_number" if column in numeric else ""),
                )
                for column, value in enumerate(row)
            ]
            for index, row in enumerate(data)
        ]
        result = LongTable(
            rows, colWidths=widths, repeatRows=1, splitByRow=1, splitInRow=1, hAlign="LEFT",
        )
        result.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbe7ef")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f8")]),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor("#28475b")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        if total:
            result.setStyle(TableStyle([
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#dbe7ef")),
                ("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#28475b")),
                ("NOSPLIT", (0, -2), (-1, -1)),
            ]))
        return result

    def highlight(label, value, *, strongest=False):
        style = "total" if strongest else "bold"
        result = Table([[paragraph(label, style), paragraph(value, style)]],
                       colWidths=[available * .70, available * .30], hAlign="LEFT")
        result.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1),
             colors.HexColor("#c8ded4" if strongest else "#e4edf4")),
            ("BOX", (0, 0), (-1, -1), 1.3 if strongest else .8, colors.HexColor("#28475b")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        return result

    story = []
    totals = report["totals"]
    if not journal_only:
        story.extend(heading("Fakturaunderlag"))
        story.append(paragraph("Status: Pågående"))
        categories = ["weekday_early_evening", "weekday_late_evening_night"]
        categories += sorted(set(totals["ob_categories"]) - set(categories))
        labels = [CATEGORY_LABELS.get(category, "Övrig OB") for category in categories]
        rows = [["Medarbetare", "Arbetade timmar", *["Varav " + label for label in labels]]]
        for employee in report["prepared_rows"]:
            values = {
                "weekday_early_evening": employee["ob1_hours"],
                "weekday_late_evening_night": employee["ob2_hours"],
                **employee["other_ob_categories"],
            }
            rows.append([
                employee["employee"], format_quantity(employee["hours"]),
                *[format_quantity(
                    values.get(category, 0) if employee["without_ob_hours"] is not None else None,
                ) for category in categories],
            ])
        rows.append([
            "Totalt antal timmar", format_quantity(totals["hours"]),
            *[format_quantity(
                totals["ob_categories"].get(category, 0)
                if totals["without_ob_hours"] is not None else None,
            ) for category in categories],
        ])
        story.append(table(
            rows, [available * .36] + [available * .64 / (len(categories) + 1)] * (len(categories) + 1),
            total=True, numeric=tuple(range(1, len(categories) + 2)),
        ))
        story.append(paragraph("OB-timmar ingår i arbetade timmar och läggs inte till."))
        story.append(Spacer(1, 10))
        costs = [["Position", "Antal", "Pris (SEK)", "Belopp (SEK)"]]
        for line in report["cost_lines"]:
            costs.append([
                line["label"], format_quantity(line["quantity"]) + " " + line["unit"],
                format_number(line["rate"]), format_number(line["amount"]),
            ])
        story.append(table(
            costs, [available * .43, available * .17, available * .18, available * .22],
            numeric=(1, 2, 3),
        ))
        preliminary = totals["invoice_total"] is None
        bottom = [
            Spacer(1, 10),
            highlight(
                "Preliminärt totalbelopp" if preliminary else "Totalt belopp",
                format_number(totals["known_subtotal"]) + " SEK", strongest=True,
            ),
            paragraph("Samtliga belopp är exklusive moms."),
        ]
        if preliminary:
            bottom.append(paragraph(
                "PRELIMINÄRT: Endast kända belopp ingår, utan ej fastställda priser eller "
                "OB-tillägg. Slutbeloppet kan inte fastställas.", "bold",
            ))
        story.append(KeepTogether(bottom))
        story.append(PageBreak())
    story.extend(heading("Arbetsjournal"))
    journal = [["Datum", "Medarbetare", "Typ", "Timmar", "Tid / OB", "Arbetsbeskrivning"]]
    for row in report["journal_rows"]:
        segments = report["journal_segments"].get(row["sync_id"], [])
        details = []
        for segment in segments:
            label = CATEGORY_LABELS.get(segment["category"], "Dagtid" if not segment["premium_percent"] else "Övrig OB")
            details.append(
                f"{segment['start_time']}–{segment['end_time']}: "
                f"{format_quantity(segment['hours'])} h {label}"
            )
        journal.append([
            row["date"], row["employee"], TYPE_LABELS.get(row["entry_type"], "Övrig tid"),
            format_quantity(row["hours"]), "\n".join(details),
            translate(row["comment"] or ""),
        ])
    story.append(table(journal, [65, 115, 100, 55, 140, available - 475], numeric=(3,)))
    journal_work_hours = sum(float(row["hours"]) for row in report["journal_rows"] if row["entry_type"] in WORK_TYPES)
    if abs(journal_work_hours - totals["hours"]) > 0.000001:
        raise ValueError("Arbetsjournalens timmar stämmer inte med fakturaunderlaget.")
    story.extend([
        Spacer(1, 8),
        highlight("Totalt antal arbetade timmar", format_quantity(journal_work_hours) + " h"),
    ])
    absence = defaultdict(float)
    for row in report["journal_rows"]:
        if row["entry_type"] not in WORK_TYPES:
            absence[TYPE_LABELS.get(row["entry_type"], "Övrig tid")] += float(row["hours"])
    if absence:
        story.append(paragraph("; ".join(f"{label}: {format_quantity(hours)} h" for label, hours in sorted(absence.items()))))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font, 9)
        canvas.drawString(36, 17, f"{report['company']} | {report['date_from']} – {report['date_to']}")
        canvas.drawRightString(width - 36, 17, f"Sida {doc.page}")
        canvas.restoreState()

    document = SimpleDocTemplate(
        os.fspath(filepath) if isinstance(filepath, os.PathLike) else filepath,
        pagesize=landscape(A4), leftMargin=36, rightMargin=36,
        topMargin=30, bottomMargin=32, title="Fakturaunderlag och arbetsjournal",
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)
