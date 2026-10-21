from pathlib import Path

from expense_documents import parse_invoice_pages, parse_project_order_allocations


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
    fields["order_proposal_warnings"] = proposals["warnings"]
    fields["raw_text"] = "\n\n".join(text_parts)
    fields["review_reasons"] = [*fields.get("review_reasons", []), *errors]
    missing = [key for key in ("supplier_name", "invoice_number", "document_date", "net_amount", "vat_amount", "amount_due")
               if fields.get(key) in (None, "")]
    if not any(page["text"].strip() for page in pages):
        errors.append("Не удалось извлечь текст; оригинал сохранён, доступен повтор обработки.")
    return {"fields": fields, "methods": sorted(methods), "errors": errors, "page_count": len(pages),
            "missing_fields": missing, "state": "failed" if errors else "requires_review"}