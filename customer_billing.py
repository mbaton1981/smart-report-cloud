"""Customer invoice composition and audited corrections to expense membership."""
import hashlib
import json
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from expense_categories import CATEGORY_MATERIALS, confirmed_expense_category
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
    schema = """CREATE TABLE IF NOT EXISTS object_material_terms (
        object_id INTEGER PRIMARY KEY REFERENCES meta_objects(id),
        percent INTEGER NOT NULL CHECK(typeof(percent)='integer' AND percent BETWEEN 0 AND 100),
        revision INTEGER NOT NULL)"""
    connection.execute("SAVEPOINT upgrade_material_terms")
    try:
        connection.execute(schema)
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='object_material_terms'").fetchone()[0]
        if "percentin(0,12)" in "".join(sql.lower().split()):
            connection.execute("ALTER TABLE object_material_terms RENAME TO previous_material_terms")
            connection.execute(schema)
            connection.execute("""INSERT INTO object_material_terms (object_id, percent, revision)
                                  SELECT object_id, percent, revision FROM previous_material_terms""")
            connection.execute("DROP TABLE previous_material_terms")
    except Exception:
        connection.execute("ROLLBACK TO upgrade_material_terms")
        raise
    finally:
        connection.execute("RELEASE upgrade_material_terms")
    connection.execute("""CREATE TABLE IF NOT EXISTS customer_invoice_batches (
        operation_id TEXT PRIMARY KEY, object_id INTEGER NOT NULL,
        invoice_number TEXT NOT NULL, invoice_date TEXT NOT NULL,
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(object_id, invoice_number))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS customer_invoice_members (
        member_key TEXT PRIMARY KEY, operation_id TEXT NOT NULL
            REFERENCES customer_invoice_batches(operation_id),
        object_id INTEGER NOT NULL, snapshot_json TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS customer_invoice_corrections (
            correction_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL
            REFERENCES customer_invoice_batches(operation_id),
            revision INTEGER NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL,
            request_json TEXT NOT NULL, removed_json TEXT NOT NULL, added_json TEXT NOT NULL,
            UNIQUE(operation_id, revision))""")


def material_terms(connection, object_id):
    row = connection.execute("SELECT percent, revision FROM object_material_terms WHERE object_id=?", (object_id,)).fetchone()
    return dict(percent=row[0], revision=row[1]) if row else dict(percent=0, revision=0)


def update_material_terms(connection, payload):
    object_id, percent, revision = (payload.get(key) for key in ("object_id", "percent", "revision"))
    if (type(object_id) is not int or type(percent) is not int or not 0 <= percent <= 100
            or type(revision) is not int or not connection.execute(
                "SELECT 1 FROM meta_objects WHERE id=?", (object_id,)).fetchone()):
        raise ValueError("Проверьте объект и ставку наценки.")
    current = material_terms(connection, object_id)
    if current["revision"] != revision:
        raise ValueError("Условия объекта изменились. Откройте окно заново.")
    if current["percent"] != percent:
        connection.execute("""INSERT INTO object_material_terms VALUES (?,?,?)
            ON CONFLICT(object_id) DO UPDATE SET percent=excluded.percent, revision=excluded.revision""",
                           (object_id, percent, revision + 1))
    return material_terms(connection, object_id)


def material_quote(allocation, percent, net):
    category, _ = confirmed_expense_category(allocation.get("expense_category"), allocation.get("expense_category_source"))
    if not percent or category != CATEGORY_MATERIALS:
        return None
    base = parse_decimal(net)
    if base is None:
        return dict(percent=percent, base=None, markup=None, customer_net=None)
    markup = (base * percent / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return dict(percent=percent, base=format(base, ".2f"), markup=format(markup, ".2f"),
                customer_net=format(base + markup, ".2f"))


def invoice_state(connection, object_id):
    batches = [dict(row) for row in connection.execute(
        "SELECT operation_id,invoice_number,invoice_date,created_at FROM customer_invoice_batches "
        "WHERE object_id=? ORDER BY invoice_date,created_at", (object_id,),
    )]
    members = {row["member_key"]: json.loads(row["snapshot_json"]) for row in connection.execute(
        "SELECT member_key,snapshot_json FROM customer_invoice_members WHERE object_id=?", (object_id,),
    )}
    return {"batches": batches, "members": members, "material_terms": material_terms(connection, object_id),
            "custom_invoice_markup": True}


def shift_member(row):
    return {
        "key": "shift:" + str(row["sync_id"]), "kind": "shift",
        "sync_id": row["sync_id"], "version": row.get("sync_version", row.get("version")),
        "signature": signature(row), "customer_pricing": row.get("customer_pricing"),
    }


def issued_history(connection, object_id):
    """Read the current membership of each issued invoice in one database snapshot."""
    batches = {}
    for row in connection.execute("""
        SELECT b.operation_id,b.invoice_number,b.invoice_date,b.payload_json,
               EXISTS(SELECT 1 FROM customer_invoice_corrections c WHERE c.operation_id=b.operation_id) AS corrected
        FROM customer_invoice_batches b WHERE b.object_id=? ORDER BY b.invoice_date DESC,b.created_at DESC
    """, (object_id,)):
        payload = json.loads(row["payload_json"])
        summary = payload.get("history_summary")
        batches[row["operation_id"]] = dict(number=row["invoice_number"], date=row["invoice_date"],
            summary=summary if isinstance(summary, dict) else {}, corrected=bool(row["corrected"]), rows=[])
    for row in connection.execute("""
        SELECT m.operation_id,m.snapshot_json,d.supplier_name,d.invoice_number,d.document_date
        FROM customer_invoice_members m
        LEFT JOIN expense_documents d ON d.id=json_extract(m.snapshot_json,'$.document_id')
        WHERE m.object_id=? ORDER BY m.member_key
    """, (object_id,)):
        item = json.loads(row["snapshot_json"])
        if item["kind"] == "expense":
            item.update(supplier=row["supplier_name"], number=row["invoice_number"], date=row["document_date"])
        batches[row["operation_id"]]["rows"].append(item)
    return list(batches.values())


def expense_member(document, index):
    allocation = document["allocations"][index]
    return {
        "key": f"expense:{document['id']}:{index}", "kind": "expense",
        "document_id": document["id"], "index": index, "version": document["sync_version"],
        "fingerprint": allocation_fingerprint(document, index),
        "net_amount": allocation.get("net_amount"), "currency": document.get("currency_code"),
    }


def priced_expense_member(document, index, percent, net):
    member = expense_member(document, index)
    quote = material_quote(document["allocations"][index], percent, net)
    if quote is not None:
        member["material_pricing"] = quote
    return member


def billing_net(connection, document, index, object_id, percent, recover_nets):
    allocation = document["allocations"][index]
    net = allocation.get("net_amount")
    if recover_nets and parse_decimal(net) is None and material_quote(allocation, percent, None):
        obj = dict(connection.execute("SELECT id,name,markning FROM meta_objects WHERE id=?", (object_id,)).fetchone())
        net = recover_nets(connection.cursor(), document, {object_id: obj}).get(index)
    return net


def checked_expense(connection, requested, object_id, accountant, serialize_document, percent=0, recover_nets=None):
    if type(requested.get("document_id")) is not int:
        raise ValueError("Некорректный номер расхода.")
    row = connection.execute("""
        SELECT d.* FROM expense_documents d WHERE d.id=? AND d.is_deleted=0
        AND NOT EXISTS (SELECT 1 FROM expense_processing p WHERE p.document_id=d.id AND p.state='split')
    """, (requested.get("document_id"),)).fetchone()
    if row is None:
        raise ValueError("Расход удалён или разделён. Обновите состав.")
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
        raise ValueError("Распределение расхода изменилось. Обновите состав.")
    allocation = document["allocations"][index]
    net = billing_net(connection, document, index, object_id, percent, recover_nets)
    snapshot = priced_expense_member(document, index, percent, net)
    if snapshot.get("material_pricing") and parse_decimal(net) is None:
        raise ValueError("Для наценки нужно сверить нетто доли материалов с оригиналом.")
    if (snapshot != requested or allocation.get("object_id") != object_id or allocation.get("company_expense")
            or allocation.get("destination") == "company" or document.get("supporting_to_document_id")
            or document["verification_status"] != "confirmed"
            or parse_decimal(allocation.get("gross_amount") if accountant else snapshot["net_amount"]) is None
            or snapshot["currency"] in (None, "", "UNKNOWN")):
        raise ValueError("Расход или его доля изменились. Обновите состав.")
    return snapshot


def _correction_invoice(connection, object_id, number):
    invoice = connection.execute("""
        SELECT b.*, o.markning, o.name FROM customer_invoice_batches b
        JOIN meta_objects o ON o.id=b.object_id WHERE b.object_id=? AND b.invoice_number=?
    """, (object_id, number)).fetchone()
    if invoice is None:
        raise ValueError("Сохранённый клиентский счёт не найден.")
    return invoice


def _correction_revision(connection, operation_id):
    return connection.execute(
        "SELECT COALESCE(MAX(revision), 0) FROM customer_invoice_corrections WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0]


def invoice_expenses(connection, object_id, number, serialize_document, recover_nets=None):
    invoice = _correction_invoice(connection, object_id, number)
    operation_id = invoice["operation_id"]
    settings = json.loads(invoice["payload_json"])
    accountant = settings.get("purpose") == "accountant"
    percent = settings.get("material_markup_percent", 0)
    current, all_members = [], {}
    for item in connection.execute("""
        SELECT operation_id, member_key, snapshot_json FROM customer_invoice_members WHERE member_key LIKE 'expense:%'
    """):
        snapshot = json.loads(item["snapshot_json"])
        all_members[item["member_key"]] = snapshot
        if item["operation_id"] == operation_id:
            doc = connection.execute("""
                SELECT supplier_name, invoice_number, document_date FROM expense_documents WHERE id=?
            """, (snapshot["document_id"],)).fetchone()
            metadata = dict(doc) if doc else {}
            label = f"{metadata.get('supplier_name') or 'Документ'} · №{metadata.get('invoice_number') or snapshot['document_id']}"
            current.append(dict(snapshot=snapshot, label=label, date=metadata.get("document_date") or "", included=True))
    documents = [serialize_document(connection.cursor(), row) for row in connection.execute("""
        SELECT d.* FROM expense_documents d WHERE d.is_deleted=0 AND d.verification_status='confirmed'
        AND EXISTS (SELECT 1 FROM expense_allocations a WHERE a.object_id=? AND a.document_id=d.id)
        AND NOT EXISTS (SELECT 1 FROM expense_processing p WHERE p.document_id=d.id AND p.state='split')
    """, (object_id,))]
    entries, _ = prepare_next_invoice([], documents, object_id, "9999-12-31", all_members, accountant=accountant)
    for entry in entries:
        document, index = entry["document"], entry["member"]["index"]
        net = billing_net(connection, document, index, object_id, percent, recover_nets)
        entry["member"] = priced_expense_member(document, index, percent, net)
    available = [
        dict(snapshot=entry["member"], label=entry["label"], date=entry["date"], included=False)
        for entry in entries
        if entry["currency"] != "UNKNOWN"
        and parse_decimal(entry["allocation"].get("gross_amount") if accountant else entry["net"]) is not None
    ]
    return dict(object_id=object_id, invoice_number=number, invoice_date=invoice["invoice_date"],
                object_label=" · ".join(filter(None, (invoice["markning"], invoice["name"]))),
                revision=_correction_revision(connection, operation_id),
                expenses=sorted(current + available, key=lambda item: (item["included"], item["date"], item["label"]),
                                reverse=True))


def correct_invoice_expenses(connection, payload, *, actor, serialize_document, now, recover_nets=None):
    if not isinstance(payload, dict):
        raise ValueError("Ожидается исправление состава счёта.")
    correction_id = payload.get("correction_id")
    revision = payload.get("revision")
    removed = payload.get("remove")
    additions = payload.get("add")
    object_id, number = payload.get("object_id"), payload.get("invoice_number")
    if (not isinstance(correction_id, str) or not 1 <= len(correction_id) <= 100
            or type(object_id) is not int or not isinstance(number, str) or type(revision) is not int
            or not isinstance(removed, list) or not isinstance(additions, list)
            or not 1 <= len(removed) + len(additions) <= 2000
            or any(not isinstance(key, str) or not key.startswith("expense:") for key in removed)
            or len(set(removed)) != len(removed)
            or any(not isinstance(item, dict) or item.get("kind") != "expense" for item in additions)):
        raise ValueError("Некорректный состав исправления расходов.")
    invoice = _correction_invoice(connection, object_id, number)
    operation_id = invoice["operation_id"]
    request_json = packed(payload)
    previous = connection.execute("""
        SELECT request_json, revision FROM customer_invoice_corrections WHERE correction_id=?
    """, (correction_id,)).fetchone()
    if previous:
        if previous["request_json"] != request_json:
            raise ValueError("Этот запрос уже использован для другого исправления.")
        return previous["revision"]
    if revision != _correction_revision(connection, operation_id):
        raise ValueError("Состав счёта уже изменился. Откройте его заново.")
    current = {row["member_key"]: json.loads(row["snapshot_json"]) for row in connection.execute(
        "SELECT member_key, snapshot_json FROM customer_invoice_members WHERE operation_id=?", (operation_id,),
    )}
    if any(key not in current or current[key]["kind"] != "expense" for key in removed):
        raise ValueError("Одна из снимаемых позиций не входит в этот счёт.")
    settings = json.loads(invoice["payload_json"])
    accountant = settings.get("purpose") == "accountant"
    added = {}
    for requested in additions:
        snapshot = checked_expense(connection, requested, object_id, accountant, serialize_document,
                                   settings.get("material_markup_percent", 0), recover_nets)
        key = snapshot["key"]
        if key in added or connection.execute(
                "SELECT 1 FROM customer_invoice_members WHERE member_key=?", (key,)).fetchone():
            raise ValueError("Расход уже включён в клиентский счёт. Обновите состав.")
        added[key] = snapshot
    removed_keys = set(removed)
    remaining = [snapshot for key, snapshot in current.items() if key not in removed_keys] + list(added.values())
    if not 1 <= len(remaining) <= 2000:
        raise ValueError("В счёте должно остаться от 1 до 2000 позиций.")
    currencies = {"SEK" if snapshot["kind"] == "shift" else snapshot["currency"] for snapshot in remaining}
    if not accountant and len(currencies) > 1:
        raise ValueError("Позиции этого счёта должны быть в одной валюте.")
    connection.executemany("DELETE FROM customer_invoice_members WHERE operation_id=? AND member_key=?",
                           [(operation_id, key) for key in removed])
    connection.executemany("INSERT INTO customer_invoice_members VALUES (?, ?, ?, ?)",
                           [(key, operation_id, object_id, packed(snapshot)) for key, snapshot in added.items()])
    revision += 1
    connection.execute("INSERT INTO customer_invoice_corrections VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (correction_id, operation_id, revision, actor, now, request_json,
                        packed([current[key] for key in removed]), packed(list(added.values()))))
    return revision


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


def prepare_next_invoice(rows, documents, object_id, cutoff, members, *, accountant=False):
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
            if accountant and (parse_decimal(allocation.get("gross_amount")) is None or currency == "UNKNOWN"):
                warnings.append(f"У расхода №{document['id']} неизвестна сумма с НДС или валюта.")
            elif not accountant and (net is None or currency == "UNKNOWN"):
                warnings.append(f"У расхода №{document['id']} неизвестно нетто или валюта.")
            entries.append({"member": member, "document": document, "allocation": allocation,
                            "date": document_date, "currency": currency,
                            "net": net if currency != "UNKNOWN" else None,
                            "label": f"{document.get('supplier_name') or 'Поставщик не указан'} · "
                                     f"№{document.get('invoice_number') or document['id']}"})
    return sorted(entries, key=lambda item: (item["date"], item["member"]["key"])), list(dict.fromkeys(warnings))


def record_invoice(connection, payload, *, serialize_document, next_sequence, segments, now, recover_nets=None):
    purpose = payload.get("purpose", "priced_invoice")
    if purpose not in ("priced_invoice", "accountant"):
        raise ValueError("Неизвестное назначение документа.")
    accountant = purpose == "accountant"
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
    percent = payload.get("material_markup_percent", 0)
    terms = material_terms(connection, object_id)
    if type(percent) is not int or not 0 <= percent <= 100:
        raise ValueError("Наценка должна быть целым числом от 0 до 100%.")
    if "material_terms_revision" in payload:
        revision = payload["material_terms_revision"]
        if type(revision) is not int or revision != terms["revision"]:
            raise ValueError("Условия объекта изменились. Откройте следующий счёт заново.")
    elif percent != terms["percent"]:
        raise ValueError("Проверьте текущую наценку объекта в обновлённом окне следующего счёта.")
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
            if not accountant:
                require_final_prices(row, segments(connection.cursor(), row["sync_id"]) if row["entry_type"] == "ob" else [])
            work.append(row)
            if not accountant:
                currencies.add("SEK")
        elif requested.get("kind") == "expense":
            snapshot = checked_expense(connection, requested, object_id, accountant, serialize_document, percent, recover_nets)
            currencies.add(snapshot["currency"])
        else:
            raise ValueError("Неизвестный тип строки счёта.")
        if snapshot["key"] in seen or connection.execute(
            "SELECT 1 FROM customer_invoice_members WHERE member_key=?", (snapshot["key"],),
        ).fetchone():
            raise ValueError("Строка уже включена в клиентскую фактуру. Обновите состав.")
        seen.add(snapshot["key"])
        snapshots.append(snapshot)
    if not accountant and len(currencies) > 1:
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
        if not accountant and quote.get("interval_terms"):
            quote["interval_calculation"] = calculated_row(row, quote)
            store_pricing(connection.cursor(), "incoming_submissions", row["sync_id"], quote)
        connection.execute("""UPDATE incoming_submissions SET invoiced=1, version=version+1,
            change_seq=?,last_operation_id=?,updated_at=?,desktop_acked=0 WHERE sync_id=?""",
                           (next_sequence(connection.cursor()), operation_id, now, row["sync_id"]))
    return invoice_state(connection, object_id)
