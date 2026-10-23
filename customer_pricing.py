"""Confirmed customer prices, independent of employee pay and current catalogues."""
from datetime import date, datetime, timezone
import json
import math


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
    if not isinstance(value.get("source"), str) or not value["source"].strip():
        raise ValueError("Укажите основание подтверждения клиентских цен.")
    ob = value.get("ob", {})
    if not isinstance(ob, dict) or len(ob) > 30:
        raise ValueError("Некорректные клиентские категории OB.")
    return {
        "version": 1, "company": value["company"], "object_name": value["object_name"],
        "date_from": start, "date_to": end, "source": value["source"].strip()[:500],
        "base": checked_rate(value.get("base")), "travel": checked_rate(value.get("travel")),
        "ob": {str(key): checked_rate(rate) for key, rate in ob.items()},
    }


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


def row_prices(row):
    quote = parse_pricing(row.get("customer_pricing"), row)
    if quote is not None:
        return quote
    # A positive legacy work price is evidence; a zero legacy value is not.
    saved = row.get("rate") if row["entry_type"] == "work" else row.get("work_rate_cache")
    saved = checked_rate(saved)
    return {"base": saved if saved else None, "travel": None, "ob": {}}


def require_final_prices(row, segments):
    prices = row_prices(row)
    missing = []
    if prices["base"] is None:
        missing.append("работа")
    if row.get("transport") and prices["travel"] is None:
        missing.append("транспорт")
    if row["entry_type"] == "ob":
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


def agreed_prices_for_date(cursor, table, row):
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
        if raw["date_from"] <= row["date"] <= raw["date_to"]:
            matches.add(pricing_json(raw, row))
    # Conflicting historical evidence requires explicit review, not last-write guessing.
    return next(iter(matches)) if len(matches) == 1 else None
