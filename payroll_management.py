"""Read-only time handoff and explicitly provisional current-rate estimates."""
from datetime import date, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from ob_calculator import _localize, _parse_time
import payroll_history as history


VERSION = "management-ob-2026-10-v1"
NOTICE = "Предварительный расчёт по текущим ставкам"
CATEGORIES = ("day", "ob1", "ob2", "ob3", "helg", "unknown")
LABELS = dict(day="Без OB", ob1="OB1 +20%", ob2="OB2 +40%",
              ob3="OB3 ночь +70%", helg="OB3 (Helg) +70%", unknown="OB не определён")
PREMIUM = dict(day=0, ob1=.2, ob2=.4, ob3=.7, helg=.7)


def split_interval(day, start, end, zone, holidays):
    """Split exact local times; never round or stack weekday and holiday premiums."""
    first = date.fromisoformat(day)
    left = _parse_time(start)
    right = _parse_time(end, allow_end_of_day=True)
    if left == right:
        raise ValueError("Время начала и окончания совпадает.")
    last = first + timedelta(days=right <= left)
    if right == 1440:
        last, right = first + timedelta(days=1), 0
    tz = ZoneInfo(zone["timezone"]) if zone else None
    policy = zone["ambiguous_policy"] if zone else "reject"
    def instant(day, minute):
        if tz:
            return _localize(day, minute, tz, policy).astimezone(timezone.utc)
        from datetime import datetime, time
        return datetime.combine(day, time()) + timedelta(minutes=minute)
    result = []
    current = first
    while current <= last:
        start_minute = left if current == first else 0
        end_minute = right if current == last else 1440
        weekend = current.weekday() >= 5 or current in holidays(current.year)
        periods = ((0, 1440, "helg"),) if weekend else (
            (0, 300, "ob3"), (300, 360, "ob1"), (360, 1020, "day"),
            (1020, 1080, "ob1"), (1080, 1320, "ob2"), (1320, 1440, "ob3"))
        for lower, upper, category in periods:
            a, b = max(lower, start_minute), min(upper, end_minute)
            if b <= a:
                continue
            hours = (instant(current, b)-instant(current, a)).total_seconds()/3600
            if hours <= 0:
                raise ValueError("Неположительная длительность сегмента.")
            result.append(dict(date=current.isoformat(), category=category, hours=hours))
        current += timedelta(days=1)
    return result


def line(row, saved, zone, start, end, rate, holidays):
    kind = row.get("entry_type", "work")
    if kind not in ("work", "ob", "atf", "sick", "vacation", "holiday"):
        return None
    hours = history.number(row["hours"], 48)
    warnings, segments = [], []
    if kind in ("work", "ob"):
        try:
            if row.get("start_time") and row.get("end_time"):
                segments = split_interval(row["date"], row["start_time"], row["end_time"], zone, holidays)
            elif saved:
                for item in saved:
                    parts = split_interval(item["segment_date"], item["start_time"], item["end_time"], zone, holidays)
                    if abs(sum(p["hours"] for p in parts)-float(item["hours"])) > .00001:
                        raise ValueError("Сохранённые часы не совпадают с временными границами.")
                    segments.extend(parts)
            else:
                missing = ("начала и окончания" if not row.get("start_time") and not row.get("end_time")
                           else "начала" if not row.get("start_time") else "окончания")
                raise ValueError(f"Нет сохранённого времени {missing}; OB-сегменты отсутствуют. "
                                 "По одному количеству часов разбивка OB не определяется.")
            if abs(sum(p["hours"] for p in segments)-hours) > .00001:
                raise ValueError("Часы не совпадают с интервалом: нужно уточнить время неоплачиваемого перерыва или переход часов.")
            if not zone:
                warnings.append("Зона объекта не подтверждена: разбивка по местным часам, переходы времени не проверены.")
        except ValueError as error:
            warnings.append(str(error))
            segments = [dict(date=row["date"], category="unknown", hours=hours)]
    else:
        segments = [dict(date=row["date"], category=kind, hours=hours)]
    segments = [s for s in segments if start <= s["date"] <= end]
    if not segments:
        return None
    categories = {key: sum(s["hours"] for s in segments if s["category"] == key) for key in CATEGORIES}
    worked = sum(categories.values())
    base = history.money(Decimal(str(worked))*Decimal(str(rate))) if rate is not None else None
    premium = None
    if rate is not None and not categories["unknown"]:
        premium = history.money(sum(Decimal(str(categories[key]))*Decimal(str(value))*Decimal(str(rate))
                                    for key, value in PREMIUM.items()))
    if rate is None and worked:
        warnings.append("Зарплатная ставка в справочнике не задана: выгружены часы без суммы.")
    return dict(sync_id=row["sync_id"], date=row["date"], company=row["company"],
                object_name=row["object_name"], kind=kind, hours=sum(s["hours"] for s in segments),
                start_time=row.get("start_time"), end_time=row.get("end_time"),
                saved_intervals=[dict(date=s["segment_date"], start_time=s["start_time"],
                                      end_time=s["end_time"], hours=s["hours"]) for s in saved],
                categories=categories, segments=segments, rate=rate,
                base=base, premium=premium,
                total=history.money(base+premium) if base is not None and premium is not None else None,
                warnings=warnings)


def reports(cursor, start, end, holidays, norm):
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("Начало периода позже окончания.")
    rates = {row["name"]: row["salary_rate"] for row in cursor.execute("SELECT name,salary_rate FROM employees")}
    _, zones, periods = history.state(history.events(cursor))
    previous = (date.fromisoformat(start)-timedelta(days=1)).isoformat()
    employees = [r[0] for r in cursor.execute(
        "SELECT DISTINCT employee FROM shifts WHERE date>=? AND date<=? ORDER BY employee", (previous, end))]
    result = []
    for employee in employees:
        value = rates.get(employee)
        # Legacy zero was the default for a missing catalogue rate, not evidence of free work.
        rate = history.number(value) if value is not None else None
        if rate == 0:
            rate = None
        lines = []
        for row, saved in history.source_rows(cursor, "shifts", employee, start, end):
            item = line(row, saved, zones.get((row["company"], row["object_name"])),
                        start, end, rate, holidays)
            if item:
                lines.append(item)
        if not lines:
            continue
        categories = {key: sum(l["categories"][key] for l in lines) for key in CATEGORIES}
        worked = sum(categories.values())
        work_lines = [l for l in lines if l["kind"] in ("work", "ob")]
        def total(key):
            return None if any(l[key] is None for l in work_lines) else history.money(sum(l[key] for l in work_lines))
        result.append(dict(
            mode="management", employee=employee, date_from=start, date_to=end, rules_version=VERSION,
            lines=lines, categories=categories, worked=worked, rate=rate,
            base=total("base"), premium=total("premium"), total=total("total"),
            above_norm=max(0, worked-norm(start, end)),
            absence={key: sum(l["hours"] for l in lines if l["kind"] == key)
                     for key in ("atf", "sick", "vacation", "holiday")},
            saved_periods=sum(name == employee and left <= end and start <= right for name, left, right in periods),
        ))
    return result
