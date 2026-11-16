"""Canonical categories for individual expense allocations."""

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
