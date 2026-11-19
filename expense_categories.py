"""Canonical categories for individual expense allocations."""

import re

CATEGORY_MATERIALS = "Материалы и закупки"
CATEGORY_SUBCONTRACTOR_WORK = "Работы подрядчиков"
CATEGORY_SUBCONTRACTOR_MIXED = "Работа и материалы подрядчика, без разбивки"
CATEGORY_OTHER = "Прочие расходы"
CATEGORY_EQUIPMENT_RENTAL = "Аренда оборудования"
CATEGORY_UNCLASSIFIED = "Не классифицировано"

EXPENSE_CATEGORIES = (
    CATEGORY_MATERIALS,
    CATEGORY_SUBCONTRACTOR_WORK,
    CATEGORY_SUBCONTRACTOR_MIXED,
    CATEGORY_EQUIPMENT_RENTAL,
    CATEGORY_OTHER,
    CATEGORY_UNCLASSIFIED,
)

CATEGORY_LABELS_SV = {
    CATEGORY_EQUIPMENT_RENTAL: "Hyra av maskiner och utrustning",
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
    CATEGORY_EQUIPMENT_RENTAL: re.compile(
        r"\b(?:maskinhyra|verktygshyra|uthyrning|hyra\s+(?:av\s+)?(?:maskin\w*|verktyg\w*|utrustning))\b|"
        r"UTH\.DAT\s+\d+\s+PERIOD\s+\d+\s*-\s*\d+", re.IGNORECASE,
    ),
    CATEGORY_SUBCONTRACTOR_WORK: re.compile(
        r"\b(?:underentreprenör(?:s)?(?:arbete)?|subcontract(?:or)?\s+work|"
        r"arbetskostnad|timarbete|byggtjänst(?:er)?|montering|målning|snickeri|elinstallation|"
        r"renoveringsarbete|arbete)\b",
        re.IGNORECASE,
    ),
    CATEGORY_MATERIALS: re.compile(
        r"\b(?:material(?:kostnad)?|byggmaterial|byggvaror|virke|\w*skruv\w*|"
        r"isolering|betong|färg|varuinköp|materialinköp|farmarskruv|överlappsskruv|"
        r"spånskiveskruv|kap(?:skiva|skivor)|trälim|flatborrset|knivblad|torktrasa|storsäck|"
        r"säkerhetskniv|träbock|wipes|gipsskr|byggregel|läkt|glespanel|dyckert|"
        r"städduk|handske|vinkelpensel|filtmask|slägga|vägg/bjälklagsk|gipsskiva|"
        r"osb(?:/3)?|k-ply|golvspån|underlagsduk|limträbalk|fiberduk|formrör|"
        r"armeringsstål|grovbetong|byggpall|byggsäck)\b",
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
_ZERO_FREIGHT = re.compile(
    r"^[ \t]*Frakt[ \t]+exkl\.?[ \t]+moms[ \t]*:?[ \t]*(?:\r?\n[ \t]*)?"
    r"0[,.]00[ \t]*(?:SEK|EUR|NOK|DKK|USD|GBP|kr)?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
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
    document_text = _ZERO_FREIGHT.sub("", "\n".join(filter(None, (
        str(document.get("document_comment") or "").strip(),
        str(document.get("recognized_text") or document.get("raw_text") or "").strip(),
    ))))
    supplier = " ".join(str(document.get("supplier_name") or "").casefold().split())
    document_text = "\n".join(line for line in document_text.splitlines()
                              if " ".join(line.casefold().split()) != supplier
                              and not re.search(r"(?:moms\s*%.*frakt|frakt\s+ex\.?\s*moms.*artik|nettosumma\s+artiklar|avrundningsdifferens.*fakturabelopp)", line, re.IGNORECASE))
    if allocation_text and not (_MIXED_WORK_AND_MATERIALS.search(allocation_text) or any(
        pattern.search(allocation_text) for pattern in _CATEGORY_TERMS.values()
    )):
        allocation_text = ""
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
    rental_blocks = re.split(r"(?m)^\d+\s+(?:PRODNR|ARTNR)\s+\d+\s+", content)[1:]
    if (CATEGORY_EQUIPMENT_RENTAL in matched and rental_blocks
            and all(re.search(r"UTH\.DAT\s+\d+\s+PERIOD|Minsta avgift|RENGÖRING MASKIN|VÄSSNING AV MEJSEL", block, re.I)
                    for block in rental_blocks)):
        # Material words in rented equipment names (e.g. a concrete blade) are
        # not purchases. Each block must evidence rental or its incidental fee.
        matched = [CATEGORY_EQUIPMENT_RENTAL]
    if CATEGORY_EQUIPMENT_RENTAL in matched and CATEGORY_OTHER in matched:
        if not re.search(r"\b(?:parkering|administrationsavgift)\b", content, re.IGNORECASE):
            matched.remove(CATEGORY_OTHER)
    if (CATEGORY_MATERIALS in matched and CATEGORY_OTHER in matched
            and not re.search(r"\b(?:hyra|hyrkostnad|administrationsavgift|serviceavgift|parkering)\b", content, re.IGNORECASE)):
        # Delivery on a goods order is part of the purchase; do not invent a split.
        matched.remove(CATEGORY_OTHER)
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
