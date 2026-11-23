from pathlib import Path
import os
import re
import sys
from time import monotonic

import requests

from expense_documents import (
    allocation_vat_from_items, parse_invoice_money, parse_invoice_pages, parse_project_order_allocations,
    required_document_fields, sha256_file,
)


def reconcile_invoice_orders(pages, fields):
    proposals = parse_project_order_allocations(pages)
    fields.setdefault("order_proposals", proposals["orders"])
    warnings = list(dict.fromkeys([*fields.get("order_proposal_warnings", []), *proposals["warnings"]]))
    orders = fields["order_proposals"]
    if orders and all(order.get("order_id") for order in orders):
        try:
            fields["order_proposals"] = allocation_vat_from_items(
                pages, orders, fields.get("net_amount"), fields.get("vat_amount")
            )
            warnings = [
                warning for warning in warnings
                if not (warning.startswith("НДС для заказа ") and "не распознан" in warning)
            ]
        except ValueError as error:
            warnings.append(str(error))
    fields["order_proposal_warnings"] = warnings


def recognize_invoice(paths, parser=None, ai_fallback=None, *, include_timings=False, allow_ai=True):
    from expense_documents import extract_local_document, ocr_pdf_heading

    started = monotonic()
    pages, text_parts, errors, methods = [], [], [], set()
    for attachment_index, source in enumerate(paths):
        try:
            extraction = extract_local_document(str(source))
            methods.add(extraction.method)
            text_parts.append(f"--- {Path(source).name} ---\n{extraction.text}")
            for page_index, text in enumerate(extraction.pages or (extraction.text,)):
                pages.append({
                    "source": Path(source).name, "page": page_index + 1, "text": text,
                    "attachment_index": attachment_index,
                    "elements": extraction.page_elements[page_index] if extraction.page_elements else [],
                })
        except Exception as error:
            errors.append(f"{Path(source).name}: {type(error).__name__}: {error}")
    for attachment_index, source in enumerate(paths):
        if Path(source).suffix.casefold() != ".pdf":
            continue
        attached_pages = [page for page in pages if page["attachment_index"] == attachment_index]
        text = "\n".join(page["text"] for page in attached_pages)
        if (not attached_pages or re.search(r"^\s*(?:Receipt|Kvitto|Kassakvitto)\s*$",
                                            text, re.IGNORECASE | re.MULTILINE)
                or not re.search(r"\bTransaction\s+ID\b", text, re.IGNORECASE)):
            continue
        try:
            heading = ocr_pdf_heading(source)
        except (OSError, RuntimeError, ValueError) as error:
            errors.append(f"{Path(source).name}: OCR заголовка: {type(error).__name__}: {error}")
            continue
        if heading.strip():
            attached_pages[0]["text"] = heading.strip() + "\n" + attached_pages[0]["text"]
            methods.add("локальный OCR заголовка")
            text_parts[attachment_index] = (
                f"--- {Path(source).name} ---\n"
                + "\n\n".join(page["text"] for page in attached_pages)
            )
    fields = (parser or parse_invoice_pages)(pages) if pages else {}
    reconcile_invoice_orders(pages, fields)
    needs_help = any(fields.get(key) in (None, "", "UNKNOWN")
                     for key in required_document_fields(fields.get("document_type")))
    timings = {"local_seconds": monotonic() - started, "ai_seconds": 0.0, "ai_requests": 0}
    if allow_ai and needs_help and not fields.get("non_invoice_document_type") and fields.get("document_type") not in {"order_confirmation", "delivery_note"}:
        from invoice_layout_ai import apply_selection, desktop_selection, masked_layout
        if ai_fallback is None:
            flask = sys.modules.get("flask")
            if flask is not None and flask.has_request_context():
                ai_fallback = flask.current_app.config.get("INVOICE_LAYOUT_AI")
            elif os.environ.get("SMARTREPORT_EXPECTED_DATASET"):
                ai_fallback = desktop_selection
        if ai_fallback is not None:
            try:
                layout, private = masked_layout(pages)
                request_started = monotonic()
                timings["ai_requests"] += 1
                try:
                    selection = ai_fallback(layout)
                finally:
                    timings["ai_seconds"] += monotonic() - request_started
                fields = apply_selection(fields, selection, private)
                methods.add("openai_masked_layout")
                fields.setdefault("review_reasons", []).append("Резервный ИИ выбрал блоки обезличенного макета; проверьте реквизиты.")
                from document_costs import extract_cost_fields
                # Снимок cost_fields не должен отставать от полей, заполненных после разбора.
                fields["object_cost_fields"] = extract_cost_fields(pages, fields)
            except (ValueError, RuntimeError, requests.RequestException) as error:
                fields.setdefault("review_reasons", []).append(f"Резервный ИИ: {error}")
    fields["raw_text"] = "\n\n".join(text_parts)
    fields["review_reasons"] = [*fields.get("review_reasons", []), *errors]
    missing = [key for key in required_document_fields(fields.get("document_type"))
               if fields.get(key) in (None, "", "UNKNOWN")]
    if not any(page["text"].strip() for page in pages):
        errors.append("Не удалось извлечь текст; оригинал сохранён, доступен повтор обработки.")
    result = {"fields": fields, "methods": sorted(methods), "errors": errors, "page_count": len(pages),
              "missing_fields": missing, "state": "failed" if errors else "requires_review"}
    if include_timings:
        result["timings"] = timings
    return result


def recover_beijer_draft_fields(saved, paths):
    supplier = " ".join(str(saved.get("supplier_name") or "").split()).casefold()
    if (supplier != "beijer byggmaterial ab" or len(paths) != 1 or Path(paths[0]).suffix.casefold() != ".pdf"
            or saved.get("verification_status") not in {"draft", "requires_review"}
            or (saved.get("invoice_number") and saved.get("vat_amount") not in (None, ""))):
        return {}
    attachments = saved.get("attachments") or []
    if len(attachments) > 1:
        return {}
    checksum = attachments[0].get("sha256") if attachments else saved.get("file_sha256")
    try:
        if checksum and sha256_file(paths[0]) != checksum:
            return {}
    except OSError:
        return {}
    result = recognize_invoice(paths, allow_ai=False)
    fields = result["fields"]
    if (result["errors"] or result["missing_fields"]
            or " ".join(str(fields.get("supplier_name") or "").split()).casefold() != supplier
            or fields.get("document_type") != "invoice"
            or fields.get("document_date") != saved.get("document_date")
            or fields.get("currency_code") != saved.get("currency_code")
            or parse_invoice_money(saved.get("net_amount")) is None):
        return {}
    for key in ("net_amount", "vat_amount", "gross_amount", "amount_due", "rounding_amount"):
        if saved.get(key) not in (None, "") and parse_invoice_money(saved[key]) != parse_invoice_money(fields.get(key)):
            return {}
    for key in ("invoice_number", "due_date", "payment_reference"):
        if saved.get(key) not in (None, "") and saved[key] != fields.get(key):
            return {}
    recovered = {key: fields[key] for key in (
        "invoice_number", "vat_amount", "gross_amount", "amount_due", "rounding_amount",
        "due_date", "payment_reference",
    ) if fields.get(key) not in (None, "")}
    recovered["raw_text"] = fields.get("raw_text") or ""
    recovered["review_reasons"] = fields.get("review_reasons") or []
    if not fields["order_proposal_warnings"]:
        recovered["order_proposals"] = fields["order_proposals"]
    return recovered
