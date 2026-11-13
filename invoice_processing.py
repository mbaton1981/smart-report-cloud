from pathlib import Path
import os
import re
import sys
import requests

from expense_documents import (
    allocation_vat_from_items, parse_invoice_pages, parse_project_order_allocations,
    required_document_fields,
)


def recognize_invoice(paths, parser=None, ai_fallback=None):
    from expense_documents import extract_local_document, ocr_pdf_heading

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
    proposals = parse_project_order_allocations(pages)
    fields.setdefault("order_proposals", proposals["orders"])
    fields["order_proposal_warnings"] = list(dict.fromkeys(
        [*fields.get("order_proposal_warnings", []), *proposals["warnings"]]
    ))
    orders = fields["order_proposals"]
    if orders and all(order.get("order_id") for order in orders):
        try:
            fields["order_proposals"] = allocation_vat_from_items(
                pages, orders, fields.get("net_amount"), fields.get("vat_amount")
            )
            fields["order_proposal_warnings"] = [
                warning for warning in fields["order_proposal_warnings"]
                if not (warning.startswith("НДС для заказа ") and "не распознан" in warning)
            ]
        except ValueError as error:
            fields["order_proposal_warnings"].append(str(error))
    needs_help = any(fields.get(key) in (None, "", "UNKNOWN")
                     for key in required_document_fields(fields.get("document_type")))
    if needs_help and not fields.get("non_invoice_document_type") and fields.get("document_type") not in {"order_confirmation", "delivery_note"}:
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
                fields = apply_selection(fields, ai_fallback(layout), private)
                methods.add("openai_masked_layout")
                fields.setdefault("review_reasons", []).append("Резервный ИИ выбрал блоки обезличенного макета; проверьте реквизиты.")
            except (ValueError, RuntimeError, requests.RequestException) as error:
                fields.setdefault("review_reasons", []).append(f"Резервный ИИ: {error}")
    fields["raw_text"] = "\n\n".join(text_parts)
    fields["review_reasons"] = [*fields.get("review_reasons", []), *errors]
    missing = [key for key in required_document_fields(fields.get("document_type"))
               if fields.get(key) in (None, "", "UNKNOWN")]
    if not any(page["text"].strip() for page in pages):
        errors.append("Не удалось извлечь текст; оригинал сохранён, доступен повтор обработки.")
    return {"fields": fields, "methods": sorted(methods), "errors": errors, "page_count": len(pages),
            "missing_fields": missing, "state": "failed" if errors else "requires_review"}