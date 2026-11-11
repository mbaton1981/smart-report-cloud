"""Explicit customer-only allocation of existing hours; never inferred payroll."""
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json

from customer_intervals import number, paid_minutes, time_facts, ZONE


SCHEMES = {"ob": "Вечерняя смена — OB", "overtime": "Сверхурочная работа — Övertid"}
BUCKETS = ("ordinary", "16-17", "17-19", "19-22", "22-06", "weekend")
LABELS = {
    "ordinary": "Обычные часы — без доплаты (явно)",
    "16-17": "16:00–17:00", "17-19": "17:00–19:00",
    "19-22": "19:00–22:00", "22-06": "22:00–06:00",
    "weekend": "Выходной — явно согласовано +100%",
}
PERCENTAGES = {"ob": (0, 0, 20, 40, 70, 100), "overtime": (0, None, 50, 70, 100, 100)}
CENT = Decimal("0.01")


def signature(row):
    facts = time_facts(row.get("time_facts"), row["date"], row["hours"])
    data = {key: row.get(key) for key in ("sync_id", "date", "employee", "company", "object_name",
                                         "start_time", "end_time")}
    data.update(hours=str(number(row["hours"]).normalize()),
                kind=row.get("entry_type", row.get("submission_type")), time_facts=facts)
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def amounts(base, hours, percent):
    basic = (base * hours).quantize(CENT, rounding=ROUND_HALF_UP)
    premium = (base * hours * percent / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    return {"base_amount": str(basic), "premium_amount": str(premium), "total": str(basic + premium)}


def allocation(row, base, scheme, quantities, *, weekend=False):
    if scheme not in SCHEMES or type(weekend) is not bool:
        raise ValueError("Выберите клиентскую схему; выходные подтверждаются отдельно.")
    if row.get("entry_type", row.get("submission_type")) not in ("work", "ob"):
        raise ValueError("Доплаты применяются только к существующим рабочим записям.")
    if base is None or isinstance(base, bool):
        raise ValueError("Не подтверждена историческая базовая клиентская ставка. Заполните базу в ценах периода.")
    base = number(base)
    if not base.is_finite() or not 0 <= base <= 100000:
        raise ValueError("Некорректная историческая базовая ставка.")
    if not isinstance(quantities, dict) or set(quantities) - set(BUCKETS):
        raise ValueError("Некорректные строки распределения клиентских часов.")
    parts, total = [], Decimal(0)
    for bucket, percent in zip(BUCKETS, PERCENTAGES[scheme]):
        raw = quantities.get(bucket, 0)
        hours = number(raw)
        if isinstance(raw, bool) or not hours.is_finite() or hours < 0 or hours > 24:
            raise ValueError("Часы распределения должны быть от 0 до 24.")
        if not hours:
            continue
        if percent is None:
            raise ValueError("Övertid 16:00–17:00 в письме не определён. Уточните условие, не подставляйте процент.")
        if (bucket == "weekend") != weekend:
            raise ValueError("Выходные +100% не складываются с OB/Övertid: распределите часы по одной схеме.")
        if weekend and date.fromisoformat(row["date"]).weekday() < 5:
            raise ValueError("Дата записи не выходной день; условие +100% не применяется автоматически.")
        total += hours
        parts.append({"bucket": bucket, "hours": str(hours), "percent": str(percent),
                      **amounts(base, hours, Decimal(percent))})
    actual = number(row["hours"])
    if not parts or abs(total - actual) > Decimal("0.000000001"):
        raise ValueError(f"Распределено {total:g} ч, сохранено {actual:g} ч. "
                         f"Обычный нераспределённый остаток: {actual-total:g} ч; укажите его явно.")
    return {"version": 1, "scheme": scheme, "weekend": weekend, "row_signature": signature(row),
            "base": format(base.normalize(), "f"), "parts": parts}


def validate(value, row, base):
    keys = {"version", "scheme", "weekend", "row_signature", "base", "parts", "confirmed_at"}
    if not isinstance(value, dict) or set(value) != keys or value["version"] != 1:
        raise ValueError("Неизвестный формат подтверждённого распределения клиентских часов.")
    stamp = datetime.fromisoformat(value["confirmed_at"])
    if stamp.tzinfo is None:
        raise ValueError("Подтверждение распределения должно содержать часовой пояс.")
    parts = value["parts"]
    if not isinstance(parts, list) or not 1 <= len(parts) <= len(BUCKETS):
        raise ValueError("Некорректные строки сохранённого распределения.")
    quantities = {}
    for part in parts:
        if not isinstance(part, dict) or part.get("bucket") in quantities:
            raise ValueError("Строки сохранённого распределения повторяются.")
        quantities[part.get("bucket")] = part.get("hours")
    expected = allocation(row, base, value["scheme"], quantities, weekend=value["weekend"])
    if dict(expected, confirmed_at=value["confirmed_at"]) != value:
        raise ValueError("Распределение, историческая ставка или время изменились; повторно подтвердите расчёт.")
    return value


def proposed_hours(row, scheme):
    """Suggest from actual paid minutes only, respecting an explicitly recorded mode."""
    facts = time_facts(row.get("time_facts"), row["date"], row["hours"])
    if facts is None:
        raise ValueError("Точных интервалов нет. Распределите сохранённые часы вручную, без выдуманного времени.")
    if scheme not in SCHEMES:
        raise ValueError("Сначала выберите схему.")
    minutes = dict.fromkeys(BUCKETS, 0)
    for instant, mode in paid_minutes(facts, row["date"]):
        local = instant.astimezone(ZONE)
        if local.weekday() >= 5:
            raise ValueError("В смене есть выходные часы. Условие +100% требует отдельного явного выбора.")
        if (mode == "overtime") != (scheme == "overtime"):
            raise ValueError("Сохранённый режим отличается от схемы. Распределите часы явно; вечер не означает Övertid.")
        hour = local.hour
        bucket = ("16-17" if hour == 16 else "17-19" if 17 <= hour < 19 else
                  "19-22" if 19 <= hour < 22 else "22-06" if hour >= 22 or hour < 6 else None)
        if bucket is None or (scheme == "overtime" and bucket == "16-17"):
            raise ValueError("В точном времени есть неописанный интервал. Уточните его, не подставляйте условие.")
        minutes[bucket] += 1
    return {bucket: str(Decimal(value) / 60) for bucket, value in minutes.items()}


def confirmed_quote(row, previous, scheme, quantities, *, weekend=False, confirmed_at):
    if row["invoiced"] or previous.get("fixed"):
        raise ValueError("Закрытая запись / Fast pris защищены; расчёт не изменён.")
    result = dict(previous)
    for key in ("interval_terms", "interval_calculation", "schedule_application", "supplement_allocation"):
        result.pop(key, None)
    result.update(version=1, company=row["company"], object_name=row["object_name"],
                  date_from=row["date"], date_to=row["date"], source="Готовая схема: явное распределение часов",
                  confirmed=True, confirmed_at=confirmed_at)
    result["supplement_allocation"] = dict(
        allocation(row, result["base"], scheme, quantities, weekend=weekend), confirmed_at=confirmed_at)
    return result
