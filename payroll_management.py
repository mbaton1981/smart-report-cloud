"""Read-only time handoff and explicitly provisional current-rate estimates."""
from datetime import date, timedelta
from decimal import Decimal

import payroll_history as history
import payroll_auto as automatic


VERSION = "management-auto-payroll-2026-11-v3"
NOTICE = "Предварительный расчёт по текущим ставкам"
CATEGORIES = ("day", "ob1", "ob2", "ob3", "helg", "unknown")
LABELS = dict(day="Без OB", ob1="OB1 +20%", ob2="OB2 +40%",
              ob3="OB3 ночь +70%", helg="OB3 (Helg) +70%", unknown="OB не определён")
PREMIUM = dict(day=0, ob1=.2, ob2=.4, ob3=.7, helg=.7)


def split_interval(day, start, end, zone, holidays):
    """Split exact local times; never round or stack weekday and holiday premiums."""
    result = []
    for _instant, segment_day, minute in automatic._interval(day, start, end, zone):
        category = automatic._category(date.fromisoformat(segment_day), minute, holidays)
        if result and result[-1]["date"] == segment_day and result[-1]["category"] == category:
            result[-1]["hours"] += 1/60
        else:
            result.append(dict(date=segment_day, category=category, hours=1/60))
    for segment in result:
        segment["hours"] = round(segment["hours"], 8)
    return result


def line(row, saved, zone, start, end, rate, holidays, classified=None):
    kind = row.get("entry_type", "work")
    if kind not in ("work", "ob", "atf", "sick", "vacation", "holiday"):
        return None
    hours = history.number(row["hours"], 48)
    warnings, segments = [], []
    if kind in ("work", "ob"):
        if classified is None:
            zones = {(row["company"], row["object_name"]): zone} if zone else {}
            classified = automatic.classify([(row, saved)], row.get("employee"), zones, holidays)[row["sync_id"]]
        segments, warnings = classified["segments"], classified["warnings"]
    else:
        segments = [dict(date=row["date"], category=kind, hours=hours)]
    segments = [s for s in segments if start <= s["date"] <= end]
    if not segments:
        return None
    categories = {key: sum(s["hours"] for s in segments if s["category"] == key) for key in CATEGORIES}
    overtime = {key: sum(s["hours"] for s in segments if s["category"] == key) for key in automatic.OVERTIME}
    worked = sum(categories.values()) + sum(overtime.values())
    base = history.money(Decimal(str(worked))*Decimal(str(rate))) if rate is not None else None
    premium = None
    if rate is not None:
        all_hours = dict(categories, **overtime)
        premium = history.money(sum(Decimal(str(all_hours[key]))*Decimal(str(value))*Decimal(str(rate))
                                    for key, value in automatic.PREMIUM.items()))
    if rate is None and worked:
        warnings.append("Зарплатная ставка в справочнике не задана: выгружены часы без суммы.")
    return dict(sync_id=row["sync_id"], date=row["date"], company=row["company"],
                object_name=row["object_name"], kind=kind, hours=sum(s["hours"] for s in segments),
                start_time=row.get("start_time"), end_time=row.get("end_time"),
                saved_intervals=[dict(date=s["segment_date"], start_time=s["start_time"],
                                      end_time=s["end_time"], hours=s["hours"]) for s in saved],
                categories=categories, overtime=overtime, segments=segments, rate=rate,
                explanation=classified["explanation"] if classified else "",
                base=base, premium=premium,
                total=history.money(base+premium) if base is not None and premium is not None else None,
                warnings=warnings)


def reports(cursor, start, end, holidays, norm, settings=None):
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("Начало периода позже окончания.")
    standard_norm = norm(start, end)
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
        sources = history.source_rows(cursor, "shifts", employee, start, end)
        classified = automatic.classify(sources, employee, zones, holidays, settings)
        for row, saved in sources:
            item = line(row, saved, zones.get((row["company"], row["object_name"])),
                        start, end, rate, holidays, classified.get(row["sync_id"]))
            if item:
                lines.append(item)
        if not lines:
            continue
        categories = {key: sum(l["categories"][key] for l in lines) for key in CATEGORIES}
        overtime = {key: sum(l["overtime"][key] for l in lines) for key in automatic.OVERTIME}
        worked = sum(categories.values()) + sum(overtime.values())
        work_lines = [l for l in lines if l["kind"] in ("work", "ob")]
        def total(key):
            return None if any(l[key] is None for l in work_lines) else history.money(sum(l[key] for l in work_lines))
        result.append(dict(
            mode="management", employee=employee, date_from=start, date_to=end, rules_version=VERSION,
            lines=lines, categories=categories, overtime=overtime, worked=worked, rate=rate,
            base=total("base"), premium=total("premium"), total=total("total"),
            standard_norm=standard_norm, norm=standard_norm, norm_override=False,
            above_norm=max(0, worked-standard_norm),
            absence={key: sum(l["hours"] for l in lines if l["kind"] == key)
                     for key in ("atf", "sick", "vacation", "holiday")},
            saved_periods=sum(name == employee and left <= end and start <= right for name, left, right in periods),
        ))
    return result
