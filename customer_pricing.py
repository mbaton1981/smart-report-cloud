"""Confirmed customer prices, independent of employee pay and current catalogues."""
from datetime import date, datetime, timezone
import json
import math
from fixed_pricing import parse_fixed, report_agreements, validate_fixed_storage
import payroll_history


CATALOG_PRICE_SOURCE = "Цены справочника объекта (применены пользователем)"
NEW_SHIFT_PRICE_SOURCE = "Цены справочника объекта при создании записи"


def new_shift_pricing(cursor, table, row, *, manual_base=None):
    """Snapshot prices for a NEW record, never reprice existing history."""
    object_table = {"shifts": "objects", "incoming_submissions": "meta_objects"}.get(table)
    if object_table is None:
        raise ValueError("Unsupported pricing table")
    objects = cursor.execute(
        f"SELECT * FROM {object_table} WHERE company=? AND name=? AND is_active=1",
        (row["company"], row["object_name"]),
    ).fetchall()
    if len(objects) != 1:
        raise ValueError("Объект не найден в выбранной фирме или неоднозначен. Обновите справочник.")
    obj = objects[0]
    quote = agreed_prices_for_date(cursor, table, row, reject_conflicts=True)
    if quote:
        quote = parse_pricing(quote, row)
    else:
        base = checked_rate(obj["rate"])
        quote = {
            "version": 1, "company": row["company"], "object_name": row["object_name"],
            "date_from": row["date"], "date_to": row["date"], "source": NEW_SHIFT_PRICE_SOURCE,
            "confirmed": True, "confirmed_at": datetime.now(timezone.utc).isoformat(),
            "base": base if base else None, "travel": checked_rate(obj["transport_rate"]), "ob": {},
        }
    if manual_base is not None:
        quote.update(
            base=checked_rate(manual_base), date_from=row["date"], date_to=row["date"],
            source="Ручной ввод клиентской цены при создании записи",
            confirmed=True, confirmed_at=datetime.now(timezone.utc).isoformat(),
        )
    return quote


def catalog_price_preview(rows, base, travel):
    """Propose only customer base/travel replacements, never payroll or OB changes."""
    base, travel = checked_rate(base), checked_rate(travel)
    if base is None or travel is None:
        raise ValueError("В справочнике должны быть заданы обе цены: работа и транспорт.")
    result = []
    for row in rows:
        if row["entry_type"] not in ("work", "ob"):
            continue
        prices = row_prices(row)
        reason = (
            "Закрыта / выставлена — защищена" if row["invoiced"] else
            "Fast pris — защищён" if prices.get("fixed") else ""
        )
        changed = [key for key, new in (("base", base), ("travel", travel)) if prices[key] != new]
        source = prices.get("source", "")
        origin = (
            "Справочник (ранее применён)" if source in (CATALOG_PRICE_SOURCE, NEW_SHIFT_PRICE_SOURCE) else
            "Ручная цена" if source.startswith("Ручн") else
            "Сохранённые цены; происхождение не указано" if any(
                prices[key] is not None for key in ("base", "travel")
            ) else "Цены не заданы"
        )
        protected = source not in (CATALOG_PRICE_SOURCE, NEW_SHIFT_PRICE_SOURCE) and any(
            prices[key] is not None for key in changed)
        result.append({
            "row": row, "before": prices, "base": base, "travel": travel,
            "eligible": bool(changed) and not reason, "replace_confirmation": protected,
            "origin": origin, "reason": reason or ("Без изменений" if not changed else origin),
        })
    return result


def apply_catalog_price(cursor, row, base, travel, *, replace_confirmed=False):
    if row["entry_type"] not in ("work", "ob"):
        raise ValueError("Цены применяются только к рабочим записям.")
    proposal = catalog_price_preview([row], base, travel)[0]
    if row["invoiced"] or proposal["before"].get("fixed"):
        raise ValueError("Закрытая запись или Fast pris защищены от изменения цен.")
    if not proposal["eligible"]:
        return False
    if proposal["replace_confirmation"] and not replace_confirmed:
        raise ValueError("Замена ручных / сохранённых цен требует отдельного подтверждения.")
    quote = {
        "version": 1, "company": row["company"], "object_name": row["object_name"],
        "date_from": row["date"], "date_to": row["date"],
        "source": CATALOG_PRICE_SOURCE, "confirmed": True,
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "base": proposal["base"], "travel": proposal["travel"], "ob": proposal["before"]["ob"],
    }
    return store_pricing(cursor, "shifts", row["sync_id"], quote)


def ensure_pricing_schema(cursor, table):
    if table not in {"shifts", "incoming_submissions"}:
        raise ValueError("Unsupported pricing table")
    columns = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}
    if "customer_pricing" not in columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN customer_pricing TEXT")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS customer_price_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shift_sync_id TEXT NOT NULL, previous_json TEXT, current_json TEXT,
            changed_at TEXT NOT NULL
        )
    """)


def checked_rate(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Цена должна быть числом, а не логическим значением.")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 100000:
        raise ValueError("Клиентская цена должна быть от 0 до 100000 SEK.")
    return result


def parse_pricing(value, row):
    if not value:
        return None
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("Неизвестный формат исторических клиентских цен.")
    start, end = value.get("date_from", ""), value.get("date_to", "")
    date.fromisoformat(start)
    date.fromisoformat(end)
    if not start <= row["date"] <= end:
        raise ValueError("Клиентская цена не действует на дату записи.")
    if value.get("company") != row["company"] or value.get("object_name") != row["object_name"]:
        raise ValueError("Клиентская цена относится к другому заказчику или объекту.")
    source = value.get("source", "")
    if not isinstance(source, str):
        raise ValueError("Комментарий к ценам должен быть текстом.")
    confirmation = {}
    if "confirmed" in value or "confirmed_at" in value:
        if value.get("confirmed") is not True or not isinstance(value.get("confirmed_at"), str):
            raise ValueError("Некорректное подтверждение клиентских цен.")
        confirmed_at = datetime.fromisoformat(value["confirmed_at"])
        if confirmed_at.tzinfo is None:
            raise ValueError("Дата подтверждения клиентских цен должна содержать часовой пояс.")
        confirmation = {"confirmed": True, "confirmed_at": confirmed_at.isoformat()}
    elif not source.strip():
        raise ValueError("Клиентские цены не подтверждены пользователем.")
    ob = value.get("ob", {})
    if not isinstance(ob, dict) or len(ob) > 30:
        raise ValueError("Некорректные клиентские категории OB.")
    result = {
        "version": 1, "company": value["company"], "object_name": value["object_name"],
        "date_from": start, "date_to": end, "source": source.strip()[:500],
        **confirmation,
        "base": checked_rate(value.get("base")), "travel": checked_rate(value.get("travel")),
        "ob": {str(key): checked_rate(rate) for key, rate in ob.items()},
    }
    if value.get("fixed") is not None:
        result["fixed"] = parse_fixed(value["fixed"], dict(row))
    return result


def pricing_json(value, row):
    parsed = parse_pricing(value, row)
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True) if parsed is not None else None


def store_pricing(cursor, table, sync_id, value, *, from_sync=False):
    """Keep old confirmed versions, including after an explicit reopening."""
    row = cursor.execute(f"SELECT * FROM {table} WHERE sync_id=?", (sync_id,)).fetchone()
    if row is None:
        raise ValueError("Запись для сохранения клиентской цены не найдена.")
    current = row["customer_pricing"]
    encoded = pricing_json(value, row)
    if current == encoded:
        return False
    if row["invoiced"] and current and not from_sync:
        raise ValueError("Сначала явно верните запись в работу; закрытая цена защищена.")
    cursor.execute("""
        INSERT INTO customer_price_audit(shift_sync_id,previous_json,current_json,changed_at)
        VALUES(?,?,?,?)
    """, (sync_id, current, encoded, datetime.now(timezone.utc).isoformat()))
    cursor.execute(f"UPDATE {table} SET customer_pricing=? WHERE sync_id=?", (encoded, sync_id))
    return True


def reopen_invoiced_shifts(cursor, table, rows):
    """Apply the same explicit reopening to a report or its complete selected package."""
    if table not in ("shifts", "incoming_submissions"):
        raise ValueError("Unsupported pricing table")
    rows = [dict(row) for row in rows]
    for row in rows:
        row.setdefault("entry_type", row.get("submission_type"))
    groups, _ = report_agreements({"journal_rows": rows})
    selected = {row["sync_id"] for row in rows}
    if any(not set(fixed["members"]) <= selected for fixed in groups.values()):
        raise ValueError(
            "Fast pris возвращается в работу только целиком. Выбранная смена связана с другими "
            "записями; откройте соответствующий отчёт и выберите весь подтверждённый состав."
        )
    if groups:
        validate_fixed_storage(cursor, table, set(groups))
    _, _, periods = payroll_history.state(payroll_history.events(cursor))
    for row in rows:
        for (employee, start, end), period in periods.items():
            if employee == row["employee"] and start <= row["date"] <= end and period["status"] == "closed":
                raise ValueError(
                    f"Зарплатный период {start} — {end} для {employee} остаётся закрытым. "
                    "Смена не возвращена в работу; нужна отдельная корректировка закрытого периода."
                )
    changed = [row for row in rows if row["invoiced"]]
    for row in changed:
        cursor.execute(f"UPDATE {table} SET invoiced=0 WHERE sync_id=?", (row["sync_id"],))
        if row_prices(row).get("fixed"):
            quote = json.loads(row["customer_pricing"])
            quote.pop("fixed")
            store_pricing(cursor, table, row["sync_id"], quote)
        if table == "shifts":
            cursor.execute(
                "DELETE FROM shift_sync_outbox WHERE sync_id=? AND operation='delete' AND state='conflict'",
                (row["sync_id"],),
            )
    return changed


def row_prices(row):
    quote = parse_pricing(row.get("customer_pricing"), row)
    if quote is not None:
        return quote
    # A positive legacy work price is evidence; a zero legacy value is not.
    saved = row.get("rate") if row["entry_type"] == "work" else row.get("work_rate_cache")
    saved = checked_rate(saved)
    return {"base": saved if saved else None, "travel": None, "ob": {}}


def fill_missing_prices(row, value):
    """Period prices fill gaps without overwriting individually agreed prices."""
    proposed = parse_pricing(value, row)
    previous = parse_pricing(row.get("customer_pricing"), row)
    if previous and previous.get("fixed"):
        raise ValueError("Fast pris защищён. Сначала верните весь подтверждённый период в работу.")
    existing = row_prices(row)
    merged = dict(proposed, ob=dict(proposed["ob"]))
    retained = []
    added = False
    for key in ("base", "travel"):
        old, new = existing[key], proposed[key]
        if old is not None:
            merged[key] = old
            if new is not None and new != old:
                retained.append(key)
        elif new is not None:
            added = True
    for category in existing["ob"].keys() | proposed["ob"].keys():
        old, new = existing["ob"].get(category), proposed["ob"].get(category)
        if old is not None:
            merged["ob"][category] = old
            if new is not None and new != old:
                retained.append(category)
        elif new is not None:
            added = True
    if previous:
        if not added:
            return previous, retained
        merged["date_from"] = max(previous["date_from"], proposed["date_from"])
        merged["date_to"] = min(previous["date_to"], proposed["date_to"])
    elif existing["base"] is not None:
        # A legacy individual rate is evidence for this record, not the whole period.
        merged["date_from"] = merged["date_to"] = row["date"]
    return merged, retained


def require_final_prices(row, segments):
    prices = row_prices(row)
    fixed = prices.get("fixed")
    extra = fixed["members"][row["sync_id"]]["extra"] if fixed else dict.fromkeys(("base", "ob", "travel"), True)
    missing = []
    if extra["base"] and prices["base"] is None:
        missing.append("работа")
    if extra["travel"] and row.get("transport") and prices["travel"] is None:
        missing.append("транспорт")
    if row["entry_type"] == "ob" and extra["ob"]:
        if not segments or abs(sum(float(s["hours"]) for s in segments) - row["hours"]) > 0.000001:
            missing.append("распределение OB")
        for segment in segments:
            if float(segment["premium_percent"] or 0) > 0 and prices["ob"].get(segment["category"]) is None:
                missing.append({
                    "weekday_early_evening": "OB1", "weekday_late_evening_night": "OB2",
                    "weekend": "OB helg", "holiday": "OB helgdag",
                    "saturday": "OB lördag", "sunday": "OB söndag",
                }.get(segment["category"], segment["category"]))
    if missing:
        raise ValueError("Не подтверждены исторические клиентские цены: " + ", ".join(sorted(set(missing))))


def agreed_prices_for_date(cursor, table, row, *, reject_conflicts=False):
    """Reuse a confirmed contract only within its recorded validity, never today's catalogue."""
    if table not in {"shifts", "incoming_submissions"}:
        raise ValueError("Unsupported pricing table")
    candidates = cursor.execute(
        f"SELECT customer_pricing FROM {table} WHERE company=? AND object_name=? "
        "AND customer_pricing IS NOT NULL" + (" AND is_deleted=0" if table == "incoming_submissions" else ""),
        (row["company"], row["object_name"]),
    ).fetchall()
    matches = set()
    for candidate in candidates:
        raw = json.loads(candidate[0])
        if raw.get("fixed") or raw.get("source") in (CATALOG_PRICE_SOURCE, NEW_SHIFT_PRICE_SOURCE):
            continue
        if raw["date_from"] <= row["date"] <= raw["date_to"]:
            matches.add(pricing_json(raw, row))
    prices = {
        json.dumps({key: json.loads(value)[key] for key in ("base", "travel", "ob")}, sort_keys=True)
        for value in matches
    }
    # Confirmation timestamps do not create a price conflict; different amounts do.
    if len(prices) > 1 and reject_conflicts:
        raise ValueError("На дату смены найдены противоречащие договорные цены. Требуется проверка администратором.")
    return min(matches) if len(prices) == 1 else None
