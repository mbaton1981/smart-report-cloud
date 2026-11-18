"""Read product descriptions without deriving VAT or changing saved allocations."""
import re

from expense_documents import invoice_layout_rows, parse_invoice_pages, parse_decimal


def single_allocation_content(pages, document):
    allocations = document.get("allocations") or []
    if (len(allocations) != 1 or parse_decimal(document.get("gross_amount")) is None
            or parse_decimal(allocations[0].get("gross_amount")) != parse_decimal(document.get("gross_amount"))):
        return ""
    fields = parse_invoice_pages(pages)
    if fields.get("invoice_number") and fields["invoice_number"] != document.get("invoice_number"):
        raise ValueError("Номер оригинала не совпадает с фактурой.")
    if fields.get("currency_code") not in (None, "", "UNKNOWN", document.get("currency_code")):
        raise ValueError("Валюта оригинала не совпадает с фактурой.")
    lines = []
    for page in pages:
        active = current_item = False
        left, right = 0, float("inf")
        for row in invoice_layout_rows(page.get("elements") or []):
            text = " ".join(cell["text"] for cell in row)
            if re.search(r"Nettosumma\s+artiklar|Artiklar\s+ex\.?\s*moms|Totalsumma|Fakturabelopp|Summa\s+order|"
                         r"Totalt\s+exkl|Moms\s*%|Bank\s*:|SWIFT|F-skatt|BUTIKER\s*:|Telephone|Telefon\s*:|"
                         r"Org\.?\s*(?:nr|no)\s*:", text, re.I) and not re.search("Beskrivning|Benämning|Description", text, re.I):
                active = current_item = False
                continue
            for cell in row:
                if re.fullmatch(r"Antal", cell["text"], re.I):
                    right = cell["x0"] - 4
                if re.search(r"Produktbeskrivning|Artikelbeskrivning|Benämning|Beskrivning|Description", cell["text"], re.I):
                    left = cell["x0"] - 4
                    active = True
                    current_item = False
            if re.search(r"\bArtikel\b", text, re.I) and re.search(r"\bBelopp\b", text, re.I):
                active = True
                current_item = False
                left = next((cell["x0"] - 4 for cell in row if re.fullmatch("Artikel", cell["text"], re.I)), left)
                continue
            if not active or re.search(r"Produktbeskrivning|Artikelbeskrivning|Benämning|Beskrivning|Description", text, re.I):
                continue
            article = any(re.fullmatch(r"(?:\d{4,}|[A-Z]\d{4,})", cell["text"]) for cell in row)
            has_money = any(re.search(r"\d[,.]\d{2}(?:\D|$)", cell["text"]) for cell in row)
            if article and has_money:
                current_item = True
            if not current_item:
                continue
            description = " ".join(cell["text"] for cell in row if left <= cell["x0"] < right
                                   and re.search(r"[A-Za-zÅÄÖåäö]{3}", cell["text"])
                                   and not re.fullmatch(r"\d+(?:[,.]\d+)?\s*(?:ST|PKT|FRP|RLE|SET|PÅS|PAR|BRK|M2|LPM)", cell["text"], re.I))
            if description and not re.search(r"Bankgiro|Plusgiro|IBAN|Org\.?\s*nr|OCR|betalning|Totalsumma|^OBS\b", description, re.I):
                lines.append(description)
    return "\n".join(dict.fromkeys(lines))
