from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


MIGRATION_ID = "supplier_documents_v1"
CENT = Decimal("0.01")
VALID_DOCUMENT_TYPES = {"invoice", "receipt", "credit_note", "transfer_receipt", "manual"}
VALID_REVIEW_STATUSES = {"draft", "requires_review", "confirmed"}
VALID_PAYMENT_STATUSES = {"unknown", "unpaid", "partially_paid", "paid"}
DOCUMENT_SOURCES = {
    "manual_upload": {"available": True, "label": "Ручная загрузка"},
    "email": {"available": False, "label": "E-post", "reason": "официальный коннектор и авторизация не настроены"},
    "kivra": {"available": False, "label": "Kivra", "reason": "официальный коннектор и авторизация не настроены"},
}


class DocumentExtractionError(RuntimeError):
    pass


class DuplicateDocumentError(ValueError):
    def __init__(self, matches: list[dict[str, Any]]):
        self.matches = matches
        super().__init__("Найдены возможные дубликаты документа.")


@dataclass(frozen=True)
class ExtractionResult:
    text: str
    method: str
    confidence: float | None
    pages: tuple[str, ...] = ()
    page_elements: tuple[tuple[dict[str, Any], ...], ...] = ()


def parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip().replace("\u00a0", " ")
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = re.sub(r"[^0-9,.'+\-]", "", text)
    if not re.search(r"\d", text):
        return None
    if "," in text and "." in text:
        decimal_separator = "," if text.rfind(",") > text.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        text = text.replace(thousands_separator, "").replace(decimal_separator, ".")
    elif "," in text:
        tail = text.rsplit(",", 1)[1]
        text = text.replace(",", ".") if len(tail) in (1, 2) else text.replace(",", "")
    elif text.count(".") > 1:
        parts = text.split(".")
        text = "".join(parts[:-1]) + "." + parts[-1] if len(parts[-1]) in (1, 2) else "".join(parts)
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    return -amount if negative else amount


def decimal_text(value: Decimal | str | int | float | None) -> str | None:
    amount = parse_decimal(value)
    if amount is None:
        return None
    return format(amount.quantize(CENT, rounding=ROUND_HALF_UP), ".2f")


def _label_value(lines: list[str], labels: str) -> str | None:
    pattern = re.compile(rf"^\s*(?:{labels})(?:\s*[:#№]\s*|\s+)(.*?)\s*$", re.IGNORECASE)
    for line in lines:
        match = pattern.match(line)
        if match and match.group(1):
            value = match.group(1).strip(" :#")
            if re.match(r"^(?:Fakturans|Faktura(?:nummer|datum)?|företagsinfo|Leverantör|Moms|Netto|Summa|Valuta)\b", value, re.IGNORECASE):
                continue
            return value or None
    return None


def _date_value(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", value)
    if match:
        year, month, day = map(int, match.groups())
    else:
        match = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", value)
        if not match:
            return None
        day, month, year = map(int, match.groups())
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None


def non_invoice_document_type(text: str) -> str | None:
    headings = [line.strip() for line in (text or "").splitlines() if line.strip() and not line.startswith("---")]
    header = "\n".join(headings[:15])
    if re.search(r"^\s*Beslut\s+om\s+trängselskatt\s*$", header, re.IGNORECASE | re.MULTILINE):
        return "tax_decision"
    if re.search(r"^\s*ORDERBEKRÄFTELSE\s*$", header, re.IGNORECASE | re.MULTILINE):
        return "order_confirmation"
    return None


def parse_document_text(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in (text or "").replace("\r", "\n").split("\n") if line.strip()]
    full_text = "\n".join(lines)
    lower_text = full_text.casefold()
    non_invoice_type = non_invoice_document_type(full_text)
    is_transfer_receipt = bool(re.search(
        r"payment\s+(?:confirmation|receipt|proof)|transfer\s+(?:confirmation|receipt|proof|date|amount)|"
        r"transaction\s+(?:confirmation|receipt|date|amount)|betalningsbekräftelse|betalningsbekraftelse|"
        r"överföringsbekräftelse|overforingsbekraftelse|banköverföring|bankoverforing|bank\s+transfer",
        lower_text,
    ))

    supplier_name = _label_value(lines, r"Leverantör|Leverantor|Supplier|Seller|Säljare|Saljare")
    if non_invoice_type:
        supplier_name = None
    if is_transfer_receipt and supplier_name and re.sub(r"[^a-z0-9]+", "", supplier_name.casefold()) in {"lunar", "lunarbank"}:
        supplier_name = None
    supplier_org_number = _label_value(lines, r"Leverantörens\s+Org\.?\s*nr|Leverantorens\s+Org\.?\s*nr|Org\.?\s*nr|Organisationsnummer")
    supplier_vat_number = _label_value(lines, r"VAT(?:\s*(?:no\.?|number|nr))?|Momsreg\.?\s*nr")
    if non_invoice_type:
        supplier_org_number = None
        supplier_vat_number = None
    invoice_number = _label_value(lines, r"Fakturanummer|Faktura\s*nr|Invoice\s*(?:number|no\.?)")
    payment_reference = _label_value(lines, r"OCR(?:-nummer)?|Betalningsreferens|Payment\s*reference")
    document_date = _date_value(_label_value(lines, r"Fakturadatum|Invoice\s*date|Kvitto(?:datum)?|Document\s*date"))
    if is_transfer_receipt:
        transfer_date = _date_value(_label_value(
            lines, r"Transfer\s+date|Transaction\s+date|Överföringsdatum|Overforingsdatum|Transaktionsdatum|Datum|Date"
        ))
        document_date = transfer_date or document_date
    due_date = _date_value(_label_value(lines, r"Förfallodatum|Forfallodatum|Due\s*date|Payment\s*due"))

    net_raw = _label_value(lines, r"Netto|Belopp\s+exkl\.?\s*moms|Summa\s+exkl\.?\s*moms|Amount\s+excl\.?\s*VAT")
    due_raw = _label_value(lines, r"Att\s+betala|Summa\s+att\s+betala|Belopp\s+att\s+betala|Amount\s+due")
    gross_raw = _label_value(lines, r"Total\s+inkl\.?\s*moms|Summa\s+inkl\.?\s*moms|Totalbelopp|Total\s+amount|Dokumentets\s+total|Totalt")
    vat_total_raw = _label_value(lines, r"Moms\s+totalt|Total\s+moms|Total\s+VAT|Momsbelopp")

    vat_lines: list[dict[str, str]] = []
    for line in lines:
        rate_first = re.fullmatch(r"(?:Moms|VAT)\s+(25|12|6|0)\s*%\s*:?\s+(-?[\d][\d\s.,']*)(?:\s+(?:SEK|kr))?", line, re.IGNORECASE)
        amount_first = re.fullmatch(r"(-?[\d][\d\s.,']*)\s+(25|12|6|0)\s*%\s+(?:Moms|VAT)", line, re.IGNORECASE)
        match = rate_first or amount_first
        if match:
            if rate_first:
                rate, raw_amount = match.groups()
            else:
                raw_amount, rate = match.groups()
            amount = parse_decimal(raw_amount)
            if amount is not None:
                vat_lines.append({"rate": rate, "amount": decimal_text(amount) or "0.00"})

    vat_total = parse_decimal(vat_total_raw)
    if vat_lines and vat_total is None:
        unique_vat_lines = list({
            (item["rate"], item["amount"]): item for item in vat_lines
        }.values())
        vat_total = sum(
            (parse_decimal(item["amount"]) or Decimal(0) for item in unique_vat_lines),
            Decimal(0),
        )
    elif vat_total is None and re.search(r"\bingen moms\b|\bno vat\b|\bvat exempt\b", lower_text):
        vat_total = Decimal(0)

    net_amount = parse_decimal(net_raw)
    gross_amount = parse_decimal(gross_raw)
    amount_due = parse_decimal(due_raw)
    transfer_amount = None
    if is_transfer_receipt:
        transfer_amount_raw = _label_value(
            lines,
            r"Transfer\s+amount|Amount\s+transferred|Transaction\s+amount|"
            r"Överfört\s+belopp|Overfort\s+belopp|Belopp|Amount",
        )
        transfer_amount = parse_decimal(transfer_amount_raw)
        if transfer_amount is None:
            currency_amounts = []
            for line in lines:
                match = re.search(
                    r"(?<!\d)(-?(?:\d{1,3}(?:[ \u00a0']\d{3})+|\d+)(?:[,.]\d{2})?)\s*(?:SEK|kr)\b",
                    line,
                    re.IGNORECASE,
                )
                if match:
                    amount = parse_decimal(match.group(1))
                    if amount is not None:
                        currency_amounts.append(amount)
            if len(currency_amounts) == 1:
                transfer_amount = currency_amounts[0]

        gross_amount = transfer_amount
        net_amount = None
        vat_total = None
        amount_due = None
        vat_lines = []

    currency_match = re.search(r"\b(SEK|EUR|NOK|DKK|USD|GBP|CHF|PLN)\b", full_text, re.IGNORECASE)
    if currency_match:
        currency = currency_match.group(1).upper()
    elif re.search(r"\b(?:kr|kronor)\b", full_text, re.IGNORECASE):
        currency = "SEK"
    else:
        currency = "UNKNOWN"

    is_credit_note = bool(re.search(r"kreditfaktura|credit\s+note|credit\s+invoice", lower_text))
    is_receipt = bool(re.search(r"kvitto|receipt|kassakvitto|\bkontantnota\b", lower_text))
    reverse_charge = bool(re.search(r"omvänd\s+skattskyldighet|omvand\s+skattskyldighet|reverse\s+charge", lower_text))
    if non_invoice_type:
        invoice_number = None
        document_date = None
        due_date = None
        payment_reference = None
    reasons = []
    if is_transfer_receipt:
        reasons.append("Распознана квитанция банковского перевода, а не фактура; проверьте получателя и связь с расходом.")
        if not supplier_name:
            reasons.append("Поставщик не определён. Lunar распознан как банк/логотип и не подставлен как поставщик.")
        if not document_date:
            reasons.append("Дата перевода не найдена; проверьте её по оригиналу.")
        if transfer_amount is None:
            reasons.append("Сумма перевода не найдена; проверьте её по оригиналу.")
        reasons.append("Нетто и НДС не вычислялись: квитанция перевода не даёт основания для расчёта НДС.")
    else:
        if not supplier_name:
            reasons.append("Поставщик не удалось надёжно определить; проверьте и заполните поле вручную.")
        if re.search(r"köpare|kopare|buyer", lower_text) and not supplier_name:
            reasons.append("Найдены данные покупателя, но они не подтверждают имя поставщика.")
        if not full_text:
            reasons.append("В документе не распознан текст; заполните поля вручную по оригиналу.")
        if not document_date:
            reasons.append("Дата фактуры/чека не распознана; проверьте её по оригиналу.")
        if not invoice_number and not is_credit_note:
            reasons.append("Номер фактуры не распознан; проверьте его по оригиналу.")
        if currency == "UNKNOWN":
            reasons.append("Валюта не распознана; выберите её вручную.")
        if net_amount is None:
            reasons.append("Сумма без НДС не найдена; проверьте её по оригиналу. Оценочное значение не подставлялось.")
        if gross_amount is None:
            reasons.append("Итоговая сумма не распознана; проверьте её по оригиналу.")
        if reverse_charge:
            reasons.append("Обнаружено указание на обратное начисление НДС; проверьте его по оригиналу.")

    if not is_transfer_receipt and net_amount is not None and vat_total is not None and gross_amount is not None:
        difference = (net_amount + vat_total - gross_amount).copy_abs()
        if difference > Decimal("0.02"):
            reasons.append(f"Нетто + НДС отличаются от итога на {decimal_text(difference)}.")
    elif not is_transfer_receipt and net_amount is not None and gross_amount is not None and vat_total is None and abs(net_amount - gross_amount) > Decimal("0.02"):
        reasons.append("НДС не удалось установить для проверки общей суммы.")
    if not is_transfer_receipt and vat_total_raw and vat_lines:
        stated_vat = parse_decimal(vat_total_raw)
        if stated_vat is not None and abs(stated_vat - vat_total) > Decimal("0.02"):
            reasons.append("Общая сумма НДС не совпадает с суммой строк по ставкам.")

    return {
        "supplier_name": supplier_name,
        "supplier_org_number": supplier_org_number,
        "supplier_vat_number": supplier_vat_number,
        "document_type": non_invoice_type or (
            "transfer_receipt" if is_transfer_receipt else
            "credit_note" if is_credit_note else "receipt" if is_receipt else "invoice"
        ),
        "non_invoice_document_type": non_invoice_type,
        "expense_category": "Квитанция перевода" if is_transfer_receipt else None,
        "suggested_verification_status": "requires_review" if is_transfer_receipt else None,
        "document_date": document_date,
        "due_date": due_date,
        "invoice_number": invoice_number,
        "payment_reference": payment_reference,
        "currency_code": currency,
        "net_amount": decimal_text(net_amount),
        "vat_amount": decimal_text(vat_total),
        "gross_amount": decimal_text(gross_amount),
        "transfer_amount": decimal_text(transfer_amount),
        "amount_due": decimal_text(amount_due),
        "vat_lines": vat_lines,
        "reverse_charge": reverse_charge,
        "review_reasons": reasons,
        "raw_text": full_text,
    }


def parse_invoice_money(value):
    text = str(value or "").strip().replace("−", "-").replace("\u00a0", " ")
    text = re.sub(r"^(?:SEK|EUR|NOK|DKK|kr)\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*(?:SEK|EUR|NOK|DKK|kr)\s*$", "", text, flags=re.IGNORECASE)
    pattern = (
        r"-?(?:\d+(?:[,.]\d{1,2})?|\d{1,3}(?:[ ']\d{3})+(?:[,.]\d{1,2})?"
        r"|\d{1,3}(?:\.\d{3})+,\d{1,2}|\d{1,3}(?:,\d{3})+\.\d{1,2})"
    )
    return parse_decimal(text) if re.fullmatch(pattern, text) else None


def invoice_layout_rows(elements, caption_patterns=()):
    def separate_captions(left, right):
        return (any(re.fullmatch(rf"(?:{pattern})\s*:?", left, re.IGNORECASE) for pattern in caption_patterns)
                and any(re.fullmatch(rf"(?:{pattern})\s*:?", right, re.IGNORECASE) for pattern in caption_patterns))

    rows = []
    for element in sorted(elements, key=lambda item: (item["y0"], item["x0"])):
        center = (element["y0"] + element["y1"]) / 2
        row = next((item for item in reversed(rows[-3:]) if abs(item[0] - center) <= 3), None)
        if row is None:
            row = [center, []]
            rows.append(row)
        row[1].append(element)
    result = []
    for _, words in rows:
        runs = []
        for word in sorted(words, key=lambda item: item["x0"]):
            word_height = max(1, word["y1"] - word["y0"])
            if (runs and word["x0"] - runs[-1]["x1"] < max(6, word_height * 1.2)
                    and not separate_captions(runs[-1]["text"], word["text"])):
                run = runs[-1]
                run["text"] += " " + word["text"]
                run["x1"] = max(run["x1"], word["x1"])
                run["y1"] = max(run["y1"], word["y1"])
            else:
                runs.append(dict(word))
        result.append(runs)
    for row in result:
        for cell in list(row):
            if cell not in row:
                continue
            adjacent = []
            for other_row in result:
                if other_row is row:
                    continue
                for other in other_row:
                    overlap = min(cell["y1"], other["y1"]) - max(cell["y0"], other["y0"])
                    height = max(1, min(cell["y1"] - cell["y0"], other["y1"] - other["y0"]))
                    gap = other["x0"] - cell["x1"]
                    if (0 <= gap < max(6, height * 1.2) and overlap / height > 0.6
                            and not separate_captions(cell["text"], other["text"])):
                        adjacent.append((gap, other_row, other))
            if adjacent:
                _, other_row, other = min(adjacent, key=lambda item: item[0])
                cell["text"] += " " + other["text"]
                cell["x1"] = other["x1"]
                cell["y0"] = min(cell["y0"], other["y0"])
                cell["y1"] = max(cell["y1"], other["y1"])
                other_row.remove(other)
    result = [sorted(row, key=lambda item: item["x0"]) for row in result if row]
    result.sort(key=lambda row: min(item["y0"] for item in row))
    return result


def invoice_order_lines(rows):
    captions = (
        r"Proj\s*\.\s*nr\s*\.?|Märkning|Ert\s+ordernr", r"Sälj\s*\.\s*ordnr\s*\.?",
        r"Order\s*(?:nr\.?|no\.?|number|#)|Ordernummer|Order\b", r"Summa\s+order(?:\s+exkl\.?\s*moms)?",
        r"Moms\s+order(?:\s+\d+(?:[,.]\d+)?\s*%)?",
    )
    lines = []
    sources = []
    for row in rows:
        for index, cell in enumerate(row):
            for caption in captions:
                match = re.fullmatch(rf"({caption})(?:\s*:\s*|\s+)?(.*?)", cell["text"].strip(), re.IGNORECASE)
                if not match:
                    continue
                label, value = match.groups()
                value_cell = cell
                if not value:
                    if not cell["text"].rstrip().endswith(":") and not re.match(r"Summa\s+order|Märkning|Ert\s+ordernr", label, re.IGNORECASE):
                        continue
                    candidates = row[index + 1:]
                    if re.match(r"Summa\s+order", label, re.IGNORECASE):
                        candidates = [item for item in candidates if parse_invoice_money(item["text"]) is not None]
                        if len(candidates) != 1:
                            continue
                    if not candidates:
                        continue
                    value_cell = candidates[0]
                    value = value_cell["text"]
                if re.fullmatch(r"Märkning|Ert\s+ordernr", label, re.IGNORECASE):
                    label = "Proj.nr."
                elif label.casefold() == "order":
                    label = "Order nr"
                lines.append(f"{label}: {value}")
                sources.append({"box": [value_cell[axis] for axis in ("x0", "y0", "x1", "y1")],
                                "kind": "order_total" if re.match(r"Summa\s+order", label, re.IGNORECASE) else "order_header"})
                break
    return lines, sources


def invoice_seller_blocks(cells, page):
    candidates = []
    width = max((cell["x1"] for cell in cells), default=1)
    height = max((cell["y1"] for cell in cells), default=1)
    for cell in cells:
        value = cell["text"].strip()
        if any(re.match(r"^(?:Faktura(?:mottagare|adress)|Fakturerat\s+till|Faktureringsadress|Betalningsavsändare|Köparens\s+adress|Köpare|Buyer|Leveransadress)\b", other["text"], re.IGNORECASE)
               and abs(other["x0"] - cell["x0"]) < 24 and 0 <= cell["y0"] - other["y0"] < 100
               for other in cells):
            continue
        if re.fullmatch(r"(?:Post|Besöks)?adress\s*:?", value, re.IGNORECASE):
            below = [other for other in cells if abs(other["x0"] - cell["x0"]) < 12
                     and 4 < other["y0"] - cell["y0"] < 28
                     and re.fullmatch(r"[^\d@:/]{3,160}", other["text"].strip())]
            if below:
                nearest = min(below, key=lambda other: other["y0"])
                candidates.append((90, nearest["text"].strip(), page, nearest))
        company = re.match(r"^(.+?\b(?:AB|HB|KB|Aktiebolag|Ltd\.?|GmbH|AS)(?:\s+Servicecenter)?)(?=\s*[,|]|$)", value, re.IGNORECASE)
        if not company:
            continue
        name = company.group(1)
        if ":" in name:
            continue
        # A receipt may identify the seller in the footer without a postal address.
        receipt_footer = (
            cell["y0"] > height * 0.9
            and re.search(r"\bKONTANTNOTA\b", str(page.get("text") or ""), re.IGNORECASE)
            and any(re.search(r"\b(?:Org\.?\s*nr|Organisationsnummer)\b", other["text"], re.IGNORECASE)
                    for other in cells)
        )
        address_lines = [other["text"] for other in cells
                         if abs(other["x0"] - cell["x0"]) < 16 and 5 < other["y0"] - cell["y0"] < 42]
        has_address = any(re.search(r"\b(?:Box\s*\d+|\d{3}\s?\d{2}\b)|[A-Za-zÅÄÖåäö]+(?:väg|gatan|vägen)\s*\d", line, re.IGNORECASE)
                          for line in [value[len(name):], *address_lines])
        if not has_address and not receipt_footer:
            continue
        company_stem = re.sub(r"\s+(?:AB|HB|KB|Aktiebolag|Ltd\.?|GmbH|AS)$", "", name, flags=re.IGNORECASE)
        own_registration = any(
            re.match(rf"^(?:{re.escape(company_stem)}\s+)?(?:Org\.?\s*(?:nr|nummer)|Organisationsn(?:ummer|r)|Momsreg|VAT\s*/)", other["text"], re.IGNORECASE)
            and abs(other["x0"] - cell["x0"]) < 24 and 0 <= other["y0"] - cell["y0"] < 80
            for other in cells
        )
        if cell["y0"] < height * 0.3:
            candidates.append((95 if own_registration else 82 if cell["x0"] < width * 0.45 else 80, name, page, cell))
        elif cell["y0"] > height * 0.65:
            candidates.append((95 if own_registration else 85, name, page, cell))
    return candidates


def invoice_context(text):
    return {
        "reverse_charge": bool(re.search(r"omvänd\s+(?:moms|skattskyldighet|betalningsskyldighet)|reverse\s+charge", text or "", re.IGNORECASE)),
        "prepaid": bool(re.search(r"förskottsbetald|betalt\s+med\s+kort|kvar\s+att\s+betala", text or "", re.IGNORECASE)),
    }


def required_document_fields(document_type):
    common = ("supplier_name", "document_date", "net_amount", "vat_amount", "currency_code")
    return common + (("gross_amount",) if document_type == "receipt"
                     else ("invoice_number", "amount_due"))


def parse_invoice_pages(pages):
    text = "\n\n".join(str(page.get("text") or "") for page in pages)
    fields = parse_document_text(text)
    non_invoice_type = fields.get("non_invoice_document_type")
    labels = {
        "supplier_name": r"Leverantör|Supplier|Seller|Returadress|Return\s+address",
        "invoice_number": r"Fakturanummer|Faktura\s*nr\.?|Faktura/OCR-nummer|Invoice\s*(?:number|no\.?)",
        "payment_reference": r"OCR(?:-\s*(?:nummer|nr)(?:/meddelande)?|/Meddelande|-/referensnummer)?|Betalningsreferens|Payment\s*reference|Vänligen\s+ange\s+referens",
        "document_date": r"Fakturadatum|Faktureringsdatum|Datum|Invoice\s*date",
        "due_date": r"Förfallodatum|Förfallodag|Oss\s+tillhanda\s+senast|Tillhanda\s+senast|Due\s*date|Payment\s*due",
        "currency_code": r"Fakturans\s+valuta|Valuta|Currency",
        "net_amount": r"Netto|Nettosumma(?:\s+artiklar)?|Summa\s+netto|Varuvärde|Belopp\s+exkl\.?\s*moms|(?:Summa|Totalt?)\s+exkl(?:usive|\.)?\s*moms|Exkl\.?\s*moms|Totalt\s+fakturerat\s+belopp\s+exkl\.?\s*moms",
        "vat_amount": r"Moms(?:\s*\(?\s*\d+(?:[,.]\d+)?\s*%\s*\)?)?|Moms\s*belopp(?:\s+(?:i\s+)?SEK)?|Moms\s+totalt|Total\s+moms|Total\s+VAT|Summa\s+moms\s+på\s+momsbelagda\s+produkter,?\s*\d+\s*%",
        "rounding_amount": r"Öresavrundning|Öresavr\.?|Öresutjämning|Avrundning|Avrundningsdifferens|Rounding",
        "amount_due": r"Att\s+betala(?:\s*\((?:SEK|EUR|NOK|DKK|USD|GBP|CHF|PLN)\))?|Kvar\s+att\s+betala|Summa\s+att\s+betala|Belopp\s+att\s+betala|Amount\s+due|Fakturatotal|Fakturabelopp|Totalt\s+fakturerat\s+belopp\s+inkl\.?\s*moms|Totalbelopp\s+i\s+SEK",
        "gross_amount": r"Totalt?\s+inkl\.?\s*moms|Summa\s+inkl\.?\s*moms|Totalbelopp(?:\s+inkl\.?\s*moms)?(?:\s+i\s+SEK)?",
        "taxable_net_amount": r"Totalbelopp\s+momspliktigt",
        "exempt_net_amount": r"Totalbelopp\s+ej\s+momspliktigt",
    }
    is_receipt = fields.get("document_type") == "receipt"
    if is_receipt:
        labels["document_date"] += r"|Kvitto(?:datum)?|Köpdatum|Följesedeldatum"
        labels["invoice_number"] += r"|Kvittonummer|Kvitto\s*nr\.?|Kontantnota"
        labels["gross_amount"] += r"|Totalt"
        labels["vat_amount"] += r"|Varav\s+moms(?:\s*\(?\s*\d+(?:[,.]\d+)?\s*%\s*\)?)?"
    if fields.get("document_type") == "credit_note":
        labels["amount_due"] += r"|Summa\s+er\s+tillgodo|Belopp\s+att\s+återfå"
    candidates = {key: [] for key in labels}
    companies = []
    field_sources = {}
    order_pages = []
    due_caption_seen = False

    def checked_value(key, raw):
        value = str(raw).strip().strip(":")
        if key.endswith("amount") or key == "amount_due":
            return decimal_text(parse_invoice_money(value))
        if key in {"document_date", "due_date"}:
            pattern = r"[\d./-]+(?:\s+\d{2}:\d{2}(?::\d{2})?)?" if is_receipt and key == "document_date" else r"[\d./-]+"
            return _date_value(value) if re.fullmatch(pattern, value) else None
        if key == "currency_code":
            return value.upper() if re.fullmatch(r"SEK|EUR|NOK|DKK|USD|GBP|CHF|PLN", value, re.IGNORECASE) else None
        if key == "invoice_number":
            return value if re.fullmatch(r"(?=[\w/-]*\d)[\w/-]{1,120}", value) else None
        if key == "payment_reference":
            value = re.sub(r"\s+vid\s+betalning\.?$", "", value, flags=re.IGNORECASE)
            return value if re.fullmatch(r"(?=[\w /-]*\d)[\w /-]{1,120}", value) else None
        value = re.sub(r"^(?:Returadress|Return\s+address)\s*:\s*", "", value, flags=re.IGNORECASE)
        if re.fullmatch(r".+\s(?:AB|Aktiebolag|Ltd\.?|GmbH|AS)", value, re.IGNORECASE):
            return value
        return None

    for page in pages:
        elements = page.get("elements") or []
        if elements:
            rows = invoice_layout_rows(elements, labels.values())
        else:
            rows = [[{"text": line.strip(), "x0": 0, "x1": 500, "y0": index * 15, "y1": index * 15 + 10}]
                    for index, line in enumerate(str(page.get("text") or "").splitlines()) if line.strip()]
        cells = [cell for row in rows for cell in row]
        companies.extend(invoice_seller_blocks(cells, page))
        order_lines, order_sources = invoice_order_lines(rows)
        order_pages.append({**page, "text": "\n".join(order_lines), "line_sources": order_sources})
        if is_receipt:
            for row in rows:
                for cell in row:
                    if re.search(r"\bFöljesedeldatum\s*$", cell["text"], re.IGNORECASE):
                        dates = [other for other in row if other["x0"] >= cell["x1"]
                                 and checked_value("document_date", other["text"]) is not None]
                        if len(dates) == 1:
                            other = dates[0]
                            candidates["document_date"].append((checked_value("document_date", other["text"]), {
                                "page": page.get("page", 1), "attachment_index": page.get("attachment_index", 0),
                                "caption": "Följesedeldatum", "text": other["text"], "kind": "header",
                                "box": [other[axis] for axis in ("x0", "y0", "x1", "y1")],
                            }))
                    included_vat = re.fullmatch(
                        r"TOTAL\s*\(\s*Inkl\.?\s*moms\s+(SEK|EUR|NOK|DKK)\s+([\d\s.,]+)\s*\)",
                        cell["text"], re.IGNORECASE,
                    )
                    if included_vat:
                        gross_cells = [other for other in row if other["x0"] >= cell["x1"]
                                       and parse_invoice_money(other["text"]) is not None]
                        vat = decimal_text(parse_invoice_money(included_vat.group(2)))
                        if len(gross_cells) == 1 and vat is not None:
                            for key, other, value in (
                                ("vat_amount", cell, vat),
                                ("gross_amount", gross_cells[0], decimal_text(parse_invoice_money(gross_cells[0]["text"]))),
                            ):
                                candidates[key].append((value, {
                                    "page": page.get("page", 1), "attachment_index": page.get("attachment_index", 0),
                                    "caption": cell["text"], "text": other["text"], "kind": "invoice_total",
                                    "priority": 3, "box": [other[axis] for axis in ("x0", "y0", "x1", "y1")],
                                }))
        for cell in cells:
            cell_text = cell["text"].strip()
            if fields.get("currency_code") == "UNKNOWN":
                currency_codes = set(re.findall(r"\b(SEK|EUR|NOK|DKK|USD|GBP|CHF|PLN)\b", "\n".join(item["text"] for item in cells), re.IGNORECASE))
                if len(currency_codes) == 1:
                    fields["currency_code"] = currency_codes.pop().upper()
            for key, label in labels.items():
                if is_receipt and key == "gross_amount" and any(
                    cell in row and sum(bool(re.fullmatch(
                        r"Antal|A\s*pris|Rabatt\s*(?:%|kr)|Artikel\s*nr|Beskrivning", other["text"], re.IGNORECASE
                    )) for other in row) >= 2 for row in rows
                ):
                    continue
                if key == "due_date" and re.match(rf"^(?:{label})(?:\s|:|$)", cell_text, re.IGNORECASE):
                    due_caption_seen = True
                inline = re.fullmatch(rf"(?:{label})(?:\s*:\s*|\s+)(.+)", cell_text, re.IGNORECASE)
                raw_value = inline.group(1) if inline else None
                value_cell = cell
                if re.fullmatch(rf"(?:{label})\s*:?", cell_text, re.IGNORECASE):
                    def is_caption(other):
                        return any(re.fullmatch(rf"(?:{caption})\s*:?", other["text"].strip(), re.IGNORECASE)
                                   for caption in labels.values())

                    row_tolerance = 6 if is_receipt and key == "vat_amount" and re.match(r"Varav\s+moms", cell_text, re.IGNORECASE) else 4
                    same_row_values = [other for other in cells if other["x0"] >= cell["x1"]
                                       and abs(other["y0"] - cell["y0"]) < row_tolerance
                                       and checked_value(key, other["text"]) is not None
                                       and not any((is_caption(between) or (
                                                       checked_value(key, between["text"]) is None
                                                       and re.search(r"[A-Za-zÅÄÖåäö]", between["text"])
                                                       and not re.fullmatch(r"SEK|EUR|NOK|DKK|USD|GBP|CHF|PLN", between["text"], re.IGNORECASE)))
                                                   and cell["x1"] <= between["x0"] < other["x0"]
                                                   and abs(between["y0"] - cell["y0"]) < row_tolerance for between in cells)]
                    right_headers = [other["x0"] for other in cells
                                     if abs(other["y0"] - cell["y0"]) < 4 and other["x0"] > cell["x0"]]
                    right_edge = min(right_headers) if right_headers else cell["x1"] + 100
                    aggregate_values = []
                    if key == "vat_amount" and re.fullmatch(r"Moms\s*belopp(?:\s*SEK)?", cell_text, re.IGNORECASE):
                        total_rows = [row for row in rows if 4 < row[0]["y0"] - cell["y0"] < 120
                                      and re.fullmatch(r"Totalt?", row[0]["text"].strip(), re.IGNORECASE)]
                        if total_rows:
                            aggregate_values = [item for item in total_rows[0]
                                                if cell["x0"] - 5 <= item["x0"] < right_edge - 5
                                                and checked_value(key, item["text"]) is not None]
                    neighbors = [other for other in cells if other is not cell and (
                        4 < other["y0"] - cell["y0"] < 42
                            and (cell["x0"] - 5 <= other["x0"] < right_edge - 5
                                or abs(other["x1"] - cell["x1"]) < 6)
                        and not any(is_caption(between) and cell["y0"] + 4 < between["y0"] <= other["y0"] + 2
                                    and abs(between["x0"] - cell["x0"]) < 25 for between in cells)
                    ) and checked_value(key, other["text"]) is not None]
                    if key == "vat_amount" and re.fullmatch(r"Moms\s*belopp(?:\s*SEK)?", cell_text, re.IGNORECASE):
                        neighbors = [other for other in neighbors if abs(other["x1"] - cell["x1"]) < 8
                                     or cell["x0"] <= other["x0"] and other["x1"] <= cell["x1"] + 8]
                    if len(aggregate_values) == 1:
                        value_cell = aggregate_values[0]
                        raw_value = value_cell["text"]
                    elif len(same_row_values) == 1:
                        value_cell = same_row_values[0]
                        raw_value = value_cell["text"]
                    elif not same_row_values and neighbors:
                        distance = min(abs(other["y0"] - cell["y0"]) for other in neighbors)
                        nearest = [other for other in neighbors if abs(other["y0"] - cell["y0"]) == distance]
                        if len(nearest) == 1:
                            value_cell = nearest[0]
                            raw_value = value_cell["text"]
                value = checked_value(key, raw_value) if raw_value is not None else None
                if value is not None:
                    source = {"source": page.get("source"), "page": page.get("page", 1),
                              "attachment_index": page.get("attachment_index", 0),
                              "box": [value_cell[axis] for axis in ("x0", "y0", "x1", "y1")],
                              "text": value_cell["text"], "caption": cell_text,
                              "kind": "invoice_total" if "amount" in key else "header"}
                    if key == "vat_amount":
                        row_with_total = any(value_cell in row and re.fullmatch(r"Totalt?", row[0]["text"].strip(), re.IGNORECASE) for row in rows)
                        ledger_row = any(cell in row and any(other is not cell and is_caption(other) for other in row) for row in rows) if re.fullmatch(rf"(?:{label})\s*:?", cell_text, re.IGNORECASE) else False
                        source["priority"] = (4 if re.fullmatch(r"Moms\s*belopp\s+i\s+SEK", cell_text, re.IGNORECASE)
                                              else 3 if row_with_total or ledger_row or re.search(r"\d.*%", cell_text)
                                              else 2 if re.search(r"belopp", cell_text, re.IGNORECASE) else 1)
                    candidates[key].append((value, source))
    if not candidates["supplier_name"] and companies:
        priority = max(item[0] for item in companies)
        best = [item for item in companies if item[0] == priority]
        if len({item[1].casefold() for item in best}) == 1:
            _, company, page, cell = best[0]
            candidates["supplier_name"].append((company, {"page": page.get("page", 1), "text": cell["text"],
                                                       "kind": "seller_address_block"}))
    fiscal_summary_pages = {
        (source.get("attachment_index", 0), source["page"])
        for _, source in candidates["taxable_net_amount"]
        if any((other.get("attachment_index", 0), other["page"]) ==
               (source.get("attachment_index", 0), source["page"])
               for _, other in candidates["exempt_net_amount"])
    }
    diagnostics = []
    for key, matches in candidates.items():
        if key == "vat_amount" and fiscal_summary_pages:
            matches = [(value, source) for value, source in matches
                       if (source.get("attachment_index", 0), source["page"]) in fiscal_summary_pages]
        if key == "vat_amount" and matches:
            priority = max(source.get("priority", 1) for _, source in matches)
            matches = [(value, source) for value, source in matches if source.get("priority", 1) == priority]
        values = {value for value, _ in matches}
        if len(values) == 1:
            fields[key] = matches[0][0]
            field_sources[key] = [source for _, source in matches]
        elif len(values) > 1:
            fields[key] = None
            diagnostics.append(f"Несколько различных значений поля {key}; требуется сверка по оригиналу.")
        elif key not in ("currency_code",):
            fields[key] = None
    if fields.get("net_amount") is None and all(fields.get(key) is not None for key in ("taxable_net_amount", "exempt_net_amount", "vat_amount", "gross_amount")):
        explicit_net = Decimal(fields["taxable_net_amount"]) + Decimal(fields["exempt_net_amount"])
        if explicit_net + Decimal(fields["vat_amount"]) == Decimal(fields["gross_amount"]):
            fields["net_amount"] = decimal_text(explicit_net)
            field_sources["net_amount"] = field_sources.get("taxable_net_amount", []) + field_sources.get("exempt_net_amount", [])
    if (is_receipt and fields.get("net_amount") is None
            and fields.get("gross_amount") is not None and fields.get("vat_amount") is not None
            and field_sources.get("gross_amount") and field_sources.get("vat_amount")):
        fields["net_amount"] = decimal_text(Decimal(fields["gross_amount"]) - Decimal(fields["vat_amount"]))
        field_sources["net_amount"] = [{"kind": "derived_from_stated_totals", "operation": "gross_minus_vat",
                                        "sources": field_sources["gross_amount"] + field_sources["vat_amount"]}]
    fields["rounding_amount"] = fields.get("rounding_amount")
    if fields.get("net_amount") is not None and fields.get("vat_amount") is not None:
        fields["gross_amount"] = decimal_text(Decimal(fields["net_amount"]) + Decimal(fields["vat_amount"]))
    fields["field_sources"] = field_sources
    due_matches = candidates["due_date"]
    explicit_due_caption = due_caption_seen or any(re.search(rf"(?:{labels['due_date']})", str(page.get("text") or ""), re.IGNORECASE)
                               for page in pages)
    if due_matches:
        fields["due_date_kind"] = "explicit" if fields.get("due_date") else "ambiguous"
    elif explicit_due_caption:
        fields["due_date_kind"] = "ambiguous"
    else:
        fields["due_date_kind"] = "absent"
        terms = re.findall(r"Betalningsvillkor\s*:?\s*(\d{1,3})\s*(?:dagar|days)(?:\s+netto)?\b", text, re.IGNORECASE)
        if len(set(terms)) == 1 and fields.get("document_date") and 0 <= int(terms[0]) <= 365:
            fields["due_date"] = (datetime.strptime(fields["document_date"], "%Y-%m-%d") + timedelta(days=int(terms[0]))).strftime("%Y-%m-%d")
            fields["due_date_kind"] = "calculated"
            field_sources["due_date"] = [{"kind": "payment_terms", "days": int(terms[0]),
                                          "base_date": fields["document_date"], "text": "Betalningsvillkor: " + terms[0] + " dagar"}]
        elif terms:
            fields["due_date_kind"] = "ambiguous"
    fields["due_date_review_required"] = fields["due_date_kind"] in {"ambiguous", "calculated"}
    fields["due_date_reviewed"] = False
    fields["due_date_evidence"] = field_sources.get("due_date", [source for _, source in due_matches])
    if fields["due_date_review_required"]:
        diagnostics.append("Срок оплаты рассчитан по условиям или неоднозначен; требуется явная сверка, это не распознанная дата документа.")
    fields["allocation_basis"] = "net"
    fields["raw_text"] = text
    fields.update(invoice_context(text))
    fields["document_total"] = fields.get("gross_amount")
    if non_invoice_type:
        fields.update(
            supplier_name=None, supplier_org_number=None, supplier_vat_number=None,
            invoice_number=None, document_date=None, due_date=None, payment_reference=None,
        )
        diagnostics.append(
            "Распознано уведомление о заказе; суммы из него не являются подтверждёнными суммами фактуры."
            if non_invoice_type == "order_confirmation"
            else "Распознано решение о транспортном налоге, а не фактура; данные получателя не являются данными поставщика."
        )
    fields["review_reasons"] = diagnostics
    for key in required_document_fields(fields.get("document_type")):
        if fields.get(key) in (None, "", "UNKNOWN"):
            fields["review_reasons"].append(f"Не подтверждено поле {key}; проверьте оригинал.")
    net = parse_decimal(fields.get("net_amount"))
    vat = parse_decimal(fields.get("vat_amount"))
    due = parse_decimal(fields.get("amount_due"))
    rounding = parse_decimal(fields.get("rounding_amount")) if fields.get("rounding_amount") is not None else Decimal(0)
    if None not in (net, vat, due, rounding) and net + vat + rounding != due:
        fields["review_reasons"].append("Сумма без НДС + НДС + округление не совпадает с суммой к оплате.")
    proposal = parse_project_order_allocations(order_pages)
    if not proposal["orders"] and fields.get("net_amount") is not None:
        markers = []
        order_ids = set()
        for page in order_pages:
            for index, line in enumerate(page["text"].splitlines()):
                marker = re.fullmatch(r"Proj\.nr\.:\s*(.+)", line, re.IGNORECASE)
                order = re.fullmatch(r"Order nr:\s*(\S+)", line, re.IGNORECASE)
                if marker:
                    markers.append((marker.group(1), page, page["line_sources"][index]))
                if order:
                    order_ids.add(order.group(1))
        if len({marker[0] for marker in markers}) == 1 and len(order_ids) == 1:
            marker, page, source = markers[0]
            proposal["orders"].append({
                "project_code": marker if re.fullmatch(r"[A-Za-z]*\d[\w./-]*", marker) else None,
                "original_marker": marker, "order_id": next(iter(order_ids)),
                "net_amount": fields["net_amount"], "vat_amount": None, "ambiguous": False,
                "sources": [{**source, "source": page.get("source"), "page": page.get("page", 1),
                             "attachment_index": page.get("attachment_index", 0)}],
            })
    fields["order_proposals"] = proposal["orders"]
    fields["suggested_verification_status"] = "requires_review"
    return fields


def due_date_edit(fields, value, reviewed):
    original = str(fields.get("due_date") or "").strip()
    raw = str(value or "").strip()
    origin = fields.get("due_date_kind") or ("manual" if original else "absent")
    changed = raw != original
    kind = ("manual" if raw else "ambiguous") if changed else origin
    evidence = list(fields.get("due_date_evidence") or [])
    if changed:
        evidence.append({"kind": "manual", "previous_due_date": original or None, "previous_kind": origin})
    return {"due_date": raw or None, "due_date_kind": kind,
            "due_date_review_required": kind in {"calculated", "ambiguous"} or not changed and fields.get("due_date_review_required") is True,
            "due_date_reviewed": bool(raw) if changed else reviewed is True, "due_date_evidence": evidence}


def due_date_issues(fields):
    raw = str(fields.get("due_date") or "").strip()
    if raw:
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return ["Срок оплаты должен быть корректной датой ГГГГ-ММ-ДД."]
    kind = fields.get("due_date_kind") or ("manual" if raw else "absent")
    if kind == "ambiguous" and not raw:
        return ["Неоднозначный срок оплаты: выберите и укажите проверенную дату, не оставляйте догадку."]
    if (fields.get("due_date_review_required") or kind in {"calculated", "ambiguous"}) and fields.get("due_date_reviewed") is not True:
        return ["Сверьте срок оплаты и подтвердите его источник: рассчитанная дата не является явно указанной."]
    return []


def parse_project_order_allocations(pages: list[dict[str, Any]]) -> dict[str, Any]:
    money_token = r"-?\d[\d.,' \u00a0]*"

    def parse_order_amount(raw):
        raw = raw.strip()
        valid_formats = (
            r"-?(?:\d+(?:[,.]\d{1,2})?"
            r"|\d{1,3}(?:[ '\u00a0]\d{3})+(?:[,.]\d{1,2})?"
            r"|\d{1,3}(?:\.\d{3})+,\d{1,2}"
            r"|\d{1,3}(?:,\d{3})+\.\d{1,2})"
        )
        return parse_decimal(raw) if re.fullmatch(valid_formats, raw) else None

    project_pattern = re.compile(
        r"^\s*Proj\s*\.\s*nr\s*\.?(?:\s*[:#]\s*|\s+)(\S.*?)\s*$",
        re.IGNORECASE,
    )
    order_pattern = re.compile(
        r"^\s*(?:Order\s*(?:nr\.?|no\.?|number|#)|Ordernummer|Sälj\s*\.\s*ordnr\s*\.?)\s*[:#]?\s*([\w./-]+)",
        re.IGNORECASE,
    )
    total_pattern = re.compile(
        r"^\s*Summa\s+order\s*(?:exkl\.?\s*moms)?\s*[:=]?\s*"
        rf"({money_token})",
        re.IGNORECASE,
    )
    vat_pattern = re.compile(
        r"^\s*(?:Moms(?:belopp)?\s+(?:på\s+)?order|Ordermoms|VAT\s+order(?:\s+amount)?)"
        r"\s*(?:(?P<rate>\d+(?:[,.]\d+)?)\s*%)?\s*[:=]\s*"
        rf"(?P<amount>{money_token})",
        re.IGNORECASE,
    )

    current_project = None
    current_order = None
    last_order_key = None
    orders_by_key = {}
    warnings = []
    project_since_total = False
    for page in pages:
        source_name = str(page.get("source") or page.get("name") or "PDF")
        page_number = int(page.get("page") or 1)
        for line_number, line in enumerate(str(page.get("text") or "").splitlines(), 1):
            project_match = project_pattern.search(line)
            if project_match:
                new_project = project_match.group(1).strip()
                if current_project and new_project.casefold() != current_project.casefold():
                    if last_order_key is not None:
                        current_order = None
                    last_order_key = None
                current_project = new_project
                project_since_total = True
            order_match = order_pattern.search(line)
            if order_match:
                new_order = order_match.group(1).strip()
                if current_order is None or new_order.casefold() != current_order.casefold():
                    if not project_since_total:
                        current_project = None
                    last_order_key = None
                current_order = new_order
            vat_match = vat_pattern.search(line)
            if vat_match:
                vat_amount = parse_order_amount(vat_match.group("amount"))
                target = orders_by_key.get(last_order_key)
                if vat_amount is not None and target is not None:
                    components = target.setdefault("vat_components", {})
                    vat_value = Decimal(decimal_text(vat_amount))
                    component_key = decimal_text(parse_decimal(vat_match.group("rate"))) or "total"
                    if (
                        component_key in components and components[component_key] != vat_value
                        or component_key == "total" and any(key != "total" for key in components)
                        or component_key != "total" and "total" in components
                    ):
                        target["ambiguous"] = True
                        target["vat_ambiguous"] = True
                        warnings.append(f"Неоднозначный VAT заказа {current_order or current_project}: {source_name}, стр. {page_number}.")
                    else:
                        components[component_key] = vat_value
                    target["sources"].append({
                        "source": source_name, "page": page_number,
                        "line": line_number, "text": line.strip(),
                        "attachment_index": int(page.get("attachment_index") or 0),
                    })
                elif vat_amount is not None:
                    warnings.append(f"Moms order utan найденного Summa order: {source_name}, стр. {page_number}.")
                else:
                    warnings.append(f"Неоднозначный формат VAT заказа: {source_name}, стр. {page_number}.")
                continue
            total_match = total_pattern.search(line)
            if not total_match:
                continue
            amount = parse_order_amount(total_match.group(1))
            if amount is None:
                warnings.append(f"Не удалось прочитать Summa order: {source_name}, стр. {page_number}.")
                continue
            amount = Decimal(decimal_text(amount))
            source = {
                "source": source_name,
                "page": page_number,
                "line": line_number,
                "text": line.strip(),
                "attachment_index": int(page.get("attachment_index") or 0),
            }
            if page.get("line_sources"):
                source.update(page["line_sources"][line_number - 1])
            project_since_total = False
            if current_order:
                key = ("order", current_order.casefold(), (current_project or "").casefold())
            else:
                key = ("project-total", (current_project or "").casefold(), decimal_text(amount))
            existing = orders_by_key.get(key)
            if existing is not None:
                if existing["net_amount"] != decimal_text(amount):
                    existing["ambiguous"] = True
                    warnings.append(
                        f"Повторный Order/Summa order имеет разные суммы: {source_name}, стр. {page_number}."
                    )
                elif not current_order:
                    existing["ambiguous"] = True
                    warnings.append(
                        f"Повторный итог без номера заказа неоднозначен и не просуммирован повторно: "
                        f"{current_project or 'код не найден'}, {source_name}, стр. {page_number}."
                    )
                existing["sources"].append(source)
                last_order_key = key
                continue
            orders_by_key[key] = {
                "order_id": current_order,
                "project_code": current_project if current_project and re.fullmatch(r"[A-Za-z]*\d[\w./-]*", current_project) else None,
                "original_marker": current_project,
                "net_amount": decimal_text(amount),
                "vat_amount": None,
                "sources": [source],
                "ambiguous": current_project is None,
                "vat_components": {},
            }
            last_order_key = key
            if current_project is None:
                warnings.append(f"Summa order без Proj.nr: {source_name}, стр. {page_number}.")

    orders = list(orders_by_key.values())
    for order in orders:
        components = order.pop("vat_components", {})
        if components and not order.pop("vat_ambiguous", False):
            vat_amount = sum(components.values(), Decimal(0))
            order["vat_amount"] = decimal_text(vat_amount)
            order["gross_amount"] = decimal_text(Decimal(order["net_amount"]) + vat_amount)
        else:
            warnings.append(
                f"НДС для заказа {order.get('order_id') or order.get('project_code') or 'без кода'} "
                "не распознан; распределение требует ручной сверки."
            )
    total_net = sum((Decimal(item["net_amount"]) for item in orders), Decimal(0))
    return {
        "orders": orders,
        "net_total": decimal_text(total_net),
        "warnings": list(dict.fromkeys(warnings)),
        "requires_review": bool(warnings) or any(item["ambiguous"] for item in orders),
    }


def build_project_order_allocations(orders, net_amount, vat_amount, gross_amount):
    totals = [parse_decimal(value) for value in (net_amount, vat_amount, gross_amount)]
    if any(value is None for value in totals):
        raise ValueError("Для сверки заказов укажите net, VAT и gross фактуры.")
    expected = [Decimal(decimal_text(value)) for value in totals]
    if expected[0] + expected[1] != expected[2]:
        raise ValueError("Net плюс VAT должны равняться gross фактуры.")
    actual = [Decimal(0), Decimal(0), Decimal(0)]
    grouped = {}
    project_objects = {}
    for order in orders:
        if order.get("ambiguous") or not str(order.get("project_code") or "").strip():
            raise ValueError("Исправьте и сверьте неоднозначные строки заказов по оригиналу.")
        if order.get("object_id") is None:
            raise ValueError("Выберите существующий объект для каждого заказа.")
        net_value = parse_decimal(order.get("net_amount"))
        vat_value = parse_decimal(order.get("vat_amount"))
        if net_value is None or vat_value is None:
            raise ValueError("Для каждого заказа нужны явные net и VAT; пропорциональный расчёт запрещён.")
        net_value = Decimal(decimal_text(net_value))
        vat_value = Decimal(decimal_text(vat_value))
        gross_value = net_value + vat_value
        object_id = int(order["object_id"])
        project_key = str(order["project_code"]).strip().casefold()
        if project_key in project_objects and project_objects[project_key] != object_id:
            raise ValueError("Один код проекта не может обозначать разные объекты одного поставщика.")
        project_objects[project_key] = object_id
        if object_id not in grouped:
            grouped[object_id] = {
                "object_id": object_id, "net_amount": Decimal(0),
                "vat_amount": Decimal(0), "gross_amount": Decimal(0), "notes": [],
            }
        allocation = grouped[object_id]
        for index, (key, value) in enumerate((
            ("net_amount", net_value), ("vat_amount", vat_value), ("gross_amount", gross_value),
        )):
            allocation[key] += value
            actual[index] += value
        pages = ",".join(str(source.get("page")) for source in order.get("sources") or [])
        allocation["notes"].append(
            f"Proj.nr. {order['project_code']}; Order {order.get('order_id') or '—'}; стр. {pages}"
        )
    if actual != expected:
        raise ValueError("Суммы заказов должны точно совпасть с net, VAT и gross фактуры.")
    return [{
        "object_id": item["object_id"],
        "net_amount": decimal_text(item["net_amount"]),
        "vat_amount": decimal_text(item["vat_amount"]),
        "gross_amount": decimal_text(item["gross_amount"]),
        "note": "; ".join(item["notes"])[:500],
    } for item in grouped.values()]


def invoice_net_schema_ready(connection):
    document_columns = {row[1] for row in connection.execute("PRAGMA table_info(expense_documents)")}
    allocation_columns = {row[1]: row for row in connection.execute("PRAGMA table_info(expense_allocations)")}
    return (
        {"rounding_amount", "amount_due", "allocation_basis"}.issubset(document_columns)
        and {"destination", "expense_category"}.issubset(allocation_columns)
        and not allocation_columns["allocated_gross"][3]
    )


def migrate_invoice_net_schema(connection):
    if invoice_net_schema_ready(connection):
        return
    if connection.in_transaction:
        raise ValueError("Миграция требует отдельной транзакции и предварительной резервной копии.")
    try:
        connection.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(expense_documents)")}
        for name, definition in (("rounding_amount", "TEXT"), ("amount_due", "TEXT"),
                                 ("allocation_basis", "TEXT NOT NULL DEFAULT 'gross'")):
            if name not in columns:
                connection.execute(f"ALTER TABLE expense_documents ADD COLUMN {name} {definition}")
        connection.execute("ALTER TABLE expense_allocations RENAME TO expense_allocations_before_net")
        connection.execute("""
            CREATE TABLE expense_allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES expense_documents(id) ON DELETE CASCADE,
                object_id INTEGER REFERENCES meta_objects(id) ON DELETE RESTRICT,
                allocated_gross TEXT,
                allocated_net TEXT,
                allocated_vat TEXT,
                allocation_note TEXT NOT NULL DEFAULT '',
                destination TEXT NOT NULL DEFAULT 'object',
                expense_category TEXT NOT NULL DEFAULT ''
            )
        """)
        connection.execute("""
            INSERT INTO expense_allocations (
                id, document_id, object_id, allocated_gross, allocated_net, allocated_vat,
                allocation_note, destination
            ) SELECT id, document_id, object_id, allocated_gross, allocated_net, allocated_vat,
                     allocation_note, CASE WHEN object_id IS NULL THEN 'company' ELSE 'object' END
              FROM expense_allocations_before_net
        """)
        connection.execute("DROP TABLE expense_allocations_before_net")
        connection.execute("CREATE INDEX idx_expense_allocations_object ON expense_allocations(object_id)")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.IntegrityError("Нарушены внешние ключи после миграции распределений.")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def net_invoice_issues(fields, allocations):
    issues = due_date_issues(fields)
    non_invoice_type = fields.get("non_invoice_document_type") or non_invoice_document_type(
        str(fields.get("raw_text") or fields.get("recognized_text") or "")
    )
    if non_invoice_type == "order_confirmation":
        issues.append("Подтверждение заказа нельзя подтверждать как расход; загрузите фактуру или чек.")
    elif non_invoice_type == "tax_decision":
        issues.append("Решение о транспортном налоге нельзя подтверждать как фактуру.")
    context = invoice_context(str(fields.get("raw_text") or fields.get("recognized_text") or ""))
    context["prepaid"] = context["prepaid"] or fields.get("prepaid") is True
    context["reverse_charge"] = context["reverse_charge"] or fields.get("reverse_charge") is True
    if context["prepaid"]:
        issues.append("Предоплаченный чек или остаток после оплаты: подтверждение этого сценария пока не поддерживается; сохраните черновик. Итог документа и остаток к оплате не взаимозаменяемы.")
    if context["reverse_charge"] and fields.get("tax_reviewed") is not True:
        issues.append("Обратное начисление НДС: сверьте налоговый режим с оригиналом и отметьте проверку; неизвестный НДС не вычисляется.")
    values = {key: parse_invoice_money(fields.get(key)) for key in
              ("net_amount", "vat_amount", "rounding_amount", "amount_due")}
    if fields.get("rounding_amount") in (None, ""):
        values["rounding_amount"] = Decimal(0)
    for key, label in (("net_amount", "сумму без НДС"), ("vat_amount", "НДС фактуры"),
                       ("rounding_amount", "округление"), ("amount_due", "сумму к оплате")):
        if values[key] is None:
            issues.append(f"Укажите {label}.")
    if all(value is not None for value in values.values()) and not context["prepaid"]:
        if values["net_amount"] + values["vat_amount"] + values["rounding_amount"] != values["amount_due"]:
            issues.append("Сумма без НДС + НДС + округление не равна сумме к оплате.")
    allocated = Decimal(0)
    for index, row in enumerate(allocations, 1):
        amount = parse_invoice_money(row.get("net_amount"))
        if amount is None:
            issues.append(f"Строка {index}: укажите сумму без НДС.")
        else:
            allocated += amount
            total = values["net_amount"]
            if total is not None and amount and total and (amount > 0) != (total > 0):
                issues.append(f"Строка {index}: знак суммы не соответствует фактуре.")
        destination = row.get("destination")
        if destination not in {"object", "company"} or destination == "object" and row.get("object_id") is None:
            issues.append(f"Строка {index}: выберите назначение расходов.")
        if row.get("ambiguous"):
            issues.append(f"Строка {index}: сверьте неоднозначный итог заказа с оригиналом.")
    if values["net_amount"] is not None and allocated != values["net_amount"]:
        issues.append("Распределено без НДС не совпадает с суммой без НДС фактуры.")
    part_vat = [parse_invoice_money(row.get("vat_amount")) for row in allocations]
    if allocations and values["vat_amount"] is not None and all(value is not None for value in part_vat):
        if sum(part_vat, Decimal(0)) != values["vat_amount"]:
            issues.append("НДС распределённых частей не совпадает с НДС фактуры.")
    return issues


def allocation_vat_from_items(pages, orders, invoice_net, invoice_vat):
    expected = {str(row.get("order_id") or ""): row for row in orders}
    if not orders or "" in expected or len(expected) != len(orders):
        raise ValueError("Для расчёта НДС нужны однозначные номера исходных заказов.")
    totals = {key: {"net": Decimal(0), "vat": Decimal(0), "items": []} for key in expected}
    current_order = None
    for page in pages:
        rows = invoice_layout_rows(page.get("elements") or [])
        lines, sources = invoice_order_lines(rows)
        events = []
        for line, source in zip(lines, sources):
            match = re.match(r"^(?:Sälj\s*\.\s*ordnr\s*\.?|Order\s*(?:nr\.?|no\.?|number|#)|Ordernummer):\s*(\S+)", line, re.IGNORECASE)
            if match:
                events.append((source["box"][1], match.group(1)))
            elif re.match(r"Summa\s+order", line, re.IGNORECASE):
                events.append((source["box"][1], None))
        events.sort(key=lambda event: event[0])
        event_index = 0
        columns = None
        for row in rows:
            top = min(cell["y0"] for cell in row)
            while event_index < len(events) and events[event_index][0] <= top + 2:
                current_order = events[event_index][1]
                event_index += 1
            vat_headers = [index for index, cell in enumerate(row)
                           if re.fullmatch(r"(?:Moms|VAT)\s*%", cell["text"], re.IGNORECASE)]
            sum_headers = [index for index, cell in enumerate(row)
                           if re.fullmatch(r"Summa|Amount|Belopp", cell["text"], re.IGNORECASE)]
            if vat_headers and sum_headers and len(row) > 2:
                vat_column, sum_column = vat_headers[0], sum_headers[-1]

                def bounds(index):
                    left = (row[index - 1]["x0"] + row[index]["x0"]) / 2 if index else row[index]["x0"] - 20
                    right = (row[index]["x0"] + row[index + 1]["x0"]) / 2 if index + 1 < len(row) else float("inf")
                    return left, right

                columns = (bounds(0), bounds(vat_column), bounds(sum_column))
                continue
            if columns is None or current_order is None:
                continue
            item_cells = [cell for cell in row if columns[0][0] <= cell["x0"] < columns[0][1]
                          and re.fullmatch(r"\d+", cell["text"])]
            if not item_cells:
                continue
            rates = [cell for cell in row if columns[1][0] <= cell["x0"] < columns[1][1]]
            amounts = [cell for cell in row if columns[2][0] <= cell["x0"] < columns[2][1]]
            if current_order not in totals or len(rates) != 1 or len(amounts) != 1:
                raise ValueError("Не все позиции имеют однозначный заказ, ставку и сумму; НДС частей не вычислен.")
            rate = parse_invoice_money(re.sub(r"\s*%$", "", rates[0]["text"]))
            net = parse_invoice_money(amounts[0]["text"])
            if rate is None or not Decimal(0) <= rate <= Decimal(100) or net is None:
                raise ValueError("Не удалось достоверно прочитать ставку или net позиции.")
            vat = (net * rate / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)
            bucket = totals[current_order]
            bucket["net"] += net
            bucket["vat"] += vat
            bucket["items"].append({"page": page.get("page", 1), "item": item_cells[0]["text"],
                                    "net_amount": decimal_text(net), "vat_rate": decimal_text(rate),
                                    "vat_amount": decimal_text(vat)})
    for identifier, row in expected.items():
        if not totals[identifier]["items"] or totals[identifier]["net"] != parse_invoice_money(row.get("net_amount")):
            raise ValueError("Позиции не сходятся с net исходного заказа; требуется проверка НДС частей.")
    if (sum((row["net"] for row in totals.values()), Decimal(0)) != parse_invoice_money(invoice_net)
            or sum((row["vat"] for row in totals.values()), Decimal(0)) != parse_invoice_money(invoice_vat)):
        raise ValueError("Построчный НДС или net не совпадает с итогом фактуры; автоматическое распределение отклонено.")
    return [{**row, "vat_amount": decimal_text(totals[str(row["order_id"])]["vat"]),
             "gross_amount": decimal_text(totals[str(row["order_id"])]["net"] + totals[str(row["order_id"])]["vat"]),
             "vat_items": totals[str(row["order_id"])]["items"]} for row in orders]


def normalize_net_allocations(allocations):
    grouped = {}
    for row in allocations:
        destination = row.get("destination") or "unselected"
        object_id = row.get("object_id") if destination == "object" else None
        category = str(row.get("expense_category") or "").strip()[:100]
        key = (destination, object_id, category)
        net = parse_invoice_money(row.get("net_amount"))
        vat = parse_invoice_money(row.get("vat_amount"))
        if key not in grouped:
            grouped[key] = {"object_id": object_id, "destination": destination, "expense_category": category,
                            "net_amount": Decimal(0), "vat_amount": Decimal(0), "note": "",
                            "net_known": True, "vat_known": True}
        item = grouped[key]
        item["net_known"] &= net is not None
        item["vat_known"] &= vat is not None
        item["net_amount"] += net or Decimal(0)
        item["vat_amount"] += vat or Decimal(0)
        marker = str(row.get("original_marker") or row.get("project_code") or row.get("order_id") or "")
        item["note"] = (item["note"] + "; " + marker).strip("; ")[:500]
    result = []
    for item in grouped.values():
        net = item.pop("net_amount") if item.pop("net_known") else None
        vat = item.pop("vat_amount") if item.pop("vat_known") else None
        result.append({**item, "net_amount": decimal_text(net), "vat_amount": decimal_text(vat),
                       "gross_amount": decimal_text(net + vat) if net is not None and vat is not None else None})
    return result


def find_document_duplicates(
    conn: sqlite3.Connection,
    fields: dict[str, Any],
    file_sha256: str | None = None,
    exclude_document_id: int | None = None,
) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    clauses = []
    params: list[Any] = []
    if file_sha256:
        clauses.append("d.file_sha256 = ?")
        params.append(file_sha256)
    if fields.get("invoice_number") and fields.get("document_date") and fields.get("gross_amount"):
        clauses.append("(lower(COALESCE(s.name, d.supplier_name, '')) = lower(?) AND d.invoice_number = ? AND d.document_date = ? AND d.gross_amount = ? AND d.currency_code = ?)")
        params.extend((
            str(fields.get("supplier_name") or ""), fields["invoice_number"],
            fields["document_date"], decimal_text(fields["gross_amount"]),
            str(fields.get("currency_code") or "UNKNOWN").upper(),
        ))
    if not clauses:
        return []
    where = " OR ".join(clauses)
    query = """
        SELECT d.id, d.invoice_number, d.document_date, d.gross_amount, d.currency_code,
               COALESCE(s.name, d.supplier_name, '') AS supplier_name
        FROM supplier_documents d
        LEFT JOIN expense_suppliers s ON s.id = d.supplier_id
        WHERE (""" + where + ")"
    if exclude_document_id is not None:
        query += " AND d.id <> ?"
        params.append(int(exclude_document_id))
    query += " ORDER BY d.id"
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def save_supplier_document(
    conn: sqlite3.Connection,
    storage_root: str | Path,
    fields: dict[str, Any],
    allocations: list[dict[str, Any]],
    source_path: str | Path | None = None,
    verification_status: str = "draft",
    allow_duplicate: bool = False,
) -> int:
    if verification_status not in VALID_REVIEW_STATUSES:
        raise ValueError("Недопустимый статус проверки документа.")
    document_type = str(fields.get("document_type") or "invoice")
    if document_type not in VALID_DOCUMENT_TYPES:
        raise ValueError("Недопустимый тип документа.")
    if verification_status == "confirmed":
        missing = [
            label for key, label in (
                ("supplier_name", "поставщик"), ("document_date", "дата документа"),
                ("currency_code", "валюта"), ("net_amount", "сумма без НДС"),
                ("vat_amount", "НДС (укажите сумму, в том числе 0)"),
                ("gross_amount", "итоговая сумма"),
            ) if fields.get(key) in (None, "", "UNKNOWN")
        ]
        if missing:
            raise ValueError("Для подтверждения заполните: " + ", ".join(missing) + ".")
        if fields.get("currency_code") == "UNKNOWN":
            raise ValueError("Для подтверждения выберите валюту вручную.")
        net = parse_decimal(fields["net_amount"])
        vat = parse_decimal(fields["vat_amount"])
        gross = parse_decimal(fields["gross_amount"])
        if net is None or vat is None or gross is None:
            raise ValueError("Суммы должны быть корректными десятичными значениями.")
        if abs(net + vat - gross) > Decimal("0.02"):
            raise ValueError("Нетто + НДС не сходятся с итоговой суммой; исправьте документ перед подтверждением.")
        if allocations:
            valid, message = validate_allocations(gross, allocations)
            if not valid:
                raise ValueError(message)

    file_hash = sha256_file(source_path) if source_path else None
    duplicates = find_document_duplicates(conn, fields, file_hash)
    if duplicates and not allow_duplicate:
        raise DuplicateDocumentError(duplicates)

    timestamp = datetime.now(timezone.utc).isoformat()
    supplier_id = None
    supplier_name = str(fields.get("supplier_name") or "").strip()
    supplier_org = str(fields.get("supplier_org_number") or "").strip() or None
    supplier_vat = str(fields.get("supplier_vat_number") or "").strip() or None
    object_ids = [int(item["object_id"]) for item in allocations if item.get("object_id") is not None]
    object_names: list[str] = []
    if object_ids:
        placeholders = ",".join("?" for _ in object_ids)
        object_names = [row[0] for row in conn.execute(
            f"SELECT name FROM objects WHERE id IN ({placeholders}) ORDER BY name", object_ids
        ).fetchall()]

    destination: Path | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not allow_duplicate:
            concurrent_duplicates = find_document_duplicates(conn, fields, file_hash)
            if concurrent_duplicates:
                raise DuplicateDocumentError(concurrent_duplicates)
        if supplier_name:
            row = conn.execute(
                "SELECT id FROM expense_suppliers WHERE name = ? COLLATE NOCASE AND COALESCE(org_number, '') = COALESCE(?, '') ORDER BY id LIMIT 1",
                (supplier_name, supplier_org),
            ).fetchone()
            if row:
                supplier_id = int(row[0])
                conn.execute(
                    "UPDATE expense_suppliers SET vat_number = COALESCE(?, vat_number) WHERE id = ?",
                    (supplier_vat, supplier_id),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO expense_suppliers (name, org_number, vat_number, created_at) VALUES (?, ?, ?, ?)",
                    (supplier_name, supplier_org, supplier_vat, timestamp),
                )
                supplier_id = int(cursor.lastrowid)

        cursor = conn.execute("""
            INSERT INTO supplier_documents (
                expense_id, supplier_id, supplier_name, supplier_org_number, supplier_vat_number,
                original_name, file_sha256, document_type, invoice_number, payment_reference,
                document_date, due_date, currency_code, net_amount, vat_amount, gross_amount,
                amount_due, expense_category, document_comment, is_reverse_charge, verification_status,
                payment_status, recognized_text, review_reasons, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            fields.get("expense_id"), supplier_id, supplier_name or None, supplier_org, supplier_vat,
            Path(source_path).name if source_path else None, file_hash, document_type,
            fields.get("invoice_number"), fields.get("payment_reference"),
            fields.get("document_date"), fields.get("due_date"),
            str(fields.get("currency_code") or "UNKNOWN").upper(),
            decimal_text(fields.get("net_amount")), decimal_text(fields.get("vat_amount")),
            decimal_text(fields.get("gross_amount")), decimal_text(fields.get("amount_due")),
            str(fields.get("expense_category") or "Прочее"), str(fields.get("document_comment") or ""),
            int(bool(fields.get("reverse_charge"))),
            verification_status, str(fields.get("payment_status") or "unknown"),
            str(fields.get("raw_text") or ""), "\n".join(fields.get("review_reasons") or []),
            timestamp, timestamp,
        ))
        document_id = int(cursor.lastrowid)
        for vat_line in fields.get("vat_lines") or []:
            conn.execute(
                "INSERT INTO supplier_document_vat_lines (document_id, vat_rate, taxable_base, vat_amount, note) VALUES (?, ?, ?, ?, ?)",
                (document_id, str(vat_line.get("rate") or ""), decimal_text(vat_line.get("taxable_base")), decimal_text(vat_line.get("amount")) or "0.00", str(vat_line.get("note") or "")),
            )
        for allocation in allocations:
            conn.execute(
                "INSERT INTO supplier_document_allocations (document_id, object_id, allocated_net, allocated_vat, allocated_gross, allocation_note) VALUES (?, ?, ?, ?, ?, ?)",
                (document_id, allocation.get("object_id"), decimal_text(allocation.get("net_amount")), decimal_text(allocation.get("vat_amount")), decimal_text(allocation.get("gross_amount")) or "0.00", str(allocation.get("note") or "")),
            )
        if source_path:
            destination, verified_hash = store_original_file(source_path, storage_root, document_id, object_names)
            conn.execute(
                "UPDATE supplier_documents SET stored_path = ?, file_sha256 = ?, updated_at = ? WHERE id = ?",
                (str(destination), verified_hash, datetime.now(timezone.utc).isoformat(), document_id),
            )
        conn.commit()
        return document_id


    except Exception:
        if conn.in_transaction:
            conn.rollback()
        if destination:
            destination.unlink(missing_ok=True)
        raise


def record_document_payment(
    conn: sqlite3.Connection,
    document_id: int,
    paid_at: str,
    amount: Decimal | str,
    reference: str = "",
) -> str:
    payment_amount = parse_decimal(amount)
    if payment_amount is None or payment_amount <= 0:
        raise ValueError("Сумма оплаты должна быть больше нуля.")
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN IMMEDIATE")
    try:
        document = conn.execute(
            "SELECT currency_code, amount_due, gross_amount FROM supplier_documents WHERE id = ?",
            (int(document_id),),
        ).fetchone()
        if document is None:
            raise ValueError("Документ не найден.")
        paid_before = sum((parse_decimal(row[0]) or Decimal(0) for row in conn.execute(
            "SELECT amount FROM supplier_document_payments WHERE document_id = ?", (int(document_id),)
        )), Decimal(0))
        due = parse_decimal(document["amount_due"]) or parse_decimal(document["gross_amount"])
        paid_total = paid_before + payment_amount
        if due is not None and paid_total - due > Decimal("0.02"):
            raise ValueError("Сумма платежей превысит сумму документа.")
        timestamp = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO supplier_document_payments (document_id, paid_at, amount, currency_code, reference, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (int(document_id), paid_at, decimal_text(payment_amount), document["currency_code"], reference.strip(), timestamp),
        )
        if due is None:
            status = "partially_paid"
        elif paid_total >= due - Decimal("0.02"):
            status = "paid"
        else:
            status = "partially_paid"
        conn.execute(
            "UPDATE supplier_documents SET payment_status = ?, updated_at = ? WHERE id = ?",
            (status, timestamp, int(document_id)),
        )
        conn.commit()
        return status
    except Exception:
        conn.rollback()
        raise


def _normalize_match(value: str | None) -> str:
    value = unicodedata.normalize("NFKD", value or "").casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", value)


def match_objects(document_text: str, objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_document = _normalize_match(document_text)
    matches = []
    for item in objects:
        candidates = []
        marking = str(item.get("markning") or "").strip()
        if marking and marking not in {"*", "**", "***", "****"} and _normalize_match(marking) in normalized_document:
            normalized_marking = _normalize_match(marking)
            match_field = None
            for line in (document_text or "").splitlines():
                if normalized_marking not in _normalize_match(line):
                    continue
                field_match = re.match(r"\s*(Projekt|Märkn(?:ing)?|Markning|Er\s+referens|Project|Your\s+reference)\b", line, re.IGNORECASE)
                if field_match:
                    match_field = field_match.group(1)
                    break
            reason = (
                f"Найден код {marking} в поле {match_field}"
                if match_field else f"Найден код {marking} в тексте документа"
            )
            candidates.append((100, reason))
        for alias in item.get("aliases", []):
            alias_text = str(alias.get("alias") if isinstance(alias, dict) else alias)
            normalized_alias = _normalize_match(alias_text)
            if len(normalized_alias) >= 4 and normalized_alias in normalized_document:
                candidates.append((90, f"Найден альтернативный вариант названия: {alias_text}"))
        name = str(item.get("name") or "").strip()
        normalized_name = _normalize_match(name)
        if len(normalized_name) >= 6 and normalized_name in normalized_document:
            candidates.append((80, f"Найдено название объекта: {name}"))
        if candidates:
            best = max(candidates, key=lambda result: result[0])
            matches.append({"object_id": item["id"], "name": name, "reason": best[1], "score": best[0]})
    return sorted(matches, key=lambda item: (-item["score"], item["name"].casefold()))


def validate_allocations(
    total_gross: Decimal | str,
    allocations: list[dict[str, Any]],
    allow_partial: bool = False,
) -> tuple[bool, str]:
    total = parse_decimal(total_gross)
    if total is None:
        return False, "Сначала укажите общую сумму документа."
    if not allocations and not allow_partial:
        return True, "Документ сохранится без объекта и может быть распределён позже."
    total = total.quantize(CENT, rounding=ROUND_HALF_UP)
    allocated = Decimal(0)
    for item in allocations:
        amount = parse_decimal(item.get("gross_amount"))
        if amount is None:
            return False, "У каждой строки распределения должна быть сумма."
        amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)
        if amount and (total == 0 or (amount > 0) != (total > 0)):
            return False, "Сумма распределения должна иметь тот же знак, что и сумма документа."
        allocated += amount
    allocated = allocated.quantize(CENT, rounding=ROUND_HALF_UP)
    if abs(allocated) > abs(total):
        return False, f"Распределено {decimal_text(allocated)}, что больше суммы документа {decimal_text(total)}."
    remaining = (total - allocated).quantize(CENT, rounding=ROUND_HALF_UP)
    if not allow_partial and remaining:
        return False, f"Сумма распределения {decimal_text(allocated)} не совпадает с документом {decimal_text(total)}."
    if remaining:
        return True, f"Не распределено: {decimal_text(remaining)}."
    return True, "Документ распределён полностью."


def allocation_breakdown(
    total_gross: Decimal | str,
    total_net: Decimal | str | None,
    total_vat: Decimal | str | None,
    allocations: list[dict[str, Any]],
) -> dict[str, Any]:
    valid, message = validate_allocations(total_gross, allocations, allow_partial=True)
    if not valid:
        raise ValueError(message)

    gross = (parse_decimal(total_gross) or Decimal(0)).quantize(CENT, rounding=ROUND_HALF_UP)
    net = parse_decimal(total_net)
    vat = parse_decimal(total_vat)
    net = net.quantize(CENT, rounding=ROUND_HALF_UP) if net is not None else None
    vat = vat.quantize(CENT, rounding=ROUND_HALF_UP) if vat is not None else None
    gross_rows = [
        (parse_decimal(item.get("gross_amount")) or Decimal(0)).quantize(CENT, rounding=ROUND_HALF_UP)
        for item in allocations
    ]
    assigned_gross = sum(gross_rows, Decimal(0))
    fully_allocated = assigned_gross == gross
    assigned_net = Decimal(0)
    assigned_vat = Decimal(0)
    rows = []

    for index, (allocation, row_gross) in enumerate(zip(allocations, gross_rows)):
        is_last = index == len(gross_rows) - 1
        if net is None:
            row_net = None
        elif fully_allocated and is_last:
            row_net = net - assigned_net
        elif gross:
            row_net = (net * abs(row_gross / gross)).quantize(CENT, rounding=ROUND_HALF_UP)
            remaining_net = net - assigned_net
            row_net = min(max(row_net, Decimal(0)), max(remaining_net, Decimal(0))) if net >= 0 else max(min(row_net, Decimal(0)), min(remaining_net, Decimal(0)))
        else:
            row_net = Decimal(0)
        if vat is None:
            row_vat = None
        elif fully_allocated and is_last:
            row_vat = vat - assigned_vat
        elif net is not None and net + vat == gross and row_net is not None:
            row_vat = row_gross - row_net
        elif gross:
            row_vat = (vat * abs(row_gross / gross)).quantize(CENT, rounding=ROUND_HALF_UP)
            remaining_vat = vat - assigned_vat
            row_vat = min(max(row_vat, Decimal(0)), max(remaining_vat, Decimal(0))) if vat >= 0 else max(min(row_vat, Decimal(0)), min(remaining_vat, Decimal(0)))
        else:
            row_vat = Decimal(0)

        rows.append({
            **allocation,
            "gross_amount": row_gross,
            "net_amount": row_net,
            "vat_amount": row_vat,
        })
        if row_net is not None:
            assigned_net += row_net
        if row_vat is not None:
            assigned_vat += row_vat

    return {
        "allocations": rows,
        "allocated_gross": assigned_gross,
        "unallocated_gross": gross - assigned_gross,
        "allocated_net": assigned_net if net is not None else None,
        "unallocated_net": net - assigned_net if net is not None else None,
        "allocated_vat": assigned_vat if vat is not None else None,
        "unallocated_vat": vat - assigned_vat if vat is not None else None,
        "fully_allocated": fully_allocated,
    }


def is_material_expense_category(category: str | None) -> bool:
    normalized = unicodedata.normalize("NFKD", category or "").casefold()
    tokens = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    return any(token.startswith(("material", "материал")) for token in tokens)


def get_object_expense_summary(
    conn: sqlite3.Connection,
    object_id: int,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    documents = conn.execute("""
        SELECT DISTINCT d.*
        FROM supplier_documents d
        JOIN supplier_document_allocations a ON a.document_id = d.id
        WHERE a.object_id = ?
        ORDER BY d.document_date, d.id
    """, (int(object_id),)).fetchall()

    def empty_totals() -> dict[str, Any]:
        return {
            "net": Decimal(0), "vat": Decimal(0), "gross": Decimal(0),
            "paid": Decimal(0), "remaining": Decimal(0),
            "materials_gross": Decimal(0), "other_gross": Decimal(0),
            "documents": set(), "unknown_payment_documents": set(),
        }

    lifetime: dict[str, dict[str, Any]] = {}
    period: dict[str, dict[str, Any]] = {}
    draft_lifetime: set[int] = set()
    review_lifetime: set[int] = set()
    draft_period: set[int] = set()
    review_period: set[int] = set()

    for document in documents:
        document_id = int(document["id"])
        document_date = str(document["document_date"] or "")
        in_period = bool(
            date_from and date_to and document_date
            and date_from <= document_date <= date_to
        )
        review_status = str(document["verification_status"])
        if review_status == "draft":
            draft_lifetime.add(document_id)
            if in_period:
                draft_period.add(document_id)
        elif review_status == "requires_review":
            review_lifetime.add(document_id)
            if in_period:
                review_period.add(document_id)
        if review_status != "confirmed":
            continue

        allocations = conn.execute("""
            SELECT id, object_id, allocated_net, allocated_vat, allocated_gross
            FROM supplier_document_allocations
            WHERE document_id = ? ORDER BY id
        """, (document_id,)).fetchall()
        document_gross = parse_decimal(document["gross_amount"])
        document_net = parse_decimal(document["net_amount"])
        document_vat = parse_decimal(document["vat_amount"])
        document_due = parse_decimal(document["amount_due"]) or document_gross
        paid_total = sum((parse_decimal(row[0]) or Decimal(0) for row in conn.execute(
            "SELECT amount FROM supplier_document_payments WHERE document_id = ?", (document_id,)
        ).fetchall()), Decimal(0))
        total_allocated = sum((parse_decimal(item["allocated_gross"]) or Decimal(0) for item in allocations), Decimal(0))
        denominator = document_gross if document_gross not in (None, Decimal(0)) else total_allocated
        if denominator == 0:
            continue

        allocation_breakdown_result = allocation_breakdown(
            document_gross or total_allocated,
            document_net,
            document_vat,
            [
                {
                    "object_id": allocation["object_id"],
                    "gross_amount": allocation["allocated_gross"],
                    "net_amount": allocation["allocated_net"],
                    "vat_amount": allocation["allocated_vat"],
                }
                for allocation in allocations
            ],
        )
        shares: list[dict[str, Any]] = []
        net_assigned = vat_assigned = paid_assigned = due_assigned = Decimal(0)
        for index, allocation in enumerate(allocations):
            breakdown_share = allocation_breakdown_result["allocations"][index]
            allocated_gross = breakdown_share["gross_amount"]
            is_last = index == len(allocations) - 1
            ratio = allocated_gross / denominator
            allocated_net = parse_decimal(allocation["allocated_net"])
            if allocated_net is None:
                allocated_net = breakdown_share["net_amount"]
            allocated_vat = parse_decimal(allocation["allocated_vat"])
            if allocated_vat is None:
                allocated_vat = breakdown_share["vat_amount"]
            paid_share = (
                paid_total - paid_assigned
                if is_last and allocation_breakdown_result["fully_allocated"]
                else (paid_total * ratio).quantize(CENT, rounding=ROUND_HALF_UP)
            )
            due_share = (
                document_due - due_assigned
                if is_last and allocation_breakdown_result["fully_allocated"] and document_due is not None
                else (document_due * ratio).quantize(CENT, rounding=ROUND_HALF_UP)
                if document_due is not None else allocated_gross
            )
            shares.append({
                "object_id": int(allocation["object_id"]) if allocation["object_id"] is not None else None,
                "gross": allocated_gross, "net": allocated_net or Decimal(0),
                "vat": allocated_vat or Decimal(0), "paid": paid_share,
                "remaining": max(Decimal(0), due_share - paid_share),
            })
            net_assigned += allocated_net or Decimal(0)
            vat_assigned += allocated_vat or Decimal(0)
            paid_assigned += paid_share
            due_assigned += due_share

        for share in shares:
            if share["object_id"] != int(object_id):
                continue
            currency = str(document["currency_code"] or "UNKNOWN").upper()
            bucket = lifetime.setdefault(currency, empty_totals())
            bucket["net"] += share["net"]
            bucket["vat"] += share["vat"]
            bucket["gross"] += share["gross"]
            bucket["paid"] += share["paid"]
            bucket["remaining"] += share["remaining"]
            bucket["documents"].add(document_id)
            if str(document["payment_status"]) == "unknown":
                bucket["unknown_payment_documents"].add(document_id)
            if is_material_expense_category(document["expense_category"]):
                bucket["materials_gross"] += share["gross"]
            else:
                bucket["other_gross"] += share["gross"]
            if in_period:
                period_bucket = period.setdefault(currency, empty_totals())
                period_bucket["net"] += share["net"]
                period_bucket["vat"] += share["vat"]
                period_bucket["gross"] += share["gross"]
                period_bucket["paid"] += share["paid"]
                period_bucket["remaining"] += share["remaining"]
                if is_material_expense_category(document["expense_category"]):
                    period_bucket["materials_gross"] += share["gross"]
                else:
                    period_bucket["other_gross"] += share["gross"]
                period_bucket["documents"].add(document_id)
                if str(document["payment_status"]) == "unknown":
                    period_bucket["unknown_payment_documents"].add(document_id)

    return {
        "lifetime": lifetime,
        "period": period,
        "drafts_lifetime": len(draft_lifetime),
        "reviews_lifetime": len(review_lifetime),
        "drafts_period": len(draft_period),
        "reviews_period": len(review_period),
    }


def pdf_display_box(bounds, crop, rotation):
    left, bottom, right, top = bounds
    crop_left, crop_bottom, crop_right, crop_top = crop
    if rotation == 90:
        coordinates = (bottom - crop_bottom, left - crop_left, top - crop_bottom, right - crop_left)
    elif rotation == 180:
        coordinates = (crop_right - right, bottom - crop_bottom, crop_right - left, top - crop_bottom)
    elif rotation == 270:
        coordinates = (crop_top - top, crop_right - right, crop_top - bottom, crop_right - left)
    else:
        coordinates = (left - crop_left, crop_top - top, right - crop_left, crop_top - bottom)
    return dict(zip(("x0", "y0", "x1", "y1"), coordinates))


def pdf_page_elements(page):
    crop = page.get_cropbox()
    rotation = page.get_rotation()
    text_page = page.get_textpage()
    elements = []
    word = None
    try:
        for index in range(text_page.count_chars()):
            character = text_page.get_text_range(index, 1)
            if not character or character.isspace():
                if word:
                    elements.append(word)
                    word = None
                continue
            box = pdf_display_box(text_page.get_charbox(index), crop, rotation)
            if word and (box["y0"] > word["y1"] + 4 or box["y1"] < word["y0"] - 4
                         or box["x0"] - word["x1"] > 12):
                elements.append(word)
                word = None
            if word is None:
                word = {"text": character, **box}
            else:
                word["text"] += character
                word["y0"] = min(word["y0"], box["y0"])
                word["x1"] = max(word["x1"], box["x1"])
                word["y1"] = max(word["y1"], box["y1"])
        if word:
            elements.append(word)
    finally:
        text_page.close()
    return tuple(elements)


def ocr_page_elements(detections):
    return tuple({
        "text": str(detection[1]),
        "x0": min(point[0] for point in detection[0]),
        "y0": min(point[1] for point in detection[0]),
        "x1": max(point[0] for point in detection[0]),
        "y1": max(point[1] for point in detection[0]),
    } for detection in detections or [] if detection[0])


def extract_local_document(path: str | Path, ocr_engine: Any = None) -> ExtractionResult:
    source = Path(path)
    suffix = source.suffix.casefold()
    if suffix not in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}:
        raise DocumentExtractionError("Поддерживаются PDF и распространённые форматы изображений.")

    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(source))
        if reader.is_encrypted:
            raise DocumentExtractionError("Зашифрованный PDF нельзя прочитать без пароля.")
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(source))
        try:
            page_elements = [pdf_page_elements(pdf[index]) for index in range(len(pdf))]
        finally:
            pdf.close()
        missing_page_indexes = [index for index, text in enumerate(pages)
                    if len(re.findall(r"\w", text)) < 12 or len(re.findall(r"\w+", text)) < 3
                    or text.count("\ufffd") > len(text) / 10]
        confidence_values = []
        if missing_page_indexes:
            try:
                import numpy as np
                import pypdfium2 as pdfium
                if ocr_engine is None:
                    from rapidocr_onnxruntime import RapidOCR
            except ImportError as error:
                raise DocumentExtractionError(f"Локальный OCR недоступен: {error.name}.") from error
            engine = ocr_engine or RapidOCR()
            pdf = pdfium.PdfDocument(str(source))
            try:
                for index in missing_page_indexes:
                    image = pdf[index].render(scale=2).to_pil().convert("RGB")
                    result, _ = engine(np.asarray(image))
                    lines = []
                    for detection in result or []:
                        lines.append(str(detection[1]))
                        if len(detection) > 2:
                            confidence_values.append(float(detection[2]))
                    pages[index] = "\n".join(lines)
                    page_elements[index] = ocr_page_elements(result)
            finally:
                pdf.close()
            method = "PDF-текст + локальный OCR" if len(missing_page_indexes) < len(pages) else "локальный OCR"
        else:
            method = "текст PDF"
        text = "\n\n".join(page for page in pages if page)
        confidence = sum(confidence_values) / len(confidence_values) if confidence_values else (1.0 if text else 0.0)
        return ExtractionResult(text=text, method=method, confidence=confidence, pages=tuple(pages), page_elements=tuple(page_elements))

    try:
        import numpy as np
        from PIL import Image
        if ocr_engine is None:
            from rapidocr_onnxruntime import RapidOCR
    except ImportError as error:
        raise DocumentExtractionError(f"Локальный OCR недоступен: {error.name}.") from error
    engine = ocr_engine or RapidOCR()
    with Image.open(source) as image:
        result, _ = engine(np.asarray(image.convert("RGB")))
    lines = []
    confidences = []
    for detection in result or []:
        lines.append(str(detection[1]))
        if len(detection) > 2:
            confidences.append(float(detection[2]))
    text = "\n".join(lines)
    confidence = sum(confidences) / len(confidences) if confidences else (0.0 if not text else None)
    return ExtractionResult(text=text, method="локальный OCR", confidence=confidence, pages=(text,), page_elements=(ocr_page_elements(result),))


def create_migration_backup(database_path: str | Path, backup_dir: str | Path) -> Path:
    source_path = Path(database_path).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    destination_dir = Path(backup_dir).resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    destination_path = destination_dir / f"{source_path.stem}_before_{MIGRATION_ID}_{stamp}.db"
    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    destination = sqlite3.connect(str(destination_path))
    try:
        source.backup(destination)
        result = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise sqlite3.DatabaseError(f"Backup integrity check failed: {result}")
    except Exception:
        destination.close()
        source.close()
        destination_path.unlink(missing_ok=True)
        raise
    destination.close()
    source.close()
    return destination_path


def apply_expense_documents_migration(database_path: str | Path, backup_dir: str | Path) -> Path | None:
    database_path = Path(database_path).resolve()
    conn = sqlite3.connect(str(database_path), timeout=30)
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        applied = None
        if table:
            applied = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE migration_id = ?", (MIGRATION_ID,)
            ).fetchone()
        if applied:
            return None
    finally:
        conn.close()

    backup_path = create_migration_backup(database_path, backup_dir)
    conn = sqlite3.connect(str(database_path), timeout=30)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT NOT NULL)")
        statements = [
            """CREATE TABLE expense_suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                org_number TEXT,
                vat_number TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(name COLLATE NOCASE, org_number)
            )""",
            """CREATE TABLE supplier_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                expense_id INTEGER UNIQUE REFERENCES company_expenses(id) ON DELETE SET NULL,
                supplier_id INTEGER REFERENCES expense_suppliers(id) ON DELETE SET NULL,
                supplier_name TEXT,
                supplier_org_number TEXT,
                supplier_vat_number TEXT,
                original_name TEXT,
                stored_path TEXT,
                file_sha256 TEXT,
                document_type TEXT NOT NULL CHECK (document_type IN ('invoice','receipt','credit_note','manual')),
                invoice_number TEXT,
                payment_reference TEXT,
                document_date TEXT,
                due_date TEXT,
                currency_code TEXT NOT NULL DEFAULT 'UNKNOWN',
                net_amount TEXT,
                vat_amount TEXT,
                gross_amount TEXT,
                amount_due TEXT,
                expense_category TEXT NOT NULL DEFAULT 'Прочее',
                document_comment TEXT NOT NULL DEFAULT '',
                is_reverse_charge INTEGER NOT NULL DEFAULT 0,
                verification_status TEXT NOT NULL DEFAULT 'draft' CHECK (verification_status IN ('draft','requires_review','confirmed')),
                payment_status TEXT NOT NULL DEFAULT 'unknown' CHECK (payment_status IN ('unknown','unpaid','partially_paid','paid')),
                recognized_text TEXT NOT NULL DEFAULT '',
                review_reasons TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            "CREATE INDEX idx_supplier_documents_date ON supplier_documents(document_date)",
            "CREATE INDEX idx_supplier_documents_hash ON supplier_documents(file_sha256)",
            "CREATE INDEX idx_supplier_documents_invoice ON supplier_documents(supplier_id, invoice_number, document_date, gross_amount)",
            """CREATE TABLE supplier_document_vat_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES supplier_documents(id) ON DELETE CASCADE,
                vat_rate TEXT,
                taxable_base TEXT,
                vat_amount TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT ''
            )""",
            """CREATE TABLE supplier_document_allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES supplier_documents(id) ON DELETE CASCADE,
                object_id INTEGER REFERENCES objects(id) ON DELETE RESTRICT,
                allocated_net TEXT,
                allocated_vat TEXT,
                allocated_gross TEXT NOT NULL,
                allocation_note TEXT NOT NULL DEFAULT ''
            )""",
            "CREATE INDEX idx_supplier_document_allocations_object ON supplier_document_allocations(object_id)",
            """CREATE TABLE supplier_document_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES supplier_documents(id) ON DELETE RESTRICT,
                paid_at TEXT NOT NULL,
                amount TEXT NOT NULL,
                currency_code TEXT NOT NULL,
                reference TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE expense_object_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                object_id INTEGER NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
                alias TEXT NOT NULL,
                alias_type TEXT NOT NULL DEFAULT 'alternate_name',
                UNIQUE(object_id, alias COLLATE NOCASE)
            )""",
        ]
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (migration_id, applied_at, description) VALUES (?, ?, ?)",
            (MIGRATION_ID, datetime.now(timezone.utc).isoformat(), "Local supplier-document register, VAT details, allocations and payments"),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return backup_path


def document_storage_root(configured_root: str | Path | None = None) -> Path:
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    import os
    configured = os.environ.get("SMARTREPORT_DOCUMENTS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".local" / "share"
    return (base / "SmartReport" / "SupplierDocuments").resolve()


def safe_folder_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(char for char in normalized if not unicodedata.combining(char))
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_value).strip("._-")
    return safe[:64] or "object"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def store_original_file(
    source_path: str | Path,
    storage_root: str | Path,
    document_id: int,
    object_names: list[str] | None = None,
) -> tuple[Path, str]:
    source = Path(source_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    objects = object_names or []
    if len(objects) == 1:
        folder = Path(storage_root) / "objects" / safe_folder_name(objects[0])
    elif len(objects) > 1:
        folder = Path(storage_root) / "shared" / "multiple_objects"
    else:
        folder = Path(storage_root) / "shared" / "unallocated"
    folder.mkdir(parents=True, exist_ok=True)
    digest = sha256_file(source)
    destination = folder / f"{int(document_id)}_{safe_folder_name(source.stem)}{source.suffix.casefold()}"
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        shutil.copy2(source, temporary)
        if sha256_file(temporary) != digest:
            raise OSError("Проверка контрольной суммы копии документа не прошла.")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination, digest
