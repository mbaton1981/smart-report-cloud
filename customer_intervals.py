"""Actual time and versioned customer supplements, independent of payroll OB."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import uuid
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Stockholm")
CENT = Decimal("0.01")
DAYS = ("weekday", "saturday", "sunday", "holiday")
MODES = ("normal", "overtime")


def number(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Ожидалось конечное числовое значение.") from None
    if isinstance(value, bool) or not result.is_finite():
        raise ValueError("Ожидалось конечное числовое значение.")
    return result


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def minute(value):
    if not isinstance(value, str) or len(value) != 5:
        raise ValueError("Время укажите в формате HH:MM.")
    parsed = datetime.strptime(value, "%H:%M")
    if parsed.strftime("%H:%M") != value:
        raise ValueError("Время укажите в формате HH:MM.")
    return parsed.hour * 60 + parsed.minute


def local_instant(day, minutes):
    naive = datetime.combine(date.fromisoformat(day), datetime.min.time()) + timedelta(minutes=minutes)
    first, second = (naive.replace(tzinfo=ZONE, fold=fold) for fold in (0, 1))
    if first.utcoffset() != second.utcoffset():
        raise ValueError("Переход летнего/зимнего времени: уточните неоднозначный интервал отдельно.")
    if first.astimezone(timezone.utc).astimezone(ZONE).replace(tzinfo=None) != naive:
        raise ValueError("Указанное местное время не существует при переходе часов.")
    return first.astimezone(timezone.utc)


def time_facts(value, work_date, expected_hours=None):
    if value in (None, ""):
        return None
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("Неизвестный формат фактического времени.")
    start, end = minute(value.get("start")), minute(value.get("end"))
    if not isinstance(value.get("next_day", False), bool):
        raise ValueError("Отметка следующего дня должна быть логической.")
    if value.get("next_day", False):
        end += 1440
    if not start < end <= start + 1440:
        raise ValueError("Окончание должно быть позже начала; для полуночи отметьте следующий день.")
    bounds = (local_instant(work_date, start), local_instant(work_date, end))
    result = {"version": 1, "start": value["start"], "end": value["end"],
              "next_day": value.get("next_day", False), "breaks": [], "overtime": []}
    ranges = {}
    for key in ("breaks", "overtime"):
        entries = value.get(key, [])
        if not isinstance(entries, list) or len(entries) > 24:
            raise ValueError("Не более 24 интервалов перерыва или сверхурочной работы.")
        spans = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Некорректный интервал времени.")
            left, right = minute(entry.get("start")), minute(entry.get("end"))
            if left < start:
                left += 1440
            if right <= left % 1440:
                right += 1440
            elif left >= 1440:
                right += 1440
            if not start <= left < right <= end:
                raise ValueError("Перерыв и сверхурочная часть должны находиться внутри смены.")
            if any(left < b and right > a for a, b in spans):
                raise ValueError("Интервалы одного вида не должны пересекаться.")
            local_instant(work_date, left)
            local_instant(work_date, right)
            spans.append((left, right))
        spans.sort()
        ranges[key] = spans
        result[key] = [{"start": f"{a % 1440 // 60:02}:{a % 60:02}",
                        "end": f"{b % 1440 // 60:02}:{b % 60:02}"} for a, b in spans]
    paid = (bounds[1] - bounds[0]).total_seconds() / 3600
    paid -= sum((local_instant(work_date, b) - local_instant(work_date, a)).total_seconds() / 3600
                for a, b in ranges["breaks"])
    if not 0 < paid <= 24:
        raise ValueError("Оплачиваемое время должно быть больше нуля и не больше 24 часов.")
    if expected_hours is not None and abs(paid - float(expected_hours)) > 0.000001:
        raise ValueError(f"Фактическое время {paid:g} ч не совпадает с сохранёнными {expected_hours:g} ч. "
                         "Уточните часы отдельно; зарплатные OB-сегменты не изменены.")
    return result


def paid_minutes(facts, work_date):
    facts = time_facts(facts, work_date)
    if facts is None:
        raise ValueError("Нет начала, окончания, перерывов и подтверждённого режима. Уточните время в десктопе.")
    start = minute(facts["start"])
    end = minute(facts["end"]) + 1440 * facts["next_day"]

    def spans(key):
        result = []
        for entry in facts[key]:
            a, b = minute(entry["start"]), minute(entry["end"])
            if a < start:
                a += 1440
            if b <= a % 1440 or a >= 1440:
                b += 1440
            result.append((a, b))
        return result

    breaks, overtime = spans("breaks"), spans("overtime")
    point, stop = local_instant(work_date, start), local_instant(work_date, end)
    while point < stop:
        local = point.astimezone(ZONE)
        offset = (local.date() - date.fromisoformat(work_date)).days * 1440 + local.hour * 60 + local.minute
        if not any(a <= offset < b for a, b in breaks):
            yield point, "overtime" if any(a <= offset < b for a, b in overtime) else "normal"
        point += timedelta(minutes=1)


def paid_hours(facts, work_date):
    return sum(1 for _ in paid_minutes(facts, work_date)) / 60


def terms(value):
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("Неизвестная версия клиентских условий.")
    date.fromisoformat(value.get("effective_from", ""))
    holidays = value.get("holidays", [])
    if not isinstance(holidays, list) or len(holidays) > 366:
        raise ValueError("Укажите явные даты праздников (не более 366).")
    for day in holidays:
        date.fromisoformat(day)
    rules = value.get("rules")
    if not isinstance(rules, list) or len(rules) > 100:
        raise ValueError("Не более 100 правил клиентских доплат.")
    checked, coverage = [], {}
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("day") not in DAYS or rule.get("mode") not in MODES:
            raise ValueError("Укажите день и режим каждого клиентского правила.")
        start, end = minute(rule.get("start")), minute(rule.get("end"))
        raw = rule.get("percent")
        percent = None if raw in (None, "") else number(raw)
        if isinstance(raw, bool) or (percent is not None and (not percent.is_finite() or not 0 <= percent <= 1000)):
            raise ValueError("Доплата — от 0 до 1000%; пусто означает неизвестно, 0 — подтверждено без доплаты.")
        points = set(range(start, end)) if end > start else set(range(start, 1440)) | set(range(end))
        key = rule["day"], rule["mode"]
        if coverage.setdefault(key, set()) & points:
            raise ValueError("Правила одного дня и режима пересекаются; доплаты не складываются.")
        coverage[key].update(points)
        checked.append({key: rule[key] for key in ("day", "mode", "start", "end")} |
                       {"percent": str(percent) if percent is not None else None})
    return {"version": 1, "effective_from": value["effective_from"],
            "holidays": sorted(set(holidays)), "rules": checked}


def calculate(facts, work_date, base, conditions):
    conditions = terms(conditions)
    if conditions["effective_from"] > work_date:
        raise ValueError("Клиентские условия ещё не действовали на дату смены.")
    base = None if base is None else number(base)
    if base is not None and (not base.is_finite() or not 0 <= base <= 100000):
        raise ValueError("Некорректная историческая базовая цена.")
    pieces = []
    for instant, mode in paid_minutes(facts, work_date):
        local = instant.astimezone(ZONE)
        day = ("holiday" if local.date().isoformat() in conditions["holidays"] else
               "saturday" if local.weekday() == 5 else "sunday" if local.weekday() == 6 else "weekday")
        minute_of_day = local.hour * 60 + local.minute
        matched = []
        for rule in conditions["rules"]:
            if rule["day"] != day or rule["mode"] != mode:
                continue
            a, b = minute(rule["start"]), minute(rule["end"])
            if (a <= minute_of_day < b if b > a else minute_of_day >= a or minute_of_day < b):
                matched.append(rule)
        percent = matched[0]["percent"] if len(matched) == 1 else None
        key = local.date().isoformat(), mode, day, percent
        if pieces and pieces[-1]["key"] == key and pieces[-1]["stop"] == instant:
            pieces[-1]["stop"] += timedelta(minutes=1)
            pieces[-1]["minutes"] += 1
        else:
            pieces.append({"key": key, "start": instant, "stop": instant + timedelta(minutes=1), "minutes": 1})
    output = []
    for piece in pieces:
        day, mode, kind, percent = piece["key"]
        hours = Decimal(piece["minutes"]) / 60
        premium_rate = base * Decimal(percent) / 100 if base is not None and percent is not None else None
        basic = (base * hours).quantize(CENT, rounding=ROUND_HALF_UP) if base is not None else None
        premium = (premium_rate * hours).quantize(CENT, rounding=ROUND_HALF_UP) if premium_rate is not None else None
        output.append({
            "date": day, "start": piece["start"].astimezone(ZONE).isoformat(),
            "end": piece["stop"].astimezone(ZONE).isoformat(), "mode": mode, "day": kind,
            "hours": str(hours), "base": str(base) if base is not None else None, "percent": percent,
            "base_amount": str(basic) if basic is not None else None,
            "premium_amount": str(premium) if premium is not None else None,
            "total": str(basic + premium) if basic is not None and premium is not None else None,
        })
    return output


def ensure_schema(cursor, table):
    if table not in ("shifts", "incoming_submissions"):
        raise ValueError("Unsupported time table")
    if "time_facts" not in {r[1] for r in cursor.execute(f"PRAGMA table_info({table})")}:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN time_facts TEXT")
    cursor.execute("""CREATE TABLE IF NOT EXISTS customer_terms(
        id TEXT PRIMARY KEY, company TEXT NOT NULL, object_name TEXT NOT NULL,
        revision INTEGER NOT NULL, conditions_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(company,object_name,revision))""")
    for operation in ("UPDATE", "DELETE"):
        cursor.execute(f"""CREATE TRIGGER IF NOT EXISTS customer_terms_no_{operation.lower()}
            BEFORE {operation} ON customer_terms BEGIN
            SELECT RAISE(ABORT,'Customer terms history is immutable'); END""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS time_facts_audit(
        id INTEGER PRIMARY KEY, shift_sync_id TEXT NOT NULL, previous_json TEXT,
        current_json TEXT, changed_at TEXT NOT NULL)""")


def store_time(cursor, table, sync_id, value, *, from_sync=False, previous_row=None, protect_payroll=False):
    row = dict(cursor.execute(f"SELECT * FROM {table} WHERE sync_id=?", (sync_id,)).fetchone())
    old = dict(previous_row) if previous_row is not None else row
    normalized = time_facts(value, row["date"], row["hours"])
    data = encoded(normalized) if normalized is not None else None
    previous = old.get("time_facts")
    if data == previous and (previous is None or all(old[k] == row[k] for k in ("date", "hours"))):
        return False
    if row["invoiced"] and not from_sync:
        raise ValueError("Закрытая смена защищена; сначала явно верните её в работу.")
    if not from_sync or protect_payroll:
        import payroll_history
        _, _, periods = payroll_history.state(payroll_history.events(cursor))
        if any(employee == candidate["employee"] and start <= candidate["date"] <= end and period["status"] == "closed"
               for (employee, start, end), period in periods.items() for candidate in (row, old)):
            raise ValueError("Зарплатный период закрыт; уточнение времени требует его штатного открытия.")
    cursor.execute("INSERT INTO time_facts_audit VALUES(NULL,?,?,?,?)",
                   (sync_id, encoded({"date": old["date"], "hours": old["hours"], "time_facts": previous}),
                    encoded({"date": row["date"], "hours": row["hours"], "time_facts": data}),
                    datetime.now(timezone.utc).isoformat()))
    cursor.execute(f"UPDATE {table} SET time_facts=? WHERE sync_id=?", (data, sync_id))
    return True


def applicable_terms(cursor, row):
    if not cursor.execute("SELECT 1 FROM sqlite_master WHERE name='customer_terms' AND type='table'").fetchone():
        return None
    matches = []
    for item in cursor.execute("SELECT * FROM customer_terms WHERE company=? AND object_name=? ORDER BY revision",
                               (row["company"], row["object_name"])):
        conditions = terms(json.loads(item["conditions_json"]))
        if conditions["effective_from"] <= row["date"]:
            matches.append((conditions["effective_from"], item["revision"], dict(item)))
    return max(matches, key=lambda x: x[:2])[2] if matches else None


def attach_terms(cursor, row, quote):
    saved = applicable_terms(cursor, row)
    if saved is not None:
        quote = dict(quote, interval_terms={"id": saved["id"], "revision": saved["revision"],
                                          "conditions": json.loads(saved["conditions_json"])})
    return quote


def verify_terms_version(cursor, row, quote):
    saved = quote.get("interval_terms")
    if saved is None:
        return
    version = cursor.execute("SELECT * FROM customer_terms WHERE id=?", (saved["id"],)).fetchone()
    if (version is None or version["company"] != row["company"] or version["object_name"] != row["object_name"]
            or version["revision"] != saved["revision"]
            or terms(json.loads(version["conditions_json"])) != saved["conditions"]):
        raise ValueError("Клиентская версия условий не подтверждена серверной историей объекта.")


def calculated_row(row, quote):
    saved = quote.get("interval_terms")
    if not saved:
        raise ValueError("Клиентские условия по времени не подтверждены для этой записи.")
    if "interval_calculation" in quote:
        return validate_calculation(quote["interval_calculation"], row, quote)
    return calculate(row.get("time_facts"), row["date"], quote["base"], saved["conditions"])


def validate_calculation(parts, row, quote):
    if not isinstance(parts, list) or not parts or len(parts) > 1440:
        raise ValueError("Некорректный сохранённый клиентский расчёт.")
    required = {"date", "start", "end", "mode", "day", "hours", "base", "percent",
                "base_amount", "premium_amount", "total"}
    covered, hours = set(), Decimal(0)
    for part in parts:
        if not isinstance(part, dict) or set(part) != required or part["mode"] not in MODES or part["day"] not in DAYS:
            raise ValueError("Некорректный участок сохранённого расчёта.")
        start, end = datetime.fromisoformat(part["start"]), datetime.fromisoformat(part["end"])
        if start.tzinfo is None or end.tzinfo is None or not start < end:
            raise ValueError("Не задан однозначный интервал сохранённого расчёта.")
        length = Decimal(str((end - start).total_seconds())) / 3600
        if abs(length - number(part["hours"])) > Decimal("0.000000001"):
            raise ValueError("Часы сохранённого участка не соответствуют интервалу.")
        hours += length
        instant = start.astimezone(timezone.utc)
        stop = end.astimezone(timezone.utc)
        while instant < stop:
            if instant in covered:
                raise ValueError("Сохранённые клиентские участки пересекаются.")
            covered.add(instant)
            instant += timedelta(minutes=1)
        base = number(part["base"]) if part["base"] is not None else None
        percent = number(part["percent"]) if part["percent"] is not None else None
        if ((base is None) != (quote["base"] is None) or
                (base is not None and (not base.is_finite() or base != Decimal(str(quote["base"])) or base < 0))
                or (percent is not None and (not percent.is_finite() or not 0 <= percent <= 1000))):
            raise ValueError("Ставка сохранённого расчёта не совпадает с исторической ценой.")
        basic = (base * length).quantize(CENT, rounding=ROUND_HALF_UP) if base is not None else None
        premium = (base * length * percent / 100).quantize(CENT, rounding=ROUND_HALF_UP) if base is not None and percent is not None else None
        for key, expected in (("base_amount", basic), ("premium_amount", premium),
                              ("total", basic + premium if basic is not None and premium is not None else None)):
            actual = number(part[key]) if part[key] is not None else None
            if actual != expected:
                raise ValueError("Суммы сохранённого клиентского расчёта не сходятся.")
    if abs(hours - Decimal(str(row["hours"]))) > Decimal("0.000001") or covered != {
            instant for instant, _ in paid_minutes(row.get("time_facts"), row["date"])}:
        raise ValueError("Сохранённый расчёт не покрывает фактическое оплачиваемое время.")
    return parts


def save_terms(cursor, company, object_name, value, expected_revision):
    value = terms(value)
    revision = cursor.execute("SELECT COALESCE(MAX(revision),0) FROM customer_terms WHERE company=? AND object_name=?",
                              (company, object_name)).fetchone()[0]
    if revision != expected_revision:
        raise ValueError("Условия объекта изменились; обновите карточку.")
    item = {"id": uuid.uuid4().hex, "company": company, "object_name": object_name,
            "revision": revision + 1, "conditions_json": encoded(value),
            "created_at": datetime.now(timezone.utc).isoformat()}
    cursor.execute("INSERT INTO customer_terms VALUES(:id,:company,:object_name,:revision,:conditions_json,:created_at)", item)
    return item
