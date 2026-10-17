import json
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_END_OF_DAY = "24:00"
_CENT = Decimal("0.01")


def load_ob_rules(config_path=None):
    path = Path(config_path) if config_path else Path(__file__).with_name("ob_rules.json")
    with path.open("r", encoding="utf-8") as config_file:
        return json.load(config_file)


def _parse_date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError("Дата должна иметь формат YYYY-MM-DD") from error


def _parse_time(value, allow_end_of_day=False):
    text = str(value)
    if allow_end_of_day and text == _END_OF_DAY:
        return 24 * 60
    if not _TIME_PATTERN.fullmatch(text):
        raise ValueError("Время должно иметь формат HH:MM")
    hour, minute = (int(part) for part in text.split(":"))
    return hour * 60 + minute


def _round_minute_to_half_hour(minute):
    return ((minute + 15) // 30) * 30


def round_time_to_half_hour(value):
    """Normalize a local clock time to the nearest HH:00 or HH:30 (half-up)."""
    rounded = _round_minute_to_half_hour(_parse_time(value)) % (24 * 60)
    return f"{rounded // 60:02d}:{rounded % 60:02d}"


def _decimal_rate(value):
    if isinstance(value, bool):
        raise ValueError("Базовая ставка должна быть числом")
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("Базовая ставка должна быть числом") from error
    if not rate.is_finite() or rate < 0:
        raise ValueError("Базовая ставка не может быть отрицательной или бесконечной")
    return rate


def _localize(day, minute, zone, ambiguous_policy):
    if minute == 24 * 60:
        day += timedelta(days=1)
        minute = 0
    wall_time = time(minute // 60, minute % 60)
    naive = datetime.combine(day, wall_time)
    candidates = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == naive:
            candidates.append(candidate)
    unique_instants = {candidate.utcoffset() for candidate in candidates}
    if not candidates:
        raise ValueError("Указанное местное время не существует из-за перехода на летнее время")
    if len(unique_instants) > 1:
        if ambiguous_policy == "reject":
            raise ValueError("Указанное местное время неоднозначно из-за перехода на зимнее время")
        candidates.sort(key=lambda candidate: candidate.astimezone(timezone.utc))
        return candidates[0] if ambiguous_policy == "earlier" else candidates[-1]
    return candidates[0]


def _rules_for_date(day, config):
    holidays = config.get("holidays", {})
    if day.isoformat() in holidays:
        return holidays[day.isoformat()]
    if day.weekday() == 5:
        return config["saturday"]
    if day.weekday() == 6:
        return config["sunday"]
    return config["weekdays"]


def _validated_periods(day, config):
    periods = _rules_for_date(day, config)
    if not isinstance(periods, list) or not periods:
        raise ValueError(f"Для даты {day.isoformat()} не настроены OB-интервалы")
    validated = []
    for period in periods:
        start = _parse_time(period.get("start"))
        end = _parse_time(period.get("end"), allow_end_of_day=True)
        try:
            premium = Decimal(str(period["premium_percent"]))
            multiplier = Decimal(str(period["multiplier"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("Некорректный OB-тариф в конфигурации") from error
        paid = bool(period.get("paid", True))
        if start >= end or premium < 0 or multiplier < 0 or not premium.is_finite() or not multiplier.is_finite():
            raise ValueError("Некорректные границы или тариф OB в конфигурации")
        expected_multiplier = Decimal("1") + premium / Decimal("100") if paid else Decimal("0")
        if multiplier != expected_multiplier or (not paid and premium != 0):
            raise ValueError("Коэффициент должен соответствовать проценту надбавки")
        validated.append((start, end, period, premium, multiplier, paid))
    validated.sort(key=lambda item: item[0])
    if validated[0][0] != 0 or validated[-1][1] != 24 * 60:
        raise ValueError("OB-конфигурация должна покрывать все сутки")
    if any(left[1] != right[0] for left, right in zip(validated, validated[1:])):
        raise ValueError("В OB-конфигурации есть пересечение или незаполненный временной интервал")
    return validated


def describe_category_time_ranges(date_from, date_to, category_labels, rules=None):
    start_day = _parse_date(date_from)
    end_day = _parse_date(date_to)
    if start_day > end_day:
        raise ValueError("Начало периода должно быть не позже окончания")
    config = rules if rules is not None else load_ob_rules()
    weekday_labels = (
        "Måndag", "Tisdag", "Onsdag", "Torsdag", "Fredag", "Lördag", "Söndag"
    )
    holidays = config.get("holidays", {})
    holiday_days = {}
    for holiday_text in holidays:
        try:
            holiday_day = date.fromisoformat(str(holiday_text))
        except (TypeError, ValueError):
            continue
        if start_day <= holiday_day <= end_day:
            holiday_days[holiday_day] = str(holiday_text)

    matches = {category: {} for category in category_labels}

    def add_day_rules(day, day_label):
        for _period_start, _period_end, period, premium, _multiplier, paid in _validated_periods(day, config):
            category = str(period.get("category", ""))
            if category not in matches or not paid or premium <= 0:
                continue
            key = (day_label, str(period["start"]), str(period["end"]))
            matches[category][key] = True

    for weekday in range(7):
        first_day = start_day + timedelta(days=(weekday - start_day.weekday()) % 7)
        if first_day > end_day:
            continue
        occurrence_count = (end_day - first_day).days // 7 + 1
        holiday_occurrences = sum(day.weekday() == weekday for day in holiday_days)
        if occurrence_count <= holiday_occurrences:
            continue
        candidate = first_day
        while candidate in holiday_days:
            candidate += timedelta(days=7)
        add_day_rules(candidate, weekday_labels[weekday])

    for holiday_day, holiday_text in sorted(holiday_days.items()):
        add_day_rules(holiday_day, f"Helgdag {holiday_text}")

    descriptions = {}
    for category, label in category_labels.items():
        ranges = matches[category]
        if not ranges:
            descriptions[label] = "Ej tillämpligt enligt reglerna för vald period"
            continue
        ranges_by_time = {}
        for day_label, start, end in ranges:
            ranges_by_time.setdefault((start, end), set()).add(day_label)

        formatted_ranges = []
        for (start, end), day_labels in sorted(ranges_by_time.items()):
            weekday_numbers = sorted(
                weekday_labels.index(day_label)
                for day_label in day_labels if day_label in weekday_labels
            )
            day_groups = []
            if weekday_numbers:
                run_start = previous = weekday_numbers[0]
                for weekday in weekday_numbers[1:]:
                    if weekday == previous + 1:
                        previous = weekday
                        continue
                    day_groups.append(
                        weekday_labels[run_start]
                        if run_start == previous
                        else f"{weekday_labels[run_start]}–{weekday_labels[previous]}"
                    )
                    run_start = previous = weekday
                day_groups.append(
                    weekday_labels[run_start]
                    if run_start == previous
                    else f"{weekday_labels[run_start]}–{weekday_labels[previous]}"
                )
            day_groups.extend(sorted(
                day_label for day_label in day_labels if day_label not in weekday_labels
            ))
            formatted_ranges.append(f"{', '.join(day_groups)} {start}–{end}")
        descriptions[label] = "; ".join(formatted_ranges)
    return descriptions


def calculate_work_interval(work_date, start_time, end_time, base_rate, rules=None):
    """Split a Stockholm local-time interval into configured rate segments."""
    config = rules if rules is not None else load_ob_rules()
    try:
        zone = ZoneInfo(config["timezone"])
    except (KeyError, ZoneInfoNotFoundError) as error:
        raise ValueError("В конфигурации указана неизвестная временная зона") from error
    ambiguous_policy = config.get("ambiguous_time_policy", "reject")
    if ambiguous_policy not in ("earlier", "later", "reject"):
        raise ValueError("Некорректная политика неоднозначного местного времени")

    start_day = _parse_date(work_date)
    raw_start_minute = _parse_time(start_time)
    raw_end_minute = _parse_time(end_time, allow_end_of_day=True)
    if raw_start_minute == raw_end_minute:
        raise ValueError("Время начала и окончания не может совпадать")
    rate = _decimal_rate(base_rate)
    end_absolute_minute = raw_end_minute if raw_end_minute > raw_start_minute else raw_end_minute + 24 * 60
    start_absolute_minute = _round_minute_to_half_hour(raw_start_minute)
    end_absolute_minute = _round_minute_to_half_hour(end_absolute_minute)
    if end_absolute_minute <= start_absolute_minute:
        raise ValueError("После округления продолжительность интервала должна быть положительной")
    start_day_offset, start_minute = divmod(start_absolute_minute, 24 * 60)
    end_day_offset, end_minute = divmod(end_absolute_minute, 24 * 60)
    original_start_day = start_day
    start_day = original_start_day + timedelta(days=start_day_offset)
    end_day = original_start_day + timedelta(days=end_day_offset)
    interval_start = _localize(start_day, start_minute, zone, ambiguous_policy)
    interval_end = _localize(end_day, end_minute, zone, ambiguous_policy)
    if interval_end.astimezone(timezone.utc) <= interval_start.astimezone(timezone.utc):
        raise ValueError("Продолжительность интервала должна быть положительной")

    segments = []
    total_seconds = Decimal("0")
    total_amount = Decimal("0")
    day = start_day
    while day <= end_day:
        day_start = start_minute if day == start_day else 0
        day_end = end_minute if day == end_day else 24 * 60
        for period_start, period_end, period, premium, multiplier, paid in _validated_periods(day, config):
            segment_start_minute = max(day_start, period_start)
            segment_end_minute = min(day_end, period_end)
            if segment_start_minute >= segment_end_minute:
                continue
            local_start = _localize(day, segment_start_minute, zone, ambiguous_policy)
            local_end = _localize(day, segment_end_minute, zone, ambiguous_policy)
            seconds = Decimal(str((local_end.astimezone(timezone.utc) - local_start.astimezone(timezone.utc)).total_seconds()))
            if seconds <= 0:
                continue
            hours = (seconds / Decimal("3600")).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            calculated_rate = rate * multiplier
            amount = (hours * calculated_rate).quantize(_CENT, rounding=ROUND_HALF_UP)
            segments.append({
                "date": day.isoformat(),
                "segment_date": day.isoformat(),
                "start_time": f"{segment_start_minute // 60:02d}:{segment_start_minute % 60:02d}",
                "end_time": "00:00" if segment_end_minute == 24 * 60 else f"{segment_end_minute // 60:02d}:{segment_end_minute % 60:02d}",
                "hours": float(hours),
                "category": str(period["category"]),
                "premium_percent": float(premium),
                "multiplier": float(multiplier),
                "paid": paid,
                "base_rate": float(rate),
                "calculated_rate": float(calculated_rate),
                "amount": float(amount),
            })
            total_seconds += seconds
            total_amount += amount
        day += timedelta(days=1)

    if not segments:
        raise ValueError("Интервал не содержит рассчитываемых сегментов")
    total_hours = (total_seconds / Decimal("3600")).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    return {
        "timezone": config["timezone"],
        "rules_version": str(config.get("version", "unversioned")),
        "start_date": start_day.isoformat(),
        "end_date": end_day.isoformat(),
        "start_time": f"{start_minute // 60:02d}:{start_minute % 60:02d}",
        "end_time": f"{end_minute // 60:02d}:{end_minute % 60:02d}",
        "total_hours": float(total_hours),
        "total_amount": float(total_amount),
        "segments": segments,
    }