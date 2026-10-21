from pathlib import Path

from expense_documents import (
    allocation_vat_from_items, parse_invoice_pages, parse_project_order_allocations,
    required_document_fields,
)


def recognize_invoice(paths, parser=None):
    from expense_documents import extract_local_document

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
    fields["raw_text"] = "\n\n".join(text_parts)
    fields["review_reasons"] = [*fields.get("review_reasons", []), *errors]
    missing = [key for key in required_document_fields(fields.get("document_type"))
               if fields.get(key) in (None, "", "UNKNOWN")]
    if not any(page["text"].strip() for page in pages):
        errors.append("Не удалось извлечь текст; оригинал сохранён, доступен повтор обработки.")
    return {"fields": fields, "methods": sorted(methods), "errors": errors, "page_count": len(pages),
            "missing_fields": missing, "state": "failed" if errors else "requires_review"}