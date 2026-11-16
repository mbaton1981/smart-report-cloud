"""Canonical categories for individual expense allocations."""

import re

CATEGORY_MATERIALS = "Материалы и закупки"
CATEGORY_SUBCONTRACTOR_WORK = "Работы подрядчиков"
CATEGORY_SUBCONTRACTOR_MIXED = "Работа и материалы подрядчика, без разбивки"
CATEGORY_OTHER = "Прочие расходы"
CATEGORY_UNCLASSIFIED = "Не классифицировано"

EXPENSE_CATEGORIES = (
    CATEGORY_MATERIALS,
    CATEGORY_SUBCONTRACTOR_WORK,
    CATEGORY_SUBCONTRACTOR_MIXED,
    CATEGORY_OTHER,
    CATEGORY_UNCLASSIFIED,
)

CATEGORY_LABELS_SV = {
    CATEGORY_MATERIALS: "Material och inköp",
    CATEGORY_SUBCONTRACTOR_WORK: "Underentreprenörers arbete",
    CATEGORY_SUBCONTRACTOR_MIXED: "Underentreprenör – arbete och material, ej specificerat",
    CATEGORY_OTHER: "Övriga kostnader",
    CATEGORY_UNCLASSIFIED: "Ej kategoriserat",
}


def confirmed_expense_category(value, source):
    category = str(value or "").strip()
    if source == "user" and category in EXPENSE_CATEGORIES[:-1]:
        return category, "user"
    return CATEGORY_UNCLASSIFIED, "unclassified"


_CATEGORY_TERMS = {
    CATEGORY_SUBCONTRACTOR_WORK: re.compile(
        r"\b(?:underentreprenör(?:s)?(?:arbete)?|subcontract(?:or)?\s+work|"
        r"arbetskostnad|timarbete|montering|målning|snickeri|elinstallation|"
        r"renoveringsarbete|arbete)\b",
        re.IGNORECASE,
    ),
    CATEGORY_MATERIALS: re.compile(
        r"\b(?:material(?:kostnad)?|byggmaterial|byggvaror|virke|skruv|"
        r"isolering|betong|färg|varuinköp|materialinköp)\b",
        re.IGNORECASE,
    ),
    CATEGORY_OTHER: re.compile(
        r"\b(?:frakt|leverans(?:avgift)?|hyra|hyrkostnad|transportavgift|"
        r"administrationsavgift|serviceavgift|parkering)\b",
        re.IGNORECASE,
    ),
}
_MIXED_WORK_AND_MATERIALS = re.compile(
    r"\b(?:arbete|arbeten|arbetskostnad)\b.{0,50}(?:\boch\b|&|\+).{0,50}"
    r"\b(?:material|materialkostnad|byggmaterial)\b|"
    r"\b(?:material|materialkostnad|byggmaterial)\b.{0,50}(?:\boch\b|&|\+).{0,50}"
    r"\b(?:arbete|arbeten|arbetskostnad)\b",
    re.IGNORECASE,
)


def suggest_expense_category(document, allocation, allocation_count=1):
    """Suggest from allocation/document content; never use supplier identity."""
    allocation = allocation or {}
    document = document or {}
    category = str(allocation.get("expense_category") or "").strip()
    evidence = str(allocation.get("expense_category_evidence") or "").strip()
    if category in EXPENSE_CATEGORIES[:-1] and evidence:
        return category, evidence

    allocation_text = str(allocation.get("note") or "").strip()
    document_text = "\n".join(filter(None, (
        str(document.get("document_comment") or "").strip(),
        str(document.get("recognized_text") or "").strip(),
    )))
    content = allocation_text or (
        document_text if int(allocation_count or 1) == 1 else ""
    )
    if not content:
        return CATEGORY_UNCLASSIFIED, "Содержание доли не распознано."

    if _MIXED_WORK_AND_MATERIALS.search(content):
        evidence_line = next(
            (line.strip() for line in content.splitlines()
             if _MIXED_WORK_AND_MATERIALS.search(line)),
            content.strip(),
        )
        return CATEGORY_SUBCONTRACTOR_MIXED, f"Строка документа: {evidence_line[:180]}"

    matched = [
        candidate for candidate, pattern in _CATEGORY_TERMS.items()
        if pattern.search(content)
    ]
    if len(matched) != 1:
        return CATEGORY_UNCLASSIFIED, (
            "В содержании несколько категорий или нет однозначной строки."
            if matched else "Однозначная строка содержания не найдена."
        )

    selected = matched[0]
    pattern = _CATEGORY_TERMS[selected]
    evidence_line = next(
        (line.strip() for line in content.splitlines() if pattern.search(line)),
        content.strip(),
    )
    source_label = "Пояснение к доле" if allocation_text else "Строка документа"
    return selected, f"{source_label}: {evidence_line[:180]}"
