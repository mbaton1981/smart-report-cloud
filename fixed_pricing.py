"""Persistent fixed-price agreements and explicit, per-record paid extras."""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import uuid


COMPONENTS = ("base", "ob", "travel")


def signature(row):
    return {
        "date": row["date"], "employee": row["employee"],
        "object_name": row["object_name"],
        "entry_type": row.get("entry_type", row.get("submission_type")),
        "hours": float(row["hours"]), "transport": float(row["transport"] or 0),
        "start_time": row.get("start_time") or "",
        "end_time": row.get("end_time") or "",
    }


def parse_fixed(value, row):
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("Неизвестный формат Fast pris.")
    try:
        amount = Decimal(str(value["amount"]))
        start, end = value["date_from"], value["date_to"]
        date.fromisoformat(start)
        date.fromisoformat(end)
        confirmed = datetime.fromisoformat(value["confirmed_at"])
        members = value["members"]
        identifier = value["id"]
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise ValueError("Некорректное подтверждение Fast pris.") from error
    if (
        not amount.is_finite() or not 0 < amount <= Decimal("1000000000000")
        or amount != amount.quantize(Decimal("0.01"))
        or confirmed.tzinfo is None or start > end
        or not isinstance(identifier, str) or not 1 <= len(identifier) <= 120
        or not isinstance(members, dict) or not members or len(members) > 10000
        or value.get("company") != row["company"]
        or not start <= row["date"] <= end
    ):
        raise ValueError("Некорректная сумма, период или состав Fast pris.")
    for key, member in members.items():
        if (
            not isinstance(key, str) or not key or not isinstance(member, dict)
            or not isinstance(member.get("signature"), dict)
            or not isinstance(member.get("extra"), dict)
            or set(member["extra"]) != set(COMPONENTS)
            or any(type(flag) is not bool for flag in member["extra"].values())
        ):
            raise ValueError("Некорректное разделение включённых работ и дополнений Fast pris.")
    member = members.get(row.get("sync_id"))
    if member is None or member["signature"] != signature(row):
        raise ValueError("Запись не соответствует подтверждённому составу Fast pris.")
    return value


def create_fixed(report, extras=None):
    rows = [row for row in report["journal_rows"] if row["entry_type"] in ("work", "ob")]
    if not rows:
        raise ValueError("Для Fast pris нужны существующие рабочие записи.")
    if any(row["invoiced"] for row in rows):
        raise ValueError("Новый Fast pris нельзя применять к закрытым записям; их подтверждённые цены защищены.")
    extras = extras or {}
    fixed = {
        "version": 1, "id": str(uuid.uuid4()), "company": report["company"],
        "object_sync_id": report["sync_id"],
        "date_from": report["date_from"], "date_to": report["date_to"],
        "amount": report["fast_pris_amount"],
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "members": {
            row["sync_id"]: {
                "signature": signature(row),
                "extra": {key: bool(extras.get(row["sync_id"], {}).get(key, False)) for key in COMPONENTS},
            } for row in rows
        },
    }
    for row in rows:
        parse_fixed(fixed, row)
    return fixed


def fixed_quote(row, fixed):
    quote = json.loads(row["customer_pricing"]) if row.get("customer_pricing") else {
        "version": 1, "company": row["company"], "object_name": row["object_name"],
        "date_from": row["date"], "date_to": row["date"], "source": "",
        "confirmed": True, "confirmed_at": fixed["confirmed_at"],
        "base": (float(row.get("rate") or 0) or None) if row["entry_type"] == "work"
        else (float(row.get("work_rate_cache") or 0) or None),
        "travel": None, "ob": {},
    }
    quote["fixed"] = fixed
    return quote


def report_agreements(report):
    """Partial selection/sync must never turn a package into a final full invoice."""
    groups, allocations = {}, {}
    for row in report["journal_rows"]:
        quote = json.loads(row["customer_pricing"]) if row.get("customer_pricing") else {}
        if quote.get("fixed"):
            fixed = parse_fixed(quote["fixed"], row)
            previous = groups.setdefault(fixed["id"], fixed)
            if previous != fixed:
                raise ValueError("Разные версии подтверждения Fast pris; нужна сверка синхронизации.")
            allocations[row["sync_id"]] = fixed
    if not groups and report.get("is_fast_pris"):
        fixed = create_fixed(report, report.get("fast_pris_extras"))
        groups[fixed["id"]] = fixed
        allocations = {key: fixed for key in fixed["members"]}
    selected = {row["sync_id"] for row in report["journal_rows"]}
    complete = all(
        set(fixed["members"]) <= selected
        and all(allocations.get(key) == fixed for key in fixed["members"])
        for fixed in groups.values()
    )
    report["fixed_agreements"] = list(groups.values())
    report["fixed_allocations"] = allocations
    report["fixed_complete"] = complete
    if groups:
        report["is_fast_pris"] = True
        report["fast_pris_amount"] = sum(float(fixed["amount"]) for fixed in groups.values())
        if not complete:
            report.setdefault("warnings", []).append(
                "Fast pris: hela det bekräftade underlaget saknas i urvalet eller synkroniseringen. "
                "Välj hela perioden; slutbeloppet kan inte fastställas."
            )
    return groups, allocations


def validate_fixed_storage(cursor, table, touched):
    if table not in ("shifts", "incoming_submissions"):
        raise ValueError("Unsupported fixed-price table")
    groups = {}
    for record in cursor.execute(f"SELECT * FROM {table}"):
        row = dict(record)
        quote = json.loads(row["customer_pricing"]) if row.get("customer_pricing") else {}
        fixed = quote.get("fixed")
        if not fixed or fixed.get("id") not in touched:
            continue
        if row.get("is_deleted"):
            raise ValueError("Нельзя удалить участника подтверждённого Fast pris.")
        fixed = parse_fixed(fixed, row)
        group = groups.setdefault(fixed["id"], {"fixed": fixed, "rows": {}})
        if fixed != group["fixed"]:
            raise ValueError("Fast pris: разные версии подтверждения.")
        group["rows"][row["sync_id"]] = bool(row["invoiced"])
    for group in groups.values():
        if set(group["rows"]) != set(group["fixed"]["members"]) or len(set(group["rows"].values())) != 1:
            raise ValueError(
                "Fast pris синхронизируется и возвращается в работу только целиком. "
                "Состав или версии записей расходятся; изменения запроса не применены."
            )
