"""Customer-only projection and PDF shared by the report and journal views."""

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
import math
import os
from xml.sax.saxutils import escape
from customer_pricing import row_prices
from customer_intervals import calculated_row
from fixed_pricing import report_agreements

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
    """Use only confirmed historical customer prices; never current wage/catalogue rates."""
    grouped = defaultdict(float)
    travel = defaultdict(float)
    ob_costs = defaultdict(float)
    ob_intervals = defaultdict(list)
    groups, allocations = report_agreements(report)
    trips = 0.0
    unassigned_hours = 0.0
    interval_lines, interval_details, supplement_details = [], [], []
    for row in report["journal_rows"]:
        if row["entry_type"] not in WORK_TYPES:
            continue
        prices = row_prices(row)
        fixed = allocations.get(row["sync_id"])
        count = float(row["transport"] or 0)
        trips += count
        if not fixed and any(
            agreement["date_from"] <= row["date"] <= agreement["date_to"]
            for agreement in groups.values()
        ):
            unassigned_hours += float(row["hours"])
            continue
        extra = fixed["members"][row["sync_id"]]["extra"] if fixed else dict.fromkeys(("base", "ob", "travel"), True)
        if prices.get("supplement_allocation"):
            allocation = prices["supplement_allocation"]
            for part in allocation["parts"]:
                supplement_details.append({**part, "date": row["date"], "employee": row["employee"],
                                           "shift_id": row["id"], "base": allocation["base"],
                                           "scheme": allocation["scheme"]})
                hours = float(part["hours"])
                interval_lines.extend((
                    {"label": "Arbete – bekräftad fördelning", "quantity": hours, "unit": "h",
                     "rate": float(allocation["base"]), "amount": float(part["base_amount"])},
                    {"label": "Kundtillägg – " + ("Helg +100%" if part["bucket"] == "weekend" else
                                                "OB" if allocation["scheme"] == "ob" else "Övertid"),
                     "quantity": hours, "unit": "h", "rate": float(part["premium_amount"]) / hours,
                     "amount": float(part["premium_amount"])},
                ))
            travel[(prices["travel"], False)] += count
            continue
        if prices.get("interval_terms") or row.get("time_facts"):
            try:
                details = calculated_row(row, prices)
            except ValueError as error:
                details = []
                interval_lines.append({"label": f"Arbete – {row['date']} / {row['employee']}: {error}",
                                       "quantity": row["hours"], "unit": "h", "rate": None, "amount": None})
            for part in details:
                interval_details.append({**part, "employee": row["employee"], "shift_id": row["id"],
                                         "time_source": "schedule" if prices.get("schedule_application") else "actual",
                                         "terms_id": prices["interval_terms"]["id"]})
                hours = float(part["hours"])
                if extra["base"]:
                    interval_lines.append({"label": "Arbete – kundvillkor", "quantity": hours, "unit": "h",
                                           "rate": float(part["base"]) if part["base"] is not None else None,
                                           "amount": float(part["base_amount"]) if part["base_amount"] is not None else None})
                if extra["ob"]:
                    amount = float(part["premium_amount"]) if part["premium_amount"] is not None else None
                    interval_lines.append({"label": "Kundtillägg – " + part["mode"], "quantity": hours, "unit": "h",
                                           "rate": amount / hours if amount is not None else None, "amount": amount})
            if extra["travel"]:
                travel[(prices["travel"], bool(fixed))] += count
            continue
        if extra["base"]:
            grouped[(prices["base"], bool(fixed))] += float(row["hours"])
        if extra["travel"]:
            travel[(prices["travel"], bool(fixed))] += count
        if row["entry_type"] == "ob" and extra["ob"]:
            for segment in report.get("journal_segments", {}).get(row["sync_id"], []):
                if float(segment["premium_percent"] or 0) > 0:
                    category = segment["category"]
                    start_h, start_m = map(int, segment["start_time"].split(":"))
                    end_h, end_m = map(int, segment["end_time"].split(":"))
                    start, end = start_h * 60 + start_m, end_h * 60 + end_m
                    if end <= start:
                        end += 24 * 60
                    ob_intervals[(row["employee"], category, segment["segment_date"], bool(fixed))].append(
                        (start, end, prices["ob"].get(category))
                    )
    for (_employee, category, _day, is_extra), intervals in ob_intervals.items():
        boundaries = sorted({point for start, end, _rate in intervals for point in (start, end)})
        for start, end in zip(boundaries, boundaries[1:]):
            active = {rate for left, right, rate in intervals if left <= start and right >= end}
            if active:
                rate = next(iter(active)) if len(active) == 1 else None
                ob_costs[(category, rate, is_extra)] += (end - start) / 60
    lines = interval_lines
    report["interval_details"] = interval_details
    report["supplement_details"] = supplement_details
    for fixed in groups.values():
        lines.append({
            "label": f"Fast pris {fixed['date_from']} – {fixed['date_to']} (inkluderade poster)",
            "quantity": 1, "unit": "st", "rate": float(fixed["amount"]),
            "amount": float(fixed["amount"]) if report["fixed_complete"] else None,
        })
    if unassigned_hours:
        lines.append({
            "label": "Nya poster utanför bekräftat fast pris – kräver separat beslut",
            "quantity": unassigned_hours, "unit": "h", "rate": None, "amount": None,
        })
        report.setdefault("warnings", []).append(
            "Nya poster ingår inte automatiskt i fast pris och debiteras inte automatiskt som extra."
        )
    for (rate, is_extra), hours in sorted(grouped.items(), key=lambda item: (item[0][0] is None, item[0][0] or 0, item[0][1])):
        lines.append({
            "label": "Arbete" + (" – extra utöver fast pris" if is_extra else ""),
            "quantity": hours, "unit": "h", "rate": rate, "amount": money(hours, rate),
        })
    for (category, rate, is_extra), hours in sorted(
        ob_costs.items(), key=lambda item: (item[0][0], item[0][1] is None, item[0][1] or 0, item[0][2])
    ):
        if hours:
            lines.append({
                "label": CATEGORY_LABELS.get(category, "Övrig OB") + " – tillägg"
                + (" utöver fast pris" if is_extra else ""),
                "quantity": hours, "unit": "h", "rate": rate, "amount": money(hours, rate),
            })
    unknown_ob_billable = report["totals"]["without_ob_hours"] is None and any(
        row["entry_type"] == "ob" and not (row_prices(row).get("interval_terms") or row.get("time_facts")
                                         or row_prices(row).get("supplement_allocation")) and (
            row["sync_id"] not in allocations
            or allocations[row["sync_id"]]["members"][row["sync_id"]]["extra"]["ob"]
        ) for row in report["journal_rows"]
    )
    if unknown_ob_billable:
        lines.append({
            "label": "OB – fördelning ej verifierad", "quantity": None,
            "unit": "h", "rate": None, "amount": None,
        })
    for (travel_rate, is_extra), count in sorted(travel.items(), key=lambda item: (item[0][0] is None, item[0][0] or 0, item[0][1])):
        if count:
            lines.append({
                "label": "Resa" + (" – extra utöver fast pris" if is_extra else ""),
                "quantity": count, "unit": "st",
                "rate": travel_rate, "amount": money(count, travel_rate),
            })
    totals = report["totals"]
    totals["work_amount"] = sum(
        line["amount"] or 0 for line in lines
        if line["label"].startswith(("Arbete", "Fast pris"))
    )
    if any(line["amount"] is None for line in lines if line["label"].startswith(("Arbete", "Fast pris"))):
        totals["work_amount"] = None
    def component(prefix):
        selected = [line["amount"] for line in lines if line["label"].startswith(prefix)]
        return sum(selected) if all(value is not None for value in selected) else None
    totals["transport_amount"] = component("Resa")
    allocated_ob = any(row["entry_type"] == "ob" for row in report["journal_rows"]) and all(
        row["entry_type"] != "ob" or row_prices(row).get("supplement_allocation")
        for row in report["journal_rows"]
    )
    totals["ob_amount"] = (
        None if totals["without_ob_hours"] is None and not allocated_ob else component(("OB", "Kundtillägg"))
    )
    known_values = [Decimal(str(line["amount"])) for line in lines if line["amount"] is not None]
    unknown = any(line["amount"] is None for line in lines)
    known = float(sum(known_values)) if known_values or not unknown else None
    totals["known_subtotal"] = known
    totals["invoice_total"] = None if unknown else known
    report["cost_lines"] = lines
    report["recorded_trips"] = trips
    if unknown:
        report.setdefault("warnings", []).append(
            "Historiska kundpriser saknas eller är ofullständiga. "
            "Preliminärt belopp utan ej fastställda poster; lönepriser används inte."
        )
    return report


def write_customer_pdf(
    report, filepath, font, bold_font, translate, *, journal_only=False, activity_only=False,
):
    body_size = 9 if activity_only else 10
    body_leading = 11 if activity_only else 13
    styles = {
        "body": ParagraphStyle("CustomerBody", fontName=font, fontSize=body_size, leading=body_leading),
        "bold": ParagraphStyle("CustomerBold", fontName=bold_font, fontSize=body_size, leading=body_leading),
        "header": ParagraphStyle(
            "CustomerHeader", fontName=bold_font, fontSize=body_size,
            leading=11 if activity_only else 12,
            textColor=colors.HexColor("#152a38"),
        ),
        "title": ParagraphStyle(
            "CustomerTitle", fontName=bold_font,
            fontSize=16 if activity_only else 17, leading=19 if activity_only else 21,
        ),
        "summary_value": ParagraphStyle(
            "CustomerSummaryValue", fontName=bold_font, fontSize=11, leading=12,
        ),
        "summary_total": ParagraphStyle(
            "CustomerSummaryTotal", fontName=bold_font, fontSize=11, leading=12,
        ),
        "total": ParagraphStyle("CustomerTotal", fontName=bold_font, fontSize=12, leading=16),
    }
    for name in ("body", "header", "summary_value", "summary_total", "total"):
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
            Spacer(1, 6 if activity_only else 10),
        ]

    def table(
        data, widths, *, total=False, total_row=None, numeric=(), bold_rows=(), large_value_rows=(),
    ):
        emphasized_total_row = total_row if total_row is not None else (len(data) - 1 if total else None)

        def cell_style(index, column):
            is_total = index == emphasized_total_row
            if index == 0:
                style = "header"
            elif is_total:
                style = "summary_total" if activity_only else "total"
            elif index in bold_rows:
                style = "bold"
            else:
                style = "body"
            if column in numeric:
                if index in large_value_rows and not is_total:
                    style = "summary_value"
                style += "_number"
            return style

        rows = [
            [
                paragraph(value, cell_style(index, column))
                for column, value in enumerate(row)
            ]
            for index, row in enumerate(data)
        ]
        result = LongTable(
            rows, colWidths=widths, repeatRows=1, splitByRow=1, splitInRow=1, hAlign="LEFT",
        )
        padding = 3 if activity_only else 4
        result.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbe7ef")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f8")]),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor("#28475b")),
            ("TOPPADDING", (0, 0), (-1, -1), padding),
            ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
        ]))
        if emphasized_total_row is not None:
            result.setStyle(TableStyle([
                ("BACKGROUND", (0, emphasized_total_row), (-1, emphasized_total_row),
                 colors.HexColor("#dbe7ef")),
                ("LINEABOVE", (0, emphasized_total_row), (-1, emphasized_total_row),
                 0.8, colors.HexColor("#28475b")),
                ("NOSPLIT", (0, max(0, emphasized_total_row - 1)), (-1, emphasized_total_row)),
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

    def format_count(value):
        quantity = float(value or 0)
        return str(int(quantity)) if quantity.is_integer() else format_quantity(quantity)

    story = []
    totals = report["totals"]
    if not journal_only:
        if activity_only:
            story.extend(heading("Arbetstidsrapport"))
            ordinary_hours = sum(
                float(row.get("hours") or 0) for row in report["journal_rows"]
                if row["entry_type"] == "work"
            )
            ob_hours = sum(
                float(row.get("hours") or 0) for row in report["journal_rows"]
                if row["entry_type"] == "ob"
            )
            total_hours = float(totals["hours"])
            if abs(ordinary_hours + ob_hours - total_hours) > 0.000001:
                raise ValueError("Arbetstimmar och OB-pass stämmer inte med rapportens total.")
            trips = sum(
                float(row.get("transport") or 0) for row in report["journal_rows"]
                if row["entry_type"] in WORK_TYPES
            )
            summary = [
                ["Sammanfattning", "Värde"],
                ["Ordinarie arbete", format_quantity(ordinary_hours) + " h"],
                ["OB-pass, hela registrerade timmar", format_quantity(ob_hours) + " h"],
                ["Totalt arbete", format_quantity(total_hours) + " h"],
                ["Antal resor", format_count(trips) + " resor"],
            ]
            story.append(table(
                summary, [available * .66, available * .34], total_row=3, numeric=(1,),
                bold_rows=(1, 2, 3, 4), large_value_rows=(1, 2, 3, 4),
            ))
            story.append(Spacer(1, 7))
        else:
            story.extend(heading("Fakturaunderlag"))
            story.append(paragraph("Status: Pågående"))
        if not activity_only:
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
        if not activity_only:
            if report.get("fixed_agreements"):
                story.append(paragraph(
                    "Fast pris omfattar inkluderade poster. Endast uttryckligen markerade "
                    "extraposter debiteras utöver paketet."
                ))
                included = [["Datum / medarbetare", "Arbete", "OB", "Resa"]]
                for row in report["journal_rows"]:
                    fixed = report["fixed_allocations"].get(row["sync_id"])
                    if fixed:
                        extra = fixed["members"][row["sync_id"]]["extra"]
                        included.append([
                            f"{row['date']} / {row['employee']}",
                            *["Extra" if extra[key] else "Ingår" for key in ("base", "ob", "travel")],
                        ])
                story.append(table(included, [available * .55] + [available * .15] * 3))
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
            if report.get("interval_details"):
                detail_rows = [["Datum / medarbetare", "Intervall / arbetssätt", "Timmar", "Bas", "%", "Tillägg", "Totalt"]]
                for part in report["interval_details"]:
                    detail_rows.append([
                        part["date"] + " / " + part["employee"],
                        part["start"][11:16] + "–" + part["end"][11:16] + " / " + part["mode"]
                        + (" / schema (bekräftat)" if part.get("time_source") == "schedule" else ""),
                        format_quantity(float(part["hours"])), part["base"] or "Ej fastställt",
                        part["percent"] if part["percent"] is not None else "Ej fastställt",
                        part["premium_amount"] or "Ej fastställt", part["total"] or "Ej fastställt",
                    ])
                story.append(table(detail_rows, [available * x for x in (.25, .23, .08, .10, .07, .13, .14)]))
            if report.get("supplement_details"):
                names = {"ordinary": "Ordinarie, utan tillägg", "16-17": "16:00–17:00",
                         "17-19": "17:00–19:00", "19-22": "19:00–22:00",
                         "22-06": "22:00–06:00", "weekend": "Helg, uttryckligen +100%"}
                story.append(paragraph(
                    "Bekräftad kundfördelning av registrerade timmar. Intervallnamn är prisgrupper, "
                    "inte påhittade start- och sluttider. OB ingår i totalt antal timmar."))
                detail_rows = [["Datum / medarbetare", "Schema / prisgrupp", "Timmar", "Bas", "%", "Tillägg", "Totalt"]]
                for part in report["supplement_details"]:
                    detail_rows.append([
                        f"#{part['shift_id']} {part['date']} / {part['employee']}",
                        ("OB" if part["scheme"] == "ob" else "Övertid") + " / " + names[part["bucket"]],
                        format_quantity(float(part["hours"])), part["base"], part["percent"],
                        part["premium_amount"], part["total"],
                    ])
                story.append(table(detail_rows, [available * x for x in (.25, .23, .08, .10, .07, .13, .14)]))
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
        if not activity_only:
            story.append(PageBreak())
    if activity_only:
        story.append(paragraph("Arbetsjournal", "bold"))
    else:
        story.extend(heading("Arbetsjournal"))
    journal = [[
        "Datum", "Medarbetare", "Typ", "Faktisk tid", "Timmar", "Resor", "Arbetsbeskrivning",
    ]] if activity_only else [
        ["Datum", "Medarbetare", "Typ", "Timmar", "Tid / OB", "Arbetsbeskrivning"]
    ]

    def actual_time(row):
        facts = row.get("time_facts")
        if isinstance(facts, str):
            try:
                facts = json.loads(facts)
            except (TypeError, ValueError):
                return "Kontrollera sparad tid"
        facts = facts if isinstance(facts, dict) else {}
        start = facts.get("start") or row.get("start_time")
        end = facts.get("end") or row.get("end_time")
        if not start or not end:
            if activity_only and row["entry_type"] == "work":
                return "Ordinarie arbete"
            if activity_only:
                return "Ej angivet" if row["entry_type"] == "ob" else "–"
            return "Ej angivet"
        next_day = facts.get("next_day")
        if next_day is None:
            next_day = end <= start
        result = f"{start}–{end}" + (" (+1)" if next_day else "")
        breaks = facts.get("breaks") or []
        if breaks:
            result += "; rast " + ", ".join(
                f"{item['start']}–{item['end']}" for item in breaks
                if isinstance(item, dict) and item.get("start") and item.get("end")
            )
        return result

    for row in report["journal_rows"]:
        details = []
        if not activity_only:
            segments = report["journal_segments"].get(row["sync_id"], [])
            for segment in segments:
                label = CATEGORY_LABELS.get(segment["category"], "Dagtid" if not segment["premium_percent"] else "Övrig OB")
                details.append(
                    f"{segment['start_time']}–{segment['end_time']}: "
                    f"{format_quantity(segment['hours'])} h {label}"
                )
        description = (
            report["journal_translations"][row["sync_id"]]
            if row["sync_id"] in report.get("journal_translations", {})
            else translate(row["comment"] or "")
        )
        if activity_only:
            journal.append([
                row["date"], row["employee"],
                {"work": "Arbete", "ob": "OB-arbete"}.get(
                    row["entry_type"], TYPE_LABELS.get(row["entry_type"], "Övrig tid"),
                ),
                actual_time(row), format_quantity(row["hours"]),
                format_count(row.get("transport")) if float(row.get("transport") or 0) > 0 else "–",
                description,
            ])
        else:
            journal.append([
                row["date"], row["employee"], TYPE_LABELS.get(row["entry_type"], "Övrig tid"),
                format_quantity(row["hours"]), "\n".join(details), description,
            ])
    widths = (
        [74, 95, 78, 82, 52, 52, available - 433]
        if activity_only else [65, 115, 100, 55, 140, available - 475]
    )
    story.append(table(journal, widths, numeric=(4, 5) if activity_only else (3,)))
    journal_work_hours = sum(float(row["hours"]) for row in report["journal_rows"] if row["entry_type"] in WORK_TYPES)
    if abs(journal_work_hours - totals["hours"]) > 0.000001:
        raise ValueError("Arbetsjournalens timmar stämmer inte med fakturaunderlaget.")
    if journal_only:
        story.extend([
            Spacer(1, 8),
            highlight("Totalt antal arbetade timmar", format_quantity(journal_work_hours) + " h"),
        ])
    if not activity_only:
        absence = defaultdict(float)
        for row in report["journal_rows"]:
            if row["entry_type"] not in WORK_TYPES:
                absence[TYPE_LABELS.get(row["entry_type"], "Övrig tid")] += float(row["hours"])
        if absence:
            story.append(paragraph(
                "; ".join(f"{label}: {format_quantity(hours)} h" for label, hours in sorted(absence.items()))
            ))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font, 9)
        canvas.drawString(36, 17, f"{report['company']} | {report['date_from']} – {report['date_to']}")
        canvas.drawRightString(width - 36, 17, f"Sida {doc.page}")
        canvas.restoreState()

    document = SimpleDocTemplate(
        os.fspath(filepath) if isinstance(filepath, os.PathLike) else filepath,
        pagesize=landscape(A4), leftMargin=36, rightMargin=36,
        topMargin=30, bottomMargin=32,
        title="Arbetstidsrapport" if activity_only else "Fakturaunderlag och arbetsjournal",
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)
