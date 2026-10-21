"""Expense suggestions independent of invoice validation and settlement fields."""
from __future__ import annotations

import re
from decimal import Decimal

from expense_documents import _date_value, decimal_text, non_invoice_document_type, parse_invoice_money


def extract_cost_fields(pages, fields):
    text = "\n\n".join(str(page.get("text") or "") for page in pages)
    tax = non_invoice_document_type(text) == "tax_decision"
    result = {key: fields.get(key) for key in (
        "supplier_name", "document_date", "invoice_number", "payment_reference",
        "currency_code", "gross_amount", "net_amount", "vat_amount")}
    result["field_sources"] = dict(fields.get("field_sources") or {})
    result["amount_candidates"] = []
    if not tax:
        return result

    result.update(supplier_name=None, document_date=None, invoice_number=None,
                  payment_reference=None, gross_amount=None, net_amount=None, vat_amount=None)
    result["field_sources"] = {}
    references = [item for item in (fields.get("field_sources") or {}).get("payment_reference", [])
                  if re.fullmatch(r"\d{5,30}", item.get("text", ""))
                  and re.search(r"OCR", item.get("caption", ""), re.IGNORECASE)]
    if len({item["text"] for item in references}) == 1:
        result["payment_reference"] = references[0]["text"]
        result["field_sources"]["payment_reference"] = references
    evidence = []
    money = r"(-?(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[,.]\d{2})?)"
    for page_index, page in enumerate(pages):
        lines = [line.strip() for line in str(page.get("text") or "").splitlines() if line.strip()]
        for index, line in enumerate(lines):
            source = {"page": page.get("page", page_index + 1),
                      "attachment_index": page.get("attachment_index", 0), "text": line,
                      "kind": "stated_tax_decision"}
            compact = re.sub(r"\s+", "", line).casefold()
            total_label = re.match(r"^(?:totaltattbetala|summaattbetala)", compact)
            tax_line = re.match(r"^trängselskatt\s+för\b", line, re.IGNORECASE)
            # Monthly tax line or a labelled total: never prose about a possible late fee.
            amount = None
            if (total_label or tax_line) and not re.search(
                    r"tilläggsavgift|försen|registrerade\s+passage", line, re.IGNORECASE):
                match = re.search(r"(?<![\d-])" + money + r"\s*(?:SEK|kr(?:onor)?)?\s*$", line, re.IGNORECASE)
                if match:
                    # A bare year after "för september 2026" is not a monetary value.
                    tail = line[:match.start()].strip()
                    if total_label or re.search(r"\b\d{4}\s*$", tail):
                        amount = parse_invoice_money(match.group(1))
                if amount is None and total_label and index + 1 < len(lines):
                    next_line = lines[index + 1]
                    match = re.fullmatch(money + r"\s*(?:SEK|kr(?:onor)?)?", next_line, re.IGNORECASE)
                    if match:
                        amount = parse_invoice_money(match.group(1))
                        source["text"] += "\n" + next_line
            if amount is not None and amount.is_finite() and amount > 0:
                evidence.append({"amount": decimal_text(amount), **source})
            if not result["document_date"] and re.fullmatch(r"(?:Beslutsdatum|Datum)\s*:?", line, re.IGNORECASE):
                value = _date_value(lines[index + 1]) if index + 1 < len(lines) else None
                if value:
                    result["document_date"] = value
                    result["field_sources"]["document_date"] = [source]
            if not result["payment_reference"] and re.fullmatch(r"OCR[- ]nummer\s*:?", line, re.IGNORECASE):
                if index + 1 < len(lines) and re.fullmatch(r"\d{5,30}", lines[index + 1]):
                    result["payment_reference"] = lines[index + 1]
                    result["field_sources"]["payment_reference"] = [source]

    if re.search(r"\bTransportstyrelsen(?:s)?\b", text, re.IGNORECASE):
        result["supplier_name"] = "Transportstyrelsen"
        result["field_sources"]["supplier_name"] = [
            {"kind": "stated_issuer", "text": "Transportstyrelsen"}]
    if result.get("currency_code") in (None, "", "UNKNOWN") and re.search(r"\b(?:kr|kronor|SEK)\b", text, re.IGNORECASE):
        result["currency_code"] = "SEK"
    amounts = sorted({item["amount"] for item in evidence}, key=Decimal)
    result["amount_candidates"] = [
        {"amount": amount, "evidence": [item for item in evidence if item["amount"] == amount]}
        for amount in amounts]
    if len(amounts) == 1:
        result["gross_amount"] = amounts[0]
        result["field_sources"]["gross_amount"] = evidence
    else:
        result["field_sources"].pop("gross_amount", None)
    return result
