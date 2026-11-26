"""Management reporting for confirmed company allocations, in their own currency."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
import re

from expense_documents import parse_decimal


FUEL = "Топливо"
REPAIRS = "Автосервис и ремонт"
ROAD = "Парковка и дорожные сборы"
STORAGE = "Склад и помещения"
CLOTHING = "Спецодежда и защита"
SOFTWARE = "Программы и ИИ"
TOOLS = "Инструменты и расходники"
OTHER = "Другие расходы фирмы"
VEHICLE_TAX = "Транспортный налог"
OTHER_TAX = "Другие налоги и взносы"
UNKNOWN = "Уточнить назначение"
TAXES = (VEHICLE_TAX, OTHER_TAX)
ARTICLES = (FUEL, REPAIRS, ROAD, STORAGE, CLOTHING, SOFTWARE, TOOLS, OTHER, *TAXES)
OPERATIONS, TAX, REVIEW = "Расходы", "Налоги и взносы", "К разбору"

_TERMS = {
    FUEL: r"\b(?:diesel\w*|bensin\w*|drivmedel\w*|hvo100|fuel|бензин\w*|дизел\w*|топлив\w*)\b",
    REPAIRS: r"\b(?:bilservice|bilreparation\w*|fordonsservice|oljebyte|däckbyte|hjulinställning|bromsbelägg|автосервис\w*)\b",
    ROAD: r"\b(?:parkering\w*|trängselskatt\w*|infrastrukturavgift\w*|vägavgift\w*|parking|парковк\w*)\b",
    STORAGE: r"\b(?:förrådshyra|lagerhyra|lokalhyra|magasinering|self.storage|hyra av förråd|аренда склада)\b",
    CLOTHING: r"\b(?:arbetskläd\w*|arbetsbyx\w*|arbetsjack\w*|skyddssko\w*|skyddskäng\w*|varsel\w*|skyddshandsk\w*|спецодежд\w*)\b",
    SOFTWARE: r"\b(?:chatgpt|copilot|api usage|api credits|github (?:team|pro|enterprise|actions)|software subscription|programlicens\w*|mjukvar\w*)\b",
    VEHICLE_TAX: r"\b(?:fordonsskatt\w*|vägtrafikskatt\w*|транспортный налог)\b",
    OTHER_TAX: r"\b(?:arbetsgivaravgift\w*|debiterad preliminärskatt)\b",
}


def company_share(share):
    return share.get("object_id") is None and share.get("destination", "company") == "company"


def section(article):
    if article in TAXES:
        return TAX
    return OPERATIONS if article in ARTICLES else REVIEW


def suggest(document, share):
    # OCR and supplier cues belong to the whole document only for single-share purchases.
    single = len(document.get("allocations", [])) == 1
    parts = [share.get("note"), share.get("expense_category_evidence")]
    if single:
        parts.extend([document.get("document_comment"), document.get("recognized_text")])
    lines = [line.strip() for part in parts if part for line in str(part).splitlines() if line.strip()]
    hits = {article: line for line in lines for article, pattern in _TERMS.items() if re.search(pattern, line, re.I)}
    if len(hits) == 1:
        article, line = next(iter(hits.items()))
        return article, line[:300]
    if single and not hits:
        supplier = str(document.get("supplier_name") or "").strip()
        for pattern, article in ((r"^(?:GitHub|OpenAI)\b", SOFTWARE), (r"^Green Storage\b", STORAGE)):
            if re.search(pattern, supplier, re.I):
                return article, "Подсказка по поставщику · " + supplier[:150]
    return UNKNOWN, "В описании несколько статей" if hits else "Нужно уточнить покупку"


@dataclass
class Share:
    document: dict
    allocation: dict
    article: str
    currency: str
    net: Decimal | None
    gross: Decimal | None


def report_shares(documents):
    result = []
    for doc in documents:
        if (doc.get("verification_status") != "confirmed" or doc.get("is_deleted")
                or doc.get("supporting_to_document_id") or (doc.get("processing") or {}).get("state") == "split"):
            continue
        for share in doc.get("allocations") or []:
            if not company_share(share):
                continue
            article = share.get("company_article")
            currency = str(doc.get("currency_code") or "").strip().upper()
            net, gross = parse_decimal(share.get("net_amount")), parse_decimal(share.get("gross_amount"))
            if (net is None and len(doc["allocations"]) == 1 and gross is not None
                    and gross == parse_decimal(doc.get("gross_amount")) and doc.get("fully_allocated")):
                net = parse_decimal(doc.get("net_amount"))
            result.append(Share(doc, share, article if article in ARTICLES else UNKNOWN,
                                currency if re.fullmatch("[A-Z]{3}", currency) else "", net, gross))
    return result


def totals(rows, basis):
    amounts, missing = defaultdict(Decimal), Counter()
    for row in rows:
        value = getattr(row, basis)
        if value is None or not row.currency:
            missing[row.article] += 1
        else:
            amounts[row.article] += value
    return dict(amounts), missing


def review_proposals(rows, request, originals, objects):
    """Read actual share descriptions; unconfirmed suggestions are never financial writes."""
    from tempfile import TemporaryDirectory
    from expense_categories import CATEGORY_UNCLASSIFIED
    from expense_category_ai import classify_unknown
    from expense_category_automation import propose_document_categories
    from object_package_report import expense_document_key
    pending = defaultdict(list)
    for row in rows:
        if row.article == UNKNOWN:
            pending[row.document["id"]].append(row)
    result = {}
    for doc_id, shares in pending.items():
        proposals = result[doc_id] = {}
        try:
            doc = request("GET", f"/api/expense-documents/{doc_id}")["document"]
            if doc["sync_version"] != shares[0].document["sync_version"]:
                raise ValueError("Документ изменился. Обновите вкладку.")
            positions = {share["allocation_id"]: i for i, share in enumerate(doc["allocations"])}
            for row in shares:
                part_id = row.allocation["allocation_id"]
                share = doc["allocations"][positions[part_id]]
                article, reason = suggest(doc, share)
                proposals[part_id] = dict(category=article if article != UNKNOWN else CATEGORY_UNCLASSIFIED,
                                          evidence=reason, content=str(share.get("note") or ""))
            if doc.get("has_original") and any(p["category"] == CATEGORY_UNCLASSIFIED for p in proposals.values()):
                with TemporaryDirectory(prefix="company-articles-") as folder:
                    paths, errors = originals([doc], folder, lambda _message: None)
                    key = expense_document_key(doc)
                    if paths.get(key) and not errors.get(key):
                        # Existing object categories do not classify a company purchase.
                        for_review = dict(doc, allocations=[dict(s, expense_category_source="unclassified")
                                                             for s in doc["allocations"]])
                        extracted = propose_document_categories(for_review, objects, paths[key])
                        for part_id, proposal in proposals.items():
                            if proposal["category"] == CATEGORY_UNCLASSIFIED:
                                content = (extracted.get(positions[part_id]) or {}).get("content")
                                if content:
                                    proposal["content"] = content
                                    article, reason = suggest({}, {"note": content})
                                    if article != UNKNOWN:
                                        proposal.update(category=article, evidence=reason)
        except Exception as error:
            for row in shares:
                proposals.setdefault(row.allocation["allocation_id"], dict(category=CATEGORY_UNCLASSIFIED, content="",
                                     evidence="Откройте документ для проверки. " + str(error)[:180]))
    def ask(payload):
        return request("POST", "/api/assistant/company-expense-articles", json=payload, timeout=105)
    classify_unknown(result, ask, (*ARTICLES, CATEGORY_UNCLASSIFIED))
    return result
