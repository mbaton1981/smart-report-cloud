"""Accountant handoff: time and trips as quantities, supplier costs as money."""
from collections import defaultdict
from decimal import Decimal
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

from customer_report import CATEGORY_LABELS, REPORT_ISSUER_NAME
from expense_documents import parse_decimal
from object_package_swedish import swedish_category


def write_accountant_report(filepath, obj, entries, report, cutoff, *, font, bold_font):
    """Never derive or print customer/payroll rates and amounts for time or trips."""
    style = ParagraphStyle("AccountantBody", fontName=font, fontSize=8, leading=11)
    title = ParagraphStyle("AccountantTitle", parent=style, fontName=bold_font, fontSize=15, leading=19)
    heading = ParagraphStyle("AccountantHeading", parent=style, fontName=bold_font, fontSize=10, leading=14)

    def paragraph(value, selected_style=style):
        return Paragraph(escape(str(value)).replace("\n", "<br/>"), selected_style)

    def table(rows, widths):
        result = Table([[paragraph(value) for value in row] for row in rows],
                       colWidths=widths, repeatRows=1, hAlign="LEFT")
        result.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dce8ef")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), .3, colors.HexColor("#b9cbd6")),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        return result

    def quantity(value):
        return f"{Decimal(str(value or 0)):g}".replace(".", ",")

    def money(value, currency):
        parsed = parse_decimal(value)
        return "Ej angivet" if parsed is None else f"{parsed:,.2f} {currency}".replace(",", " ").replace(".", ",")

    shifts = [entry for entry in entries if entry["member"]["kind"] == "shift"]
    expenses = [entry for entry in entries if entry["member"]["kind"] == "expense"]
    hours = sum((Decimal(str(entry["row"]["hours"])) for entry in shifts), Decimal(0))
    trips = sum((Decimal(str(entry["row"].get("transport") or 0)) for entry in shifts), Decimal(0))
    ob_hours = sum((Decimal(str(entry["row"]["hours"])) for entry in shifts if entry["row"]["entry_type"] == "ob"), Decimal(0))
    story = [paragraph(REPORT_ISSUER_NAME, heading), paragraph("Underlag till redovisningen", title),
             paragraph(obj["name"], heading),
             paragraph(f"Till och med {cutoff}. Endast markerade poster ingår."),
             paragraph("Arbete och resor redovisas som antal. Priser och OB-ersättning fastställs av redovisningen."),
             Spacer(1, 10), paragraph(f"Arbetade timmar: {quantity(hours)} h · OB-pass: {quantity(ob_hours)} h · Resor: {quantity(trips)}", heading)]
    if shifts:
        rows = [["Datum", "Medarbetare", "Arbete / OB", "Timmar", "Resor"]]
        for entry in shifts:
            row = entry["row"]
            details = "Ordinarie arbete"
            if row["entry_type"] == "ob":
                parts = report.get("journal_segments", {}).get(row["sync_id"], [])
                details = "\n".join(
                    f"{part.get('start_time') or ''}–{part.get('end_time') or ''} "
                    f"{CATEGORY_LABELS.get(part['category'], part['category'])}: {quantity(part['hours'])} h"
                    for part in parts
                ) or "OB-arbete; kontrollera tidsfördelningen"
            rows.append([row["date"], row["employee"], details, quantity(row["hours"]), quantity(row.get("transport"))])
        story.extend([table(rows, [65, 115, 205, 65, 65]), Spacer(1, 14)])
    story.append(paragraph("Leverantörsdokument och fördelade kostnader", heading))
    totals = defaultdict(lambda: {"net": Decimal(0), "gross": Decimal(0), "net_missing": 0, "gross_missing": 0,
                                  "net_count": 0, "gross_count": 0})
    rows = [["Datum", "Leverantör / dokument", "Kategori", "Exkl. moms", "Inkl. moms"]]
    for entry in expenses:
        allocation = entry.get("display_allocation", entry["allocation"])
        currency = entry["currency"]
        for field, key in (("net_amount", "net"), ("gross_amount", "gross")):
            value = parse_decimal(allocation.get(field))
            if value is None:
                totals[currency][key + "_missing"] += 1
            else:
                totals[currency][key] += value
                totals[currency][key + "_count"] += 1
        rows.append([entry["date"], entry["label"], swedish_category(entry.get("category") or "Не классифицировано"),
                     money(allocation.get("net_amount"), currency), money(allocation.get("gross_amount"), currency)])
    if expenses:
        story.append(table(rows, [65, 150, 100, 100, 100]))
        for currency, values in sorted(totals.items()):
            note = " (känd del; nettobelopp saknas för vissa andelar)" if values["net_missing"] else ""
            story.append(paragraph(f"Leverantörskostnader exkl. moms: {money(values['net'] if values['net_count'] else None, currency)}{note}", heading))
            gross_note = " (känd del)" if values["gross_missing"] else ""
            story.append(paragraph(f"Leverantörskostnader inkl. moms: {money(values['gross'] if values['gross_count'] else None, currency)}{gross_note}"))
    else:
        story.append(paragraph("Inga leverantörsdokument är markerade."))
    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.drawRightString(A4[0] - 40, 20, f"Sida {document.page}")
        canvas.restoreState()

    SimpleDocTemplate(str(filepath), pagesize=A4, leftMargin=40, rightMargin=40,
                      topMargin=35, bottomMargin=35, title="Underlag till redovisningen").build(
                          story, onFirstPage=footer, onLaterPages=footer)
