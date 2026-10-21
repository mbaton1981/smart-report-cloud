"""Expense suggestions independent of invoice validation and settlement fields."""
from __future__ import annotations

import re
from decimal import Decimal

from expense_documents import _date_value, decimal_text, non_invoice_document_type, parse_invoice_money


def propose_object_allocations(fields, objects):
    """Build a review-only proposal from verified order/item evidence, never save it."""
    from decimal import ROUND_DOWN
    orders = fields.get("order_proposals") or []
    result = {"parts": [], "warnings": [], "errors": [], "rounding": "0.00", "total": None}
    result["errors"].extend(fields.get("order_proposal_warnings") or [])
    if not orders:
        result["errors"].append("Маркировки и суммы заказов не распознаны. Доступно ручное распределение.")
        return result
    grouped, seen = {}, set()
    for order in orders:
        marker = str(order.get("project_code") or order.get("original_marker") or "").strip()
        identifier = order.get("order_id")
        net, vat = (parse_invoice_money(order.get(key)) for key in ("net_amount", "vat_amount"))
        if (not marker or not identifier or identifier in seen or order.get("ambiguous")
                or net is None or vat is None or not net.is_finite() or not vat.is_finite()
                or net < 0 or vat < 0):
            result["errors"].append("Есть неоднозначный заказ, маркировка или сумма. Автоприменение запрещено.")
            continue
        seen.add(identifier)
        items = order.get("vat_items") or []
        values = [[parse_invoice_money(item.get(key)) for item in items]
                  for key in ("net_amount", "vat_amount")]
        if (not items or any(value is None or not value.is_finite() for row in values for value in row)
                or sum(values[0], Decimal(0)) != net or sum(values[1], Decimal(0)) != vat):
            result["errors"].append(f"{marker}: позиции не подтверждают сумму и НДС заказа.")
            continue
        key = marker.casefold()
        part = grouped.setdefault(key, {"marker": marker, "net": Decimal(0), "vat": Decimal(0),
                                         "pages": set(), "orders": []})
        part["net"] += net
        part["vat"] += vat
        part["orders"].append(identifier)
        part["pages"].update(int(item["page"]) for item in items)
    net, vat = (parse_invoice_money(fields.get(key)) for key in ("net_amount", "vat_amount"))
    rounding = parse_invoice_money(fields.get("rounding_amount")) if fields.get("rounding_amount") not in (None, "") else Decimal(0)
    if (net is None or vat is None or rounding is None
            or any(not value.is_finite() for value in (net, vat, rounding))):
        result["errors"].append("Итог, НДС или округление документа не определены.")
        return result
    gross = net + vat
    total = gross + rounding
    if (not grouped or gross <= 0 or total <= 0 or abs(rounding) > Decimal("1.00")
            or sum((p["net"] for p in grouped.values()), Decimal(0)) != net
            or sum((p["vat"] for p in grouped.values()), Decimal(0)) != vat):
        result["errors"].append("Суммы блоков не сходятся с итогом документа или округление требует ручной проверки.")
        return result
    # Allocate only the stated rounding, in cents, using largest remainders.
    count = int(abs(rounding) * 100)
    shares = {key: Decimal(count) * (part["net"] + part["vat"]) / gross for key, part in grouped.items()}
    cents = {key: int(value.to_integral_value(rounding=ROUND_DOWN)) for key, value in shares.items()}
    for key in sorted(shares, key=lambda k: (-(shares[k] - cents[k]), k))[:count - sum(cents.values())]:
        cents[key] += 1
    for key, part in grouped.items():
        matches = [obj for obj in objects if str(obj.get("markning") or "").strip().casefold() == key]
        selected = matches[0] if len(matches) == 1 else None
        warning = ("Неизвестная маркировка — выберите объект вручную" if not matches else
                   "Маркировка соответствует нескольким объектам — выберите вручную" if len(matches) > 1 else
                   "Объект неактивен" if not selected.get("is_active", 1) else "")
        adjustment = Decimal(cents[key]) / 100 * (-1 if rounding < 0 else 1)
        result["parts"].append({
            "marker": part["marker"], "object_id": selected["id"] if selected else None,
            "object_name": selected["name"] if selected else "", "warning": warning,
            "net_amount": decimal_text(part["net"]), "vat_amount": decimal_text(part["vat"]),
            "before_rounding": decimal_text(part["net"] + part["vat"]),
            "rounding": decimal_text(adjustment),
            "gross_amount": decimal_text(part["net"] + part["vat"] + adjustment),
            "pages": sorted(part["pages"]), "orders": part["orders"],
        })
        if warning:
            result["warnings"].append(f"{part['marker']}: {warning}")
    result.update(rounding=decimal_text(rounding), total=decimal_text(total))
    return result


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
