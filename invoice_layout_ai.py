"""Privacy-preserving invoice fallback: the provider selects opaque layout blocks."""
import json
import math
import os
import re
import uuid

import requests
from expense_documents import _date_value, decimal_text, invoice_layout_rows, parse_invoice_money

FIELDS = ("supplier_name", "invoice_number", "document_date", "due_date", "net_amount",
          "vat_amount", "gross_amount", "amount_due", "rounding_amount", "currency_code")
CAPTIONS = {
    "seller": r"(?:Säljare(?: företagsinfo)?|Seller|Supplier|Leverantör|Returadress)",
    "buyer": r"(?:Fakturaadress|Köpare|Buyer|Customer|Kund)",
    "invoice_number": r"(?:Faktura\s*(?:nr|nummer)|Invoice\s*(?:number|no\.?)|Fakturanr\.?)",
    "document_date": r"(?:Fakturadatum|Invoice date|Datum)",
    "due_date": r"(?:Förfallodatum|Due date|Oss tillhanda senast)",
    "net_amount": r"(?:Exkl\.?\s*moms|Netto|Net amount|Summa rader|Subtotal)",
    "vat_amount": r"(?:Moms|VAT|Momsbelopp|Tax amount)",
    "gross_amount": r"(?:Inkl\.?\s*moms|Gross amount|Total including VAT)",
    "amount_due": r"(?:Att betala|Fakturatotal|Amount due|Total due)",
    "rounding_amount": r"(?:Öresavr\.?|Öresavrundning|Avrundning|Rounding)",
    "currency_code": r"(?:Fakturans valuta|Valuta|Currency)",
}


def masked_layout(pages):
    public, private = [], {}
    for page_index, page in enumerate(pages, 1):
        for row in invoice_layout_rows(page.get("elements") or []):
            for cell in row:
                text = cell["text"].strip()
                label, value = None, text
                for name, pattern in CAPTIONS.items():
                    match = re.match(rf"^(?:{pattern})(?:\s*:\s*|\s+|$)", text, re.IGNORECASE)
                    if match:
                        label, value = name, text[match.end():].strip()
                        break
                kind = ("date" if _date_value(value) else "money" if parse_invoice_money(value) is not None
                        else "identifier" if re.fullmatch(r"[A-Za-z0-9-]{3,40}", value) else "text")
                index = len(public) + 1
                block = {"id": index, "page": page_index,
                         "box": [round(cell[k], 1) for k in ("x0", "y0", "x1", "y1")],
                         "label": label, "kind": kind, "has_value": bool(value)}
                # Page identity must be stable across attachments without revealing filenames.
                public.append(block)
                private[index] = dict(block, value=value, text=text, source=page.get("source"),
                                      original_page=page.get("page"))
    if not public or len(public) > 2000:
        raise ValueError("Для резервного ИИ нужны координаты текста; не более 2000 блоков.")
    return {"version": 1, "blocks": public}, private


def validate_layout(value):
    if not isinstance(value, dict) or set(value) != {"version", "blocks"} or value["version"] != 1:
        raise ValueError("Неверный обезличенный макет.")
    blocks = value["blocks"]
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 2000:
        raise ValueError("Недопустимый размер макета.")
    for index, block in enumerate(blocks, 1):
        if (not isinstance(block, dict) or set(block) != {"id", "page", "box", "label", "kind", "has_value"}
                or type(block["id"]) is not int or block["id"] != index
                or type(block["page"]) is not int or not 1 <= block["page"] <= 100
                or block["label"] not in (None, *CAPTIONS)
                or block["kind"] not in {"date", "money", "identifier", "text"}
                or type(block["has_value"]) is not bool
                or not isinstance(block["box"], list) or len(block["box"]) != 4
                or any(type(n) not in (int, float) or not math.isfinite(n) or not -100 <= n <= 20000
                       for n in block["box"])):
            raise ValueError("Макет содержит недопустимые поля или необезличенный текст.")
    return value


def apply_selection(fields, selection, private):
    if not isinstance(selection, dict) or set(selection) != set(FIELDS):
        raise ValueError("ИИ вернул неполный набор ссылок.")
    additions, sources = {}, dict(fields.get("field_sources") or {})
    for field in FIELDS:
        picked = selection[field]
        if not isinstance(picked, dict) or set(picked) != {"value", "caption"}:
            raise ValueError("Некорректная ссылка ИИ.")
        if picked["value"] is None and picked["caption"] is None:
            continue
        if type(picked["value"]) is not int or type(picked["caption"]) is not int:
            raise ValueError("ИИ должен возвращать только ID блоков.")
        value, caption = private.get(picked["value"]), private.get(picked["caption"])
        expected = "seller" if field == "supplier_name" else field
        if (value is None or caption is None or caption["label"] != expected
                or value["page"] != caption["page"] or not value["has_value"]
                or abs(value["box"][1] - caption["box"][1]) > 120):
            raise ValueError("Ссылка ИИ не подтверждена подписью и расположением оригинала.")
        raw = value["value"]
        if field in {"document_date", "due_date"}:
            parsed = _date_value(raw)
        elif field.endswith("_amount") or field == "amount_due":
            parsed = decimal_text(parse_invoice_money(raw))
        elif field == "currency_code":
            parsed = raw if raw in {"SEK", "EUR", "USD", "NOK", "DKK"} else None
        elif field == "invoice_number":
            parsed = raw if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/-]{2,49}", raw) else None
        else:
            parsed = raw if 3 <= len(raw) <= 200 and not re.search(r"\b(?:bank|giro|IBAN)\b", raw, re.I) else None
            buyers = [block for block in private.values() if block["page"] == value["page"] and block["label"] == "buyer"]
            if any(abs(block["box"][1] - value["box"][1]) < abs(caption["box"][1] - value["box"][1])
                   for block in buyers):
                raise ValueError("Выбранное название ближе к реквизитам покупателя; продавец не установлен.")
        if parsed is None:
            raise ValueError("Выбранный блок не содержит реквизит ожидаемого типа.")
        if fields.get(field) not in (None, "", "UNKNOWN"):
            continue
        additions[field] = parsed
        sources[field] = [{"kind": "ai_layout_selection", "source": value["source"],
                          "page": value["original_page"], "box": value["box"],
                          "text": value["text"], "caption": caption["text"]}]
    merged = dict(fields, **additions)
    amounts = {key: parse_invoice_money(merged.get(key)) for key in
               ("net_amount", "vat_amount", "gross_amount", "rounding_amount", "amount_due")}
    if any(key in additions for key in amounts):
        if any(amounts[key] is None for key in ("net_amount", "vat_amount", "gross_amount")):
            raise ValueError("ИИ не подтвердил полный fiscal итог; суммы оставлены неизвестными.")
        if amounts["net_amount"] + amounts["vat_amount"] != amounts["gross_amount"]:
            raise ValueError("Net + НДС не совпадают с итогом без округления.")
        if amounts["amount_due"] is not None:
            difference = amounts["amount_due"] - amounts["gross_amount"]
            if difference != (amounts["rounding_amount"] if amounts["rounding_amount"] is not None else 0):
                raise ValueError("Сумма к оплате не совпадает с итогом и отдельным округлением.")
    merged["field_sources"] = sources
    merged["ai_recognition"] = {"method": "masked_layout", "filled": sorted(additions), "requires_review": True}
    return merged


def request_selection(conn, actor, layout, operation, post=None):
    import assistant_api as assistant
    try:
        layout = validate_layout(layout)
    except ValueError as error:
        raise assistant.AssistantError(str(error)) from None
    nullable_id = {"type": ["integer", "null"], "enum": [None, *[b["id"] for b in layout["blocks"]]]}
    item = {"type": "object", "additionalProperties": False, "required": ["value", "caption"],
            "properties": {"value": nullable_id, "caption": nullable_id}}
    schema = {"type": "object", "additionalProperties": False, "required": list(FIELDS),
              "properties": {field: item for field in FIELDS}}
    body = {
        "model": assistant.MODEL, "store": False,
        "instructions": "Select invoice fields using ONLY opaque block IDs and fixed caption labels/geometry. "
                        "No document text or values are supplied. Choose seller, never buyer or payment intermediary. "
                        "A caption must have the field's label (seller for supplier_name), on the same page. "
                        "A caption with has_value=false is a normal label: its value may be in ANOTHER block. "
                        "For every field, prefer the unique value block to the right on the same row; otherwise "
                        "use a uniquely associated block immediately below. Money fields use kind=money, dates "
                        "use kind=date, invoice numbers use kind=identifier or money. The supplier uses kind=text. "
                        "Example: caption 11 label=net_amount at [0,50,100,60], value 12 kind=money has_value=true "
                        "at [160,50,260,60] means net_amount={value:12,caption:11}. "
                        "Do not leave uniquely labelled geometrical matches unknown merely because their text "
                        "is masked: all values and arithmetic will be verified locally. "
                        "Gross is before rounding, amount_due after rounding. Unknown: value=null, caption=null. "
                        "Do not infer missing data. Select only geometrically unambiguous values.",
        "input": json.dumps(layout), "max_output_tokens": 2000,
        "text": {"format": {"type": "json_schema", "name": "invoice_layout", "strict": True, "schema": schema}},
    }
    bound = len(json.dumps(body).encode("utf-8")) + 4096
    micro = math.ceil(bound * assistant.INPUT_MICRO_USD + 2000 * assistant.OUTPUT_MICRO_USD)
    assistant.reserve(conn, actor, operation, "invoice_layout", micro)
    try:
        response = (post or requests.post)(
            "https://api.openai.com/v1/responses", json=body,
            headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
            timeout=(10, 75), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError(f"OpenAI не выполнил резервный разбор (HTTP {response.status_code}).")
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("output"), list):
            raise ValueError("OpenAI вернул неверную структуру ответа.")
        for output in payload["output"]:
            if not isinstance(output, dict):
                raise ValueError("OpenAI вернул неверную структуру сообщения.")
            if output.get("type") == "message" and (
                    not isinstance(output.get("content"), list)
                    or any(not isinstance(part, dict) for part in output["content"])):
                raise ValueError("OpenAI вернул неверную структуру содержимого.")
        texts = [part.get("text") for output in payload.get("output", []) if output.get("type") == "message"
                 for part in output.get("content", []) if part.get("type") == "output_text"]
        if payload.get("status") != "completed" or len(texts) != 1:
            raise ValueError("OpenAI не завершил резервный разбор.")
        result = json.loads(texts[0])
        if not isinstance(result, dict) or set(result) != set(FIELDS):
            raise ValueError("OpenAI вернул неверную схему ссылок.")
        assistant.finish(conn, actor, operation, "invoice_layout", "selected")
        return result
    except (requests.RequestException, ValueError, TypeError, KeyError):
        assistant.finish(conn, actor, operation, "invoice_layout", "failed")
        raise assistant.AssistantError("Резервный ИИ недоступен или ответ не проверен; реквизиты не изменены.") from None


def desktop_selection(layout):
    import desktop_auth
    from sync_dataset import HEADER, file_dataset, require_response_dataset
    token = desktop_auth.load_saved_token()
    if not token:
        raise ValueError("Резервный ИИ: требуется штатный вход администратора в десктопе.")
    expected = os.environ.get("SMARTREPORT_EXPECTED_DATASET")
    if not expected or expected != file_dataset(os.environ.get("SMARTREPORT_DB_FILE", "")):
        raise ValueError("Резервный ИИ: не подтверждён набор данных рабочего профиля.")
    response = requests.post(desktop_auth.server_url().rstrip("/") + "/api/assistant/invoice-layout",
                             headers={"Authorization": "Bearer " + token, HEADER: expected},
                             json={"layout": validate_layout(layout), "operation_id": str(uuid.uuid4())},
                             timeout=(10, 90), allow_redirects=False)
    require_response_dataset(response, expected)
    result = response.json()
    if not response.ok:
        raise ValueError(result.get("error") or f"Резервный ИИ: HTTP {response.status_code}.")
    return result["selection"]
