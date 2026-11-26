"""Read-only category proposals tied to verified original orders and saved shares."""
from pathlib import Path

from document_costs import verified_original_order_allocation_proposal
from expense_documents import extract_local_document, parse_decimal
from expense_categories import CATEGORY_UNCLASSIFIED, confirmed_expense_category, suggest_expense_category
from expense_item_content import single_allocation_content


def category_review_amounts(document, objects, paths=(), errors=None):
    """Return display-only amounts using the same recovery as object reports."""
    from object_package_report import collect_object_expenses, expense_document_key, _recover_report_share_tax
    result = {}
    for obj in objects:
        expenses, _ = collect_object_expenses([document], obj["id"], None, None)
        if not expenses:
            continue
        key = expense_document_key(document)
        _recover_report_share_tax({"object": obj, "expenses": expenses}, {key: list(paths)}, {key: errors} if errors else {})
        for item in expenses:
            for share, index in zip(item.get("object_allocations") or [], item.get("object_allocation_indexes") or []):
                result[index] = {"net_amount": share.get("net_amount"),
                                 "currency_code": document.get("currency_code"),
                                 "explanation": item.get("net_recovery_error") or (
                                     "Проверено по оригиналу; сохранённая доля не изменена."
                                     if share.get("net_recovered_from_original") else "")}
    return result


def original_order_pages(paths):
    pages = []
    for attachment, raw_path in enumerate(paths):
        path = Path(raw_path)
        extracted = extract_local_document(path)
        for page_index, text in enumerate(extracted.pages or (extracted.text,)):
            pages.append({"source": path.name, "page": page_index + 1, "attachment_index": attachment,
                          "text": text, "elements": extracted.page_elements[page_index]
                          if page_index < len(extracted.page_elements) else ()})
    return pages


def propose_document_categories(document, objects, paths=()):
    allocations = document.get("allocations") or []
    result = {}
    for index, allocation in enumerate(allocations):
        _category, source = confirmed_expense_category(allocation.get("expense_category"), allocation.get("expense_category_source"))
        if source == "user":
            continue
        category, evidence = suggest_expense_category(document, allocation, len(allocations))
        if document.get("has_original") or document.get("attachments"):
            category, evidence = CATEGORY_UNCLASSIFIED, "Читаем содержание оригинала."
        result[index] = {"category": category, "evidence": evidence, "content": allocation.get("note") or ""}
    if not any(item["category"] == CATEGORY_UNCLASSIFIED for item in result.values()) or not paths:
        return result
    try:
        pages = original_order_pages(paths)
        content = single_allocation_content(pages, document)
        if len(allocations) == 1:
            if 0 in result:
                if content:
                    category, evidence = suggest_expense_category({}, {"note": content})
                    result[0].update(category=category, evidence="Товарные строки оригинала. " + evidence, content=content)
                else:
                    result[0].update(category=CATEGORY_UNCLASSIFIED, content="", evidence="Товарные строки оригинала не выделены; нужна проверка.")
            return result
        parts = verified_original_order_allocation_proposal(pages, document, objects)
        for index, proposal in result.items():
            if proposal["category"] != CATEGORY_UNCLASSIFIED:
                continue
            allocation = allocations[index]
            def matches_amounts(part):
                net, vat, gross, rounding = (parse_decimal(allocation.get(key)) for key in
                                              ("net_amount", "vat_amount", "gross_amount", "rounding_amount"))
                if (net is not None and vat is not None and gross is not None and rounding is not None
                        and net + vat + rounding == gross
                        and net == parse_decimal(part.get("net_amount"))
                        and vat == parse_decimal(part.get("vat_amount"))):
                    return True
                return (parse_decimal(part.get("gross_amount")) == gross
                        and all(parse_decimal(allocation.get(saved)) is None
                                or parse_decimal(allocation[saved]) == parse_decimal(part.get(original))
                                for saved, original in (("net_amount", "net_amount"), ("vat_amount", "vat_amount"),
                                                        ("rounding_amount", "rounding"))))

            candidates = [part for part in parts
                          if len(part.get("object_options") or []) == 1
                          and part["object_options"][0]["id"] == allocation.get("object_id")
                          and matches_amounts(part)]
            if len(candidates) != 1:
                continue
            part = candidates[0]
            matches = [other for other in allocations
                       if other.get("object_id") == allocation.get("object_id")
                       and (parse_decimal(other.get("gross_amount")) == parse_decimal(allocation.get("gross_amount"))
                            or parse_decimal(other.get("net_amount")) == parse_decimal(allocation.get("net_amount")))]
            if len(matches) != 1:
                continue
            category = part.get("expense_category", CATEGORY_UNCLASSIFIED)
            proposal.update(category=category, content=part.get("content") or "",
                            evidence=f"Оригинал, заказ {', '.join(part['orders'])}, {part['marker']}. "
                                     + str(part.get("expense_category_evidence") or "Содержание неоднозначно."))
    except (ValueError, RuntimeError, OSError) as error:
        for proposal in result.values():
            if proposal["category"] == CATEGORY_UNCLASSIFIED:
                proposal["evidence"] = "Требуется проверка оригинала: " + str(error)
    return result
