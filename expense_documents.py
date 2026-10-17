from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
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
    pattern = re.compile(rf"^\s*(?:{labels})\s*(?:[:#№])?\s*(.*?)\s*$", re.IGNORECASE)
    for line in lines:
        match = pattern.match(line)
        if match and match.group(1):
            return match.group(1).strip(" :#") or None
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


def parse_document_text(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in (text or "").replace("\r", "\n").split("\n") if line.strip()]
    full_text = "\n".join(lines)
    lower_text = full_text.casefold()
    is_transfer_receipt = bool(re.search(
        r"payment\s+(?:confirmation|receipt|proof)|transfer\s+(?:confirmation|receipt|proof|date|amount)|"
        r"transaction\s+(?:confirmation|receipt|date|amount)|betalningsbekräftelse|betalningsbekraftelse|"
        r"överföringsbekräftelse|overforingsbekraftelse|banköverföring|bankoverforing|bank\s+transfer",
        lower_text,
    ))

    supplier_name = _label_value(lines, r"Leverantör|Leverantor|Supplier|Seller|Säljare|Saljare")
    if is_transfer_receipt and supplier_name and re.sub(r"[^a-z0-9]+", "", supplier_name.casefold()) in {"lunar", "lunarbank"}:
        supplier_name = None
    supplier_org_number = _label_value(lines, r"Leverantörens\s+Org\.?\s*nr|Leverantorens\s+Org\.?\s*nr|Org\.?\s*nr|Organisationsnummer")
    supplier_vat_number = _label_value(lines, r"VAT(?:\s*(?:no\.?|number|nr))?|Momsreg\.?\s*nr")
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
        rate_first = re.search(r"(?:Moms|VAT)?\s*(25|12|6|0)\s*%\s*[:\s]+(-?[\d][\d\s.,']*)", line, re.IGNORECASE)
        amount_first = re.search(r"(-?[\d][\d\s.,']*)\s+(25|12|6|0)\s*%\s*(?:Moms|VAT)", line, re.IGNORECASE)
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
    is_receipt = bool(re.search(r"kvitto|receipt|kassakvitto", lower_text))
    reverse_charge = bool(re.search(r"omvänd\s+skattskyldighet|omvand\s+skattskyldighet|reverse\s+charge", lower_text))
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
        "document_type": "transfer_receipt" if is_transfer_receipt else "credit_note" if is_credit_note else "receipt" if is_receipt else "invoice",
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
        missing_page_indexes = [index for index, text in enumerate(pages) if len(text) < 12]
        confidence_values = []
        if missing_page_indexes:
            try:
                import numpy as np
                import pypdfium2 as pdfium
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as error:
                raise DocumentExtractionError(f"Локальный OCR недоступен: {error.name}.") from error
            engine = ocr_engine or RapidOCR()
            pdf = pdfium.PdfDocument(str(source))
            for index in missing_page_indexes:
                image = pdf[index].render(scale=2).to_pil().convert("RGB")
                result, _ = engine(np.asarray(image))
                lines = []
                for detection in result or []:
                    lines.append(str(detection[1]))
                    if len(detection) > 2:
                        confidence_values.append(float(detection[2]))
                pages[index] = "\n".join(lines)
            method = "PDF-текст + локальный OCR" if any(text.strip() for text in pages) else "локальный OCR"
        else:
            method = "текст PDF"
        text = "\n\n".join(page for page in pages if page)
        confidence = sum(confidence_values) / len(confidence_values) if confidence_values else (1.0 if text else 0.0)
        return ExtractionResult(text=text, method=method, confidence=confidence)

    try:
        import numpy as np
        from PIL import Image
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
    return ExtractionResult(text=text, method="локальный OCR", confidence=confidence)


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
