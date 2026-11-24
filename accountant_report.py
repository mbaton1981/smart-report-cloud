"""Build an invoice handoff from the selected work and supplier allocations."""
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from xml.sax.saxutils import escape

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, TableStyle

from customer_billing import priced_report
from customer_pricing import row_prices
from customer_report import REPORT_ISSUER_NAME
from expense_documents import parse_decimal
from object_package_report import _prepare_sources, expense_document_key
from object_package_swedish import swedish_category
from payroll_auto import PREMIUM
from swedish_translation import CYRILLIC


TIME_LABELS = {
    "day": "Ordinarie arbete",
    "ob1": "OB 1",
    "ob2": "OB 2",
    "ob3": "OB 3 natt",
    "helg": "OB helg",
    "ot_a": "Övertid A",
    "ot_b": "Övertid B",
    "ot_c": "Övertid C",
    "ot_d": "Övertid D",
    "unknown": "Arbetstid att kontrollera",
}
CENT = Decimal("0.01")
ZERO = Decimal(0)


def decimal(value):
    return Decimal(str(value))


def money(value, currency="SEK"):
    if value is None:
        return "Att kontrollera"
    text = f"{decimal(value):,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} {currency}"


def quantity(value):
    return f"{decimal(value):.4f}".rstrip("0").rstrip(".").replace(".", ",")


def work_parts(report, row):
    parts = report.get("accountant_payroll", {}).get(row["sync_id"], {}).get("segments", [])
    if parts and abs(sum(part["hours"] for part in parts) - float(row["hours"])) < .00001:
        return parts
    return [{"hours": row["hours"], "category": "unknown"}]


def line_details(report, row):
    lines = []
    for part in work_parts(report, row):
        interval = ""
        if part.get("start_time") and part.get("end_time"):
            interval = f"{part['start_time']}-{part['end_time']} "
        lines.append(f"{interval}{TIME_LABELS[part['category']]}: {quantity(part['hours'])} h")
    return "\n".join(lines)


def summarize_accountant(report, entries, *, include_trips=True, rates=None, use_saved_terms=True):
    rates = rates or {}
    expenses, shifts = [], []
    for entry in entries:
        (shifts if entry["member"]["kind"] == "shift" else expenses).append(entry)
    work = defaultdict(Decimal)
    materials = defaultdict(lambda: {"known": ZERO, "missing": 0})
    stored_terms = []
    for entry in shifts:
        row = entry["row"]
        try:
            prices = row_prices(row)
        except (TypeError, ValueError, KeyError):
            prices = {"base": None, "travel": None}
        if prices.get("fixed") or (use_saved_terms and any(
                prices.get(key) for key in ("supplement_allocation", "interval_terms", "ob"))):
            stored_terms.append(row)
            continue
        base = rates.get("day", prices["base"])
        for part in work_parts(report, row):
            category = part["category"]
            rate = rates.get(category)
            if rate is None and base is not None and category != "unknown":
                rate = decimal(base) * (1 + decimal(PREMIUM[category]))
            if rate is not None:
                rate = decimal(rate).quantize(CENT, rounding=ROUND_HALF_UP)
            work[TIME_LABELS[category], "h", rate] += decimal(part["hours"])
        if include_trips and row.get("transport"):
            travel = rates.get("trip", prices["travel"])
            work["Resor", "st", decimal(travel) if travel is not None else None] += decimal(row["transport"])
    lines = [
        (label, count, unit, rate, (count * rate).quantize(CENT, rounding=ROUND_HALF_UP)
         if rate is not None else None)
        for (label, unit, rate), count in work.items()
    ]
    if stored_terms:
        try:
            stored = priced_report(report, stored_terms)
        except (ValueError, KeyError, TypeError):
            lines.append(("Sparade kundvillkor", None, "", None, None))
        else:
            for line in stored["cost_lines"]:
                count = decimal(line["quantity"])
                rate = decimal(line["rate"]) if line["rate"] is not None else None
                amount = decimal(line["amount"]) if line["amount"] is not None else None
                if line["label"].startswith("Resa"):
                    if not include_trips:
                        continue
                    if "trip" in rates:
                        rate = rates["trip"]
                        amount = (count * rate).quantize(CENT, rounding=ROUND_HALF_UP)
                lines.append((line["label"], count, line["unit"], rate, amount))
    hours = sum((decimal(entry["row"]["hours"]) for entry in shifts), ZERO)
    trips = sum((decimal(entry["row"].get("transport") or 0) for entry in shifts), ZERO) if include_trips else ZERO
    for entry in expenses:
        allocation = entry.get("display_allocation", entry["allocation"])
        key = (swedish_category(entry.get("category") or "Не классифицировано"), entry["currency"])
        net = parse_decimal(allocation.get("net_amount"))
        if net is None:
            materials[key]["missing"] += 1
        else:
            materials[key]["known"] += net
    known = sum((line[-1] for line in lines if line[-1] is not None), ZERO)
    known += sum((item["known"] for (_, currency), item in materials.items() if currency == "SEK"), ZERO)
    complete = all(line[-1] is not None for line in lines) and all(
        currency == "SEK" and not item["missing"] for (_, currency), item in materials.items()
    )
    return dict(shifts=shifts, expenses=expenses, lines=lines, materials=materials, hours=hours, trips=trips,
                total=known if complete else None, known=known, stored_terms=bool(stored_terms))


def write_accountant_report(filepath, obj, entries, report, cutoff, *, font, bold_font,
                            include_trips=True, include_journal=True, rates=None, use_saved_terms=True,
                            copy_documents=(), source_paths=None, source_errors=None):
    """Render the handoff and merge selected supplier copies after its own pages."""
    filepath = Path(filepath)
    summary = summarize_accountant(report, entries, include_trips=include_trips,
                                   rates=rates, use_saved_terms=use_saved_terms)
    copies = _prepare_sources(copy_documents, source_paths or {}, source_errors or {}, filepath.parent)
    appendix_numbers = {expense_document_key(item["document"]): i for i, item in enumerate(copies, 1)}
    style = ParagraphStyle("AccountantBody", fontName=font, fontSize=9, leading=12,
                           textColor=colors.HexColor("#173346"))
    title = ParagraphStyle("AccountantTitle", parent=style, fontName=bold_font, fontSize=20, leading=25)
    heading = ParagraphStyle("AccountantHeading", parent=style, fontName=bold_font, fontSize=12, leading=16)
    cell_header = ParagraphStyle("AccountantHeader", parent=style, fontName=bold_font)

    def paragraph(value, selected_style=style):
        return Paragraph(escape(str(value)).replace("\n", "<br/>"), selected_style)

    def table(rows, widths):
        result = LongTable([[paragraph(value, cell_header if i == 0 else style) for value in row]
                            for i, row in enumerate(rows)], colWidths=widths, repeatRows=1, hAlign="LEFT")
        result.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dceaf2")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f6f8")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), .6, colors.HexColor("#558197")),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        return result

    scope = " · ".join(str(x) for x in (obj.get("markning"), obj["name"]) if x)
    headline = f"Arbetade timmar: {quantity(summary['hours'])} h"
    if include_trips:
        headline += f"  ·  Resor: {quantity(summary['trips'])}"
    story = [paragraph(REPORT_ISSUER_NAME, heading), paragraph("Faktureringsunderlag", title),
             paragraph(scope, heading), paragraph(f"Till och med {cutoff} · Markerade poster"),
             Spacer(1, 11), paragraph(headline, heading), Spacer(1, 6)]
    total = summary["total"]
    story.append(paragraph(
        f"Totalt exkl. moms: {money(total)}" if total is not None
        else f"Totalt exkl. moms: att kontrollera · Känd del {money(summary['known'])}", heading,
    ))
    story.append(paragraph("Kontrollera kundpriserna före fakturering."))
    if summary["stored_terms"]:
        story.append(paragraph("Sparade kundvillkor ingår i beräkningen. Dagboken visar tid enligt lönereglerna."))
    if summary["lines"]:
        rows = [["Arbete och resor" if include_trips else "Arbete", "Antal", "Pris exkl. moms", "Belopp exkl. moms"]]
        rows += [(label, f"{quantity(count)} {unit}" if count is not None else "—",
                  money(rate), money(amount)) for label, count, unit, rate, amount in summary["lines"]]
        story.extend([Spacer(1, 9), table(rows, [213, 70, 100, 120])])
    if summary["materials"]:
        rows = [["Leverantörskostnader", "Exkl. moms", "Underlag"]]
        for (label, currency), item in sorted(summary["materials"].items()):
            amount = money(item["known"], currency)
            if item["missing"]:
                amount = f"{amount} (känd del)" if item["known"] else "Att kontrollera"
            rows.append((label, amount, "Projektets andelar"))
        story.extend([Spacer(1, 11), table(rows, [213, 145, 145])])

    if summary["expenses"]:
        story.extend([Spacer(1, 13), paragraph("Leverantörsdokument och fördelade kostnader", heading)])
        rows = [["Datum", "Leverantör / dokument", "Exkl. moms", "Inkl. moms", "Bilaga"]]
        for entry in summary["expenses"]:
            part = entry.get("display_allocation", entry["allocation"])
            number = appendix_numbers.get(expense_document_key(entry["document"]))
            rows.append([entry["date"], entry["label"], money(parse_decimal(part.get("net_amount")), entry["currency"]),
                         money(parse_decimal(part.get("gross_amount")), entry["currency"]), number or "—"])
        story.append(table(rows, [63, 175, 100, 100, 65]))
    if include_journal and summary["shifts"]:
        story.extend([PageBreak(), paragraph("Dagbok", title), paragraph(scope, heading), Spacer(1, 8)])
        rows = [["Datum", "Medarbetare", "Arbetsbeskrivning / tid", "Timmar", *(["Resor"] if include_trips else [])]]
        translations = report.get("journal_translations", {})
        for entry in summary["shifts"]:
            row = entry["row"]
            description = (translations.get(row["sync_id"]) or "").strip()
            if not description or CYRILLIC.search(description):
                description = ("Svensk översättning saknas" if (row.get("comment") or "").strip()
                               else "Arbetsbeskrivning saknas")
            details = f"{description}\n{line_details(report, row)}"
            rows.append([row["date"], row["employee"], details, quantity(row["hours"]),
                         *([quantity(row.get("transport") or 0)] if include_trips else [])])
        story.append(table(rows, [63, 104, 225, 61, 50] if include_trips else [63, 104, 275, 61]))
    if copies:
        story.extend([PageBreak(), paragraph("Bilagor", title),
                      paragraph("Kopior av leverantörsdokument. Beloppen i originalen kan avse flera projekt."),
                      Spacer(1, 7)])
        for index, item in enumerate(copies, 1):
            doc = item["document"]
            label = " · ".join(str(x) for x in (doc.get("supplier_name"), doc.get("invoice_number")) if x)
            count = item["page_count"]
            note = "1 sida" if count == 1 else f"{count} sidor" if count else "Kopia saknas"
            if item["error"] and item["page_count"]:
                note += " (ofullständig kopia)"
            story.append(paragraph(f"Bilaga {index} · {label or doc['id']} · {note}"))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.drawRightString(A4[0] - 42, 20, f"Sida {document.page}")
        canvas.restoreState()

    main = filepath.with_name("accountant-main.pdf") if copies else filepath
    SimpleDocTemplate(str(main), pagesize=A4, leftMargin=40, rightMargin=40,
                      topMargin=35, bottomMargin=35, title="Faktureringsunderlag").build(
                          story, onFirstPage=footer, onLaterPages=footer)
    if copies:
        writer = PdfWriter()
        writer.append(PdfReader(str(main)), import_outline=False)
        for index, item in enumerate(copies, 1):
            for part_index, path in enumerate(item["source_paths"], 1):
                writer.append(str(path), outline_item=f"Bilaga {index}" if part_index == 1 else None,
                              import_outline=False)
        with filepath.open("wb") as output:
            writer.write(output)
        writer.close()
