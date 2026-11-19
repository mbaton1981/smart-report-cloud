"""Read-only payroll classification from working time, never customer prices."""
from collections import Counter, defaultdict
from datetime import date, timedelta, timezone
import json
from zoneinfo import ZoneInfo

from ob_calculator import _localize, _parse_time
import customer_intervals


DEFAULT_SCHEDULE = dict(days=[0, 1, 2, 3, 4], start="07:00", end="16:00",
                        breaks=[dict(start="12:00", end="13:00")])
OVERTIME = ("ot_a", "ot_b", "ot_c", "ot_d")
PREMIUM = dict(day=0, ob1=.2, ob2=.4, ob3=.7, helg=.7,
               ot_a=.3, ot_b=.5, ot_c=.7, ot_d=1, unknown=0)
LABELS = dict(day="Обычные", ob1="OB1 +20%", ob2="OB2 +40%", ob3="OB3 ночь +70%",
              helg="OB выходные +70%", unknown="Требует уточнения",
              ot_a="Övertid A +30%", ot_b="Övertid B +50%",
              ot_c="Övertid C +70%", ot_d="Övertid D +100%")


def validate_schedule(value):
    if not isinstance(value, dict):
        raise ValueError("Не задан рабочий график.")
    days = value.get("days")
    if (not isinstance(days, list) or not days or
            any(type(d) is not int or not 0 <= d <= 6 for d in days)):
        raise ValueError("Выберите рабочие дни недели.")
    if _parse_time(value.get("end")) <= _parse_time(value.get("start")):
        raise ValueError("Обычный график должен начинаться и заканчиваться в пределах одного дня.")
    facts = customer_intervals.time_facts(dict(version=1, start=value.get("start"),
        end=value.get("end"), breaks=value.get("breaks", [])), "2026-01-12")
    return dict(days=sorted(set(days)), start=facts["start"], end=facts["end"], breaks=facts["breaks"])


def validate_settings(value):
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("schedules"), list):
        raise ValueError("Некорректные настройки автоматического расчёта зарплаты.")
    result, keys = [], set()
    for entry in value["schedules"]:
        employee = entry.get("employee")
        if employee is not None and (not isinstance(employee, str) or not employee.strip()):
            raise ValueError("Некорректный сотрудник графика.")
        effective = date.fromisoformat(entry["effective_from"]).isoformat()
        key = employee, effective
        if key in keys:
            raise ValueError("Повтор графика сотрудника с одной даты.")
        keys.add(key)
        result.append(dict(employee=employee, effective_from=effective,
                           schedule=validate_schedule(entry["schedule"])))
    return dict(version=1, schedules=result)


def schedule_for(settings, employee, day):
    entries = [e for e in settings["schedules"] if e["effective_from"] <= day
               and e["employee"] in (None, employee)]
    personal = [e for e in entries if e["employee"] == employee]
    chosen = max(personal or entries, key=lambda e: e["effective_from"], default=None)
    return chosen["schedule"] if chosen else DEFAULT_SCHEDULE


def schedule_minutes(schedule):
    result = set(range(_parse_time(schedule["start"]), _parse_time(schedule["end"])))
    for gap in schedule["breaks"]:
        result.difference_update(range(_parse_time(gap["start"]), _parse_time(gap["end"])))
    return result


def _interval(day, start, end, zone):
    first = date.fromisoformat(day)
    a, b = _parse_time(start), _parse_time(end, allow_end_of_day=True)
    if a == b:
        raise ValueError("Время начала и окончания совпадает.")
    if b <= a:
        b += 1440
    tz = ZoneInfo(zone["timezone"] if zone else "Europe/Stockholm")
    policy = zone["ambiguous_policy"] if zone else "reject"
    def instant(offset):
        d, minute = divmod(offset, 1440)
        return _localize(first+timedelta(days=d), minute, tz, policy).astimezone(timezone.utc)
    point, stop = instant(a), instant(b)
    minutes = []
    while point < stop:
        local = point.astimezone(tz)
        minutes.append((point, local.date().isoformat(), local.hour*60+local.minute))
        point += timedelta(minutes=1)
    return minutes


def _minutes(row, saved, zone, schedule):
    """Explicit breaks take precedence; a standard break is inferred only on exact agreement."""
    facts = row.get("time_facts")
    if facts:
        facts = customer_intervals.time_facts(facts, row["date"], row["hours"])
        # time_facts is explicitly stored in Stockholm local time.
        if zone and zone["timezone"] != "Europe/Stockholm":
            raise ValueError("Часовая зона фактического времени отличается от зоны объекта.")
        values = []
        for instant, _mode in customer_intervals.paid_minutes(facts, row["date"]):
            local = instant.astimezone(customer_intervals.ZONE)
            values.append((instant, local.date().isoformat(), local.hour*60+local.minute))
        return values, "Фактическое время и сохранённые перерывы"
    if row.get("start_time") and row.get("end_time"):
        values = _interval(row["date"], row["start_time"], row["end_time"], zone)
    elif saved:
        values = []
        for segment in saved:
            part = _interval(segment["segment_date"], segment["start_time"], segment["end_time"], zone)
            if abs(len(part)/60-float(segment["hours"])) > .00001:
                raise ValueError("Сохранённые часы не совпадают с временными границами.")
            values.extend(part)
    else:
        if row.get("start_time") or row.get("end_time"):
            missing = "начала" if not row.get("start_time") else "окончания"
            raise ValueError(f"Нет сохранённого времени {missing} смены.")
        return None, "Обычная смена без интервала: по введённому количеству часов"
    if len({v[0] for v in values}) != len(values):
        raise ValueError("Сохранённые интервалы пересекаются.")
    if abs(len(values)/60-float(row["hours"])) < .00001:
        return values, "Сохранённый интервал"
    breaks = set()
    for gap in schedule["breaks"]:
        breaks.update(range(_parse_time(gap["start"]), _parse_time(gap["end"])))
    reduced = [v for v in values if not (v[1] == row["date"] and v[2] in breaks)]
    if (date.fromisoformat(row["date"]).weekday() in schedule["days"] and
            abs(len(reduced)/60-float(row["hours"])) < .00001):
        return reduced, "Интервал с перерывом по рабочему графику"
    raise ValueError("Часы не совпадают с интервалом: уточните время неоплачиваемого перерыва.")


def _category(day, minute, holidays, overtime=False):
    if day.weekday() >= 5 or day in holidays(day.year):
        return "helg"
    if overtime:
        return ("ot_d" if minute < 300 or minute >= 1320 else "ot_b" if minute < 360
                or 1020 <= minute < 1140 else "ot_c" if minute >= 1140 else "ot_a")
    return ("ob3" if minute < 300 or minute >= 1320 else "ob1" if minute < 360
            else "ob2" if minute >= 1080 else "day")


def classify(sources, employee, zones, holidays, settings=None):
    """Aggregate across ALL objects before filtering dates. Privat never earns premiums."""
    settings = validate_settings(settings or dict(version=1, schedules=[]))
    holiday_source, holiday_cache = holidays, {}
    def holidays(year):
        if year not in holiday_cache:
            holiday_cache[year] = holiday_source(year)
        return holiday_cache[year]
    prepared, occupancy = {}, Counter()
    ordinary = defaultdict(float)
    day_schedules = {}
    def day_schedule(day):
        if day not in day_schedules:
            schedule = schedule_for(settings, employee, day)
            day_schedules[day] = (schedule, schedule_minutes(schedule),
                                  _parse_time(schedule["start"]), _parse_time(schedule["end"]))
        return day_schedules[day]
    for row, saved in sources:
        if row.get("entry_type", "work") not in ("work", "ob"):
            continue
        private = str(row["company"]).strip().casefold() == "privat"
        schedule = schedule_for(settings, employee, row["date"])
        try:
            points, explanation = _minutes(row, saved, zones.get((row["company"], row["object_name"])), schedule)
            if points is None and row.get("entry_type") == "ob" and not private:
                raise ValueError("Нет начала и окончания OB-смены: доплату определить нельзя.")
            error = None
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            points, explanation, error = None, "", str(exc)
        prepared[row["sync_id"]] = dict(row=row, points=points, private=private,
                                        explanation=explanation, error=error)
        if points:
            occupancy.update(p[0] for p in points)
        elif not error and (row.get("entry_type") == "work" or private):
            ordinary[row["date"]] += float(row["hours"])*60
    # A full ordinary day must be evidenced before an extra interval becomes overtime.
    for item in prepared.values():
        for instant, day, minute in item["points"] or []:
            schedule, paid, first, last = day_schedule(day)
            if occupancy[instant] == 1 and date.fromisoformat(day).weekday() in schedule["days"]:
                if first <= minute < last:
                    ordinary[day] += 1
    result = {}
    for key, item in prepared.items():
        row, private = item["row"], item["private"]
        points, warnings = item["points"], []
        if item["error"]:
            warnings.append(item["error"])
        segments = []
        if points:
            overlapping = any(occupancy[p[0]] > 1 for p in points)
            if overlapping:
                warnings.append("Смены сотрудника пересекаются по времени; проверьте записи по всем объектам.")
            previous_overtime = False
            for instant, day, minute in points:
                schedule, paid, first, last = day_schedule(day)
                d = date.fromisoformat(day)
                overtime = (d.weekday() in schedule["days"] and
                            not first <= minute < last
                            and (ordinary[day] >= len(paid) or
                                 previous_overtime and minute < first) and d.weekday() < 5
                            and d not in holidays(d.year))
                category = ("day" if private else "unknown" if overlapping else
                            _category(d, minute, holidays, overtime))
                previous_overtime = category in OVERTIME
                if (segments and segments[-1]["category"] == category and segments[-1]["date"] == day
                        and segments[-1]["until"] == instant):
                    segments[-1]["hours"] += 1/60
                    segments[-1]["until"] = instant+timedelta(minutes=1)
                    segments[-1]["end_time"] = f"{(minute+1)//60:02}:{(minute+1)%60:02}"
                else:
                    segments.append(dict(date=day, category=category, hours=1/60,
                        start_time=f"{minute//60:02}:{minute%60:02}",
                        end_time=f"{(minute+1)//60:02}:{(minute+1)%60:02}", until=instant+timedelta(minutes=1)))
        else:
            category = "day" if private or not item["error"] else "unknown"
            segments = [dict(date=row["date"], category=category, hours=float(row["hours"]),
                             start_time=None, end_time=None)]
        for segment in segments:
            segment.pop("until", None)
            segment["hours"] = round(segment["hours"], 8)
        explanation = ("Privat: все часы обычные, без OB и Övertid" if private else item["explanation"])
        if any(s["category"] in OVERTIME for s in segments):
            explanation += "; обычный рабочий день отработан, дополнительные интервалы — Övertid, без OB"
        result[key] = dict(segments=segments, warnings=warnings, explanation=explanation)
    return result
