"""Immutable customer-invoice membership and preparation of the next invoice."""
import hashlib
import json
from datetime import date

from expense_documents import parse_decimal
from fixed_pricing import signature


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def allocation_fingerprint(document, index):
    allocation = document["allocations"][index]
    financial = {
        key: str(parse_decimal(allocation.get(key))) if parse_decimal(allocation.get(key)) is not None else None
        for key in ("net_amount", "vat_amount", "gross_amount", "rounding_amount")
    }
    financial.update(object_id=allocation.get("object_id"), destination=allocation.get("destination"),
                     currency=document.get("currency_code"))
    return hashlib.sha256(packed(financial).encode()).hexdigest()


def ensure_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS customer_invoice_batches (
        operation_id TEXT PRIMARY KEY, object_id INTEGER NOT NULL,
        invoice_number TEXT NOT NULL, invoice_date TEXT NOT NULL,
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(object_id, invoice_number))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS customer_invoice_members (
        member_key TEXT PRIMARY KEY, operation_id TEXT NOT NULL
            REFERENCES customer_invoice_batches(operation_id),
        object_id INTEGER NOT NULL, snapshot_json TEXT NOT NULL)""")


def invoice_state(connection, object_id):
    batches = [dict(row) for row in connection.execute(
        "SELECT operation_id,invoice_number,invoice_date,created_at FROM customer_invoice_batches "
        "WHERE object_id=? ORDER BY invoice_date,created_at", (object_id,),
    )]
    members = {row["member_key"]: json.loads(row["snapshot_json"]) for row in connection.execute(
        "SELECT member_key,snapshot_json FROM customer_invoice_members WHERE object_id=?", (object_id,),
    )}
    return {"batches": batches, "members": members}


def shift_member(row):
    return {
        "key": "shift:" + str(row["sync_id"]), "kind": "shift",
        "sync_id": row["sync_id"], "version": row.get("sync_version", row.get("version")),
        "signature": signature(row), "customer_pricing": row.get("customer_pricing"),
    }


def expense_member(document, index):
    allocation = document["allocations"][index]
    return {
        "key": f"expense:{document['id']}:{index}", "kind": "expense",
        "document_id": document["id"], "index": index, "version": document["sync_version"],
        "fingerprint": allocation_fingerprint(document, index),
        "net_amount": allocation.get("net_amount"), "currency": document.get("currency_code"),
    }


def priced_report(report, rows):
    from copy import deepcopy
    from customer_report import prepare_customer_costs
    result = deepcopy(report)
    result["journal_rows"] = rows
    result["warnings"] = []
    verified_ob = all(row["entry_type"] != "ob" or abs(
        sum(float(segment["hours"]) for segment in result["journal_segments"].get(row["sync_id"], []))
        - float(row["hours"])) < 0.000001 for row in rows)
    result["totals"] = {"hours": sum(float(row["hours"]) for row in rows),
                        "without_ob_hours": 0.0 if verified_ob else None, "ob_categories": {}}
    prepare_customer_costs(result)
    return result


def prepare_next_invoice(rows, documents, object_id, cutoff, members):
    date.fromisoformat(cutoff)
    entries, warnings = [], []
    for row in rows:
        if row.get("entry_type") not in ("work", "ob") or row["date"] > cutoff:
            continue
        member = shift_member(row)
        if member["key"] in members or row.get("invoiced"):
            continue
        entries.append({"member": member, "row": row, "date": row["date"],
                        "label": f"{row['employee']} · {row['hours']:g} ч", "currency": "SEK"})
    for document in documents:
        if document.get("supporting_to_document_id"):
            continue
        allocations = document.get("allocations") or []
        billed_parts = [member for member in members.values()
                        if member.get("kind") == "expense" and member.get("document_id") == document["id"]]
        if any(member["index"] >= len(allocations) or member["fingerprint"] != allocation_fingerprint(document, member["index"])
               for member in billed_parts):
            warnings.append(f"Расход №{document['id']} изменился после выставления. Сверьте прежнюю фактуру.")
            continue
        for index, allocation in enumerate(allocations):
            if (allocation.get("object_id") != object_id or allocation.get("company_expense")
                    or allocation.get("destination") == "company"):
                continue
            member = expense_member(document, index)
            previous = members.get(member["key"])
            if previous:
                if previous["fingerprint"] != member["fingerprint"]:
                    warnings.append(f"Расход №{document['id']} изменился после выставления. Сверьте прежнюю фактуру.")
                continue
            if document.get("verification_status") != "confirmed" or document.get("sync_state") in ("pending", "conflict", "unverified"):
                warnings.append(f"Расход №{document['id']} ожидает подтверждения или синхронизации.")
                continue
            document_date = document.get("document_date")
            if not document_date:
                warnings.append(f"У расхода №{document['id']} нет даты.")
                continue
            if document_date > cutoff:
                continue
            net = parse_decimal(allocation.get("net_amount"))
            currency = document.get("currency_code") or "UNKNOWN"
            if net is None or currency == "UNKNOWN":
                warnings.append(f"У расхода №{document['id']} неизвестно нетто или валюта.")
                continue
            entries.append({"member": member, "document": document, "allocation": allocation,
                            "date": document_date, "currency": currency, "net": net,
                            "label": f"{document.get('supplier_name') or 'Поставщик не указан'} · "
                                     f"№{document.get('invoice_number') or document['id']}"})
    return sorted(entries, key=lambda item: (item["date"], item["member"]["key"])), list(dict.fromkeys(warnings))


def record_invoice(connection, payload, *, serialize_document, next_sequence, segments, now):
    operation_id = payload.get("operation_id")
    number = str(payload.get("invoice_number") or "").strip()
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 100:
        raise ValueError("Требуется устойчивый ID операции.")
    if not number or len(number) > 120:
        raise ValueError("Укажите номер клиентской фактуры.")
    date.fromisoformat(payload.get("invoice_date") or "")
    object_id = payload.get("object_id")
    if type(object_id) is not int:
        raise ValueError("Выберите объект.")
    raw_members = payload.get("members")
    if not isinstance(raw_members, list) or not 1 <= len(raw_members) <= 2000:
        raise ValueError("Выберите от 1 до 2000 строк счёта.")
    serialized = packed(payload)
    existing = connection.execute("SELECT payload_json FROM customer_invoice_batches WHERE operation_id=?", (operation_id,)).fetchone()
    if existing:
        if existing[0] != serialized:
            raise ValueError("ID операции уже использован для другого состава.")
        return invoice_state(connection, object_id)
    obj = connection.execute("SELECT * FROM meta_objects WHERE id=?", (object_id,)).fetchone()
    if obj is None:
        raise ValueError("Объект отсутствует в справочнике.")
    snapshots, seen, work, currencies = [], set(), [], set()
    from customer_pricing import require_final_prices, row_prices
    for requested in raw_members:
        if not isinstance(requested, dict):
            raise ValueError("Некорректная строка счёта.")
        if requested.get("kind") == "shift":
            row = connection.execute("SELECT * FROM incoming_submissions WHERE sync_id=?", (requested.get("sync_id"),)).fetchone()
            if row is None or row["is_deleted"] or row["invoiced"]:
                raise ValueError("Смена отсутствует или уже выставлена. Обновите состав.")
            row = dict(row)
            row["entry_type"] = row["submission_type"]
            if (row["company"] != obj["company"] or row["object_name"] != obj["name"]
                    or row["submission_type"] not in ("work", "ob")):
                raise ValueError("Смена относится к другому объекту.")
            snapshot = shift_member(row)
            if snapshot != requested:
                raise ValueError("Смена или её цена изменились. Обновите состав.")
            if row_prices(row).get("fixed"):
                raise ValueError("Fast pris требует полного согласованного состава. Используйте действующий отчёт Fast pris.")
            if row["entry_type"] == "ob" and connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ob_segments'",
            ).fetchone() is None:
                raise ValueError("Для смены OB отсутствует разбивка часов.")
            require_final_prices(row, segments(connection.cursor(), row["sync_id"]) if row["entry_type"] == "ob" else [])
            work.append(row)
            currencies.add("SEK")
        elif requested.get("kind") == "expense":
            row = connection.execute("SELECT * FROM expense_documents WHERE id=? AND is_deleted=0", (requested.get("document_id"),)).fetchone()
            if row is None:
                raise ValueError("Расход удалён. Обновите состав.")
            document = serialize_document(connection.cursor(), row)
            existing_members = [json.loads(item[0]) for item in connection.execute(
                "SELECT snapshot_json FROM customer_invoice_members WHERE member_key LIKE ?",
                (f"expense:{document['id']}:%",),
            )]
            if any(member["index"] >= len(document["allocations"])
                   or member["fingerprint"] != allocation_fingerprint(document, member["index"])
                   for member in existing_members):
                raise ValueError("Ранее выставленные доли изменились. Сверьте прежнюю фактуру.")
            index = requested.get("index")
            if type(index) is not int or not 0 <= index < len(document["allocations"]):
                raise ValueError("Распределение расхода изменилось.")
            snapshot = expense_member(document, index)
            allocation = document["allocations"][index]
            if (snapshot != requested or allocation.get("object_id") != object_id
                    or document.get("supporting_to_document_id") or document["verification_status"] != "confirmed"
                    or parse_decimal(snapshot["net_amount"]) is None or snapshot["currency"] in (None, "", "UNKNOWN")):
                raise ValueError("Расход или его доля изменились. Обновите состав.")
            currencies.add(snapshot["currency"])
        else:
            raise ValueError("Неизвестный тип строки счёта.")
        if snapshot["key"] in seen or connection.execute(
            "SELECT 1 FROM customer_invoice_members WHERE member_key=?", (snapshot["key"],),
        ).fetchone():
            raise ValueError("Строка уже включена в клиентскую фактуру. Обновите состав.")
        seen.add(snapshot["key"])
        snapshots.append(snapshot)
    if len(currencies) > 1:
        raise ValueError("Выберите строки в одной валюте. Для пересчёта валют нужен отдельный согласованный курс.")
    connection.execute("INSERT INTO customer_invoice_batches VALUES (?,?,?,?,?,?)",
                       (operation_id, object_id, number, payload["invoice_date"], serialized, now))
    for snapshot in snapshots:
        connection.execute("INSERT INTO customer_invoice_members VALUES (?,?,?,?)",
                           (snapshot["key"], operation_id, object_id, packed(snapshot)))
    from customer_pricing import store_pricing
    from customer_intervals import calculated_row
    for row in work:
        quote = row_prices(row)
        if quote.get("interval_terms"):
            quote["interval_calculation"] = calculated_row(row, quote)
            store_pricing(connection.cursor(), "incoming_submissions", row["sync_id"], quote)
        connection.execute("""UPDATE incoming_submissions SET invoiced=1, version=version+1,
            change_seq=?,last_operation_id=?,updated_at=?,desktop_acked=0 WHERE sync_id=?""",
                           (next_sequence(connection.cursor()), operation_id, now, row["sync_id"]))
    return invoice_state(connection, object_id)
