"""Versioned payroll evidence, independent of customer prices and payments."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ob_calculator import calculate_work_interval, load_ob_rules


RULES_VERSION = "payroll-1"
WRITES_ENABLED = False
PARAMETERS = ("vacation", "tax", "employer_fee", "atf", "sick")


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def number(value, maximum=100000):
    if isinstance(value, bool):
        raise ValueError("Ожидается число, не логическое значение.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("Некорректное число.") from error
    if not result.is_finite() or not 0 <= result <= maximum:
        raise ValueError(f"Число должно быть от 0 до {maximum}.")
    return float(result)


def money(value):
    return float(Decimal(str(value)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def ensure_schema(cursor):
    # No backfill: an empty ledger is not evidence for any historic salary.
    cursor.execute("""CREATE TABLE IF NOT EXISTS payroll_events (
        seq INTEGER PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
        request_json TEXT NOT NULL, event_json TEXT NOT NULL,
        actor TEXT NOT NULL, created_at TEXT NOT NULL)""")
    for operation in ("UPDATE", "DELETE"):
        cursor.execute(f"""CREATE TRIGGER IF NOT EXISTS payroll_events_no_{operation.lower()}
            BEFORE {operation} ON payroll_events
            BEGIN SELECT RAISE(ABORT, 'Payroll history is append-only'); END""")


def events(cursor):
    if not cursor.execute("SELECT 1 FROM sqlite_master WHERE name='payroll_events'").fetchone():
        return []
    return [dict(row) for row in cursor.execute("SELECT * FROM payroll_events ORDER BY seq")]


def replace_cache(cursor, incoming):
    existing = events(cursor)
    if not isinstance(incoming, list) or incoming[:len(existing)] != existing:
        raise ValueError("История зарплаты расходится с сервером; замена запрещена.")
    for row in incoming[len(existing):]:
        if row["seq"] != len(existing) + 1:
            raise ValueError("Пропуск версии зарплатной истории.")
        json.loads(row["event_json"])
        cursor.execute("INSERT INTO payroll_events VALUES(?,?,?,?,?,?)", tuple(
            row[key] for key in ("seq", "operation_id", "request_json", "event_json", "actor", "created_at")
        ))
        existing.append(row)


def state(history):
    rates, zones, periods = {}, {}, {}
    for row in history:
        event = json.loads(row["event_json"])
        kind = event["kind"]
        if kind == "rate":
            rates.setdefault(event["employee"], {})[event["effective_from"]] = event
        elif kind == "zone":
            zones[(event["company"], event["object_name"])] = event
        elif kind in ("confirm", "close", "adjust"):
            value = event["snapshot"]
            periods[(value["employee"], value["date_from"], value["date_to"])] = value
        elif kind == "payment-status":
            key = (event["employee"], event["date_from"], event["date_to"])
            periods[key] = dict(periods[key], paid=event["paid"])
    return rates, zones, periods


def dated_rate(rates, employee, day):
    available = [r for start, r in rates.get(employee, {}).items() if start <= day]
    return max(available, key=lambda r: r["effective_from"]) if available else None


def entry_interval(cursor, employee, company, object_name, day, start, end, fallback_rate):
    """Use declared object time and dated rates for new/explicitly edited OB only."""
    rates, zones, _ = state(events(cursor))
    zone = zones.get((company, object_name))
    if rates.get(employee) and zone is None:
        raise ValueError("Для новой OB-смены с датированной ставкой подтвердите часовую зону объекта.")
    config = load_ob_rules()
    if zone:
        config = dict(config, timezone=zone["timezone"], ambiguous_time_policy=zone["ambiguous_policy"])
    result = calculate_work_interval(day, start, end, fallback_rate, config)
    if rates.get(employee):
        for segment in result["segments"]:
            rate = dated_rate(rates, employee, segment["date"])
            if rate is None:
                raise ValueError(f"{segment['date']}: зарплатная ставка не подтверждена.")
            segment["base_rate"] = rate["rate"]
            segment["calculated_rate"] = rate["rate"] * segment["multiplier"]
            segment["amount"] = money(Decimal(str(segment["hours"])) * Decimal(str(rate["rate"])) *
                                      Decimal(str(segment["multiplier"])))
        result["total_amount"] = money(sum(Decimal(str(s["amount"])) for s in result["segments"]))
    return result


def rate_known(cursor, employee, day, fallback):
    rates, _, _ = state(events(cursor))
    return fallback > 0 or dated_rate(rates, employee, day) is not None


def source_rows(cursor, table, employee, start, end):
    if table not in ("shifts", "incoming_submissions"):
        raise ValueError("Некорректный источник зарплаты.")
    deleted = " AND is_deleted=0" if table == "incoming_submissions" else ""
    previous = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
    rows = [dict(row) for row in cursor.execute(
        f"SELECT * FROM {table} WHERE employee=? AND date>=? AND date<=?{deleted} ORDER BY date,sync_id",
        (employee, previous, end),
    )]
    has_segments = cursor.execute("SELECT 1 FROM sqlite_master WHERE name='ob_segments'").fetchone()
    result = []
    for row in rows:
        segments = [dict(s) for s in cursor.execute(
            "SELECT * FROM ob_segments WHERE shift_id=? ORDER BY segment_date,start_time,id", (row["sync_id"],)
        )] if has_segments else []
        result.append((row, segments))
    return result


def signature(row, saved):
    return {
        **{key: row.get(key) for key in ("sync_id", "employee", "date", "company", "object_name",
                                       "hours", "start_time", "end_time")},
        "kind": row.get("entry_type", row.get("submission_type")),
        "segments": [{key: s.get(key) for key in (
            "segment_date", "start_time", "end_time", "hours", "category",
            "premium_percent", "multiplier", "base_rate", "amount",
        )} for s in saved],
    }


def calculate_line(row, saved, rates, zones, start, end, overrides=None, reprice=False):
    source = signature(row, saved)
    kind = source["kind"]
    if kind not in ("work", "ob", "atf", "sick", "vacation", "holiday"):
        return None
    zone = zones.get((row["company"], row["object_name"]))
    zone_name = zone["timezone"] if zone else None
    segments, warnings = [], []
    hours = number(row["hours"], 48)
    timed = bool(row.get("start_time") and row.get("end_time"))
    intervals = []
    if kind == "ob" and saved and not reprice:
        if abs(sum(number(s["hours"], 48) for s in saved) - hours) > .000001:
            warnings.append("Сохранённые OB-сегменты не покрывают часы смены.")
        intervals = [dict(s, date=s["segment_date"], evidence=True) for s in saved]
    elif timed:
        if zone_name is None:
            warnings.append("Часовая зона объекта не подтверждена.")
        else:
            config = dict(load_ob_rules(), timezone=zone_name, ambiguous_time_policy=zone["ambiguous_policy"])
            try:
                calculated = calculate_work_interval(row["date"], row["start_time"], row["end_time"], 0, config)
            except ValueError as error:
                warnings.append(f"Требует подтверждения: {error}")
            else:
                if abs(calculated["total_hours"] - hours) > .000001:
                    warnings.append("Часы смены расходятся с интервалом в подтверждённой зоне.")
                intervals = calculated["segments"]
    elif kind == "ob":
        warnings.append("Нет сохранённых OB-сегментов и достоверного интервала.")
    else:
        intervals = [{"date": row["date"], "hours": hours, "multiplier": 1, "category": kind}]
    if not intervals and (start <= row["date"] <= end or (
            timed and row["date"] < start and row["end_time"] <= row["start_time"])):
        intervals = [{"date": max(row["date"], start), "hours": hours, "multiplier": 1, "category": kind}]
    for interval in intervals:
        day = interval["date"]
        if not start <= day <= end:
            continue
        applied = dated_rate(rates, row["employee"], day)
        evidence = bool(interval.get("evidence"))
        override_key = f"{row['sync_id']}:{day}"
        explicit = (overrides or {}).get(override_key)
        if explicit is not None and (applied or evidence) and not reprice:
            raise ValueError("Индивидуальная известная ставка защищена; нужна явная корректировка.")
        preserved = evidence and not reprice
        if preserved:
            rate = number(interval["base_rate"])
            multiplier = number(interval["multiplier"], 100)
            amount = number(interval["amount"], 10000000)
            if rate == 0 and applied is None and explicit is None:
                warnings.append("Нулевая старая OB-ставка не подтверждена историческими данными.")
            if abs(money(Decimal(str(interval["hours"])) * Decimal(str(rate)) *
                         Decimal(str(multiplier))) - amount) > .01:
                warnings.append("Сохранённая сумма OB расходится с параметрами.")
            # A deliberately dated rate applies to new payroll, not to legacy source data.
            if applied and applied["effective_from"] <= day:
                preserved = False
                if zone_name is None:
                    warnings.append("Для применения датированной ставки к OB подтвердите зону объекта.")
        if not preserved:
            rate = number(explicit) if explicit is not None else applied["rate"] if applied else None
            multiplier = number(interval["multiplier"], 100) if kind == "ob" else 1.0
            amount = None if rate is None else money(
                Decimal(str(interval["hours"])) * Decimal(str(rate)) * Decimal(str(multiplier))
            )
        if kind in ("vacation", "holiday"):
            amount = 0.0
        if amount is None:
            warnings.append(f"{day}: Требует подтверждения — историческая ставка неизвестна.")
        segments.append({
            "date": day, "hours": float(interval["hours"]), "rate": rate,
            "multiplier": multiplier, "amount": amount, "category": interval["category"],
            "premium_percent": interval.get("premium_percent", 0),
            "source": "saved-ob" if preserved else "explicit-period" if explicit is not None
            else "dated-rate" if applied else "unknown",
        })
    if not segments:
        return None
    return {
        "sync_id": row["sync_id"], "kind": kind, "source": source, "timezone": zone_name,
        "ambiguous_policy": zone["ambiguous_policy"] if zone else None,
        "segments": segments, "hours": sum(s["hours"] for s in segments), "warnings": sorted(set(warnings)),
        "amount": None if warnings or any(s["amount"] is None for s in segments)
        else money(sum(Decimal(str(s["amount"])) for s in segments)),
    }


def amounts(lines, parameters):
    if any(line["amount"] is None for line in lines):
        return None
    totals = {}
    for line in lines:
        totals[line["kind"]] = totals.get(line["kind"], Decimal(0)) + Decimal(str(line["amount"]))
    base = totals.get("work", Decimal(0)) + totals.get("ob", Decimal(0))
    vacation = Decimal(str(money(base * Decimal(str(parameters["vacation"])) / 100)))
    atf = Decimal(str(money(totals.get("atf", Decimal(0)) * Decimal(str(parameters["atf"])) / 100)))
    sick = Decimal(str(money(totals.get("sick", Decimal(0)) * Decimal(str(parameters["sick"])) / 100)))
    gross = base + vacation + atf + sick
    tax = Decimal(str(money(gross * Decimal(str(parameters["tax"])) / 100)))
    fee = Decimal(str(money(gross * Decimal(str(parameters["employer_fee"])) / 100)))
    return dict(base_salary=money(base), vacation_pay=float(vacation), atf_pay=float(atf),
                sick_pay=float(sick), taxable_gross=money(gross), tax=float(tax),
                net_pay=money(gross-tax), employer_fee=float(fee), employer_total_cost=money(gross+fee))


def preview(cursor, table, history, employee, start, end, parameters, overrides=None, reprice=False):
    date.fromisoformat(start)
    date.fromisoformat(end)
    if start > end:
        raise ValueError("Некорректные границы периода.")
    parameters = {key: number(parameters[key], 100) for key in PARAMETERS}
    rates, zones, periods = state(history)
    saved = periods.get((employee, start, end))
    lines = []
    prior = {line["sync_id"]: line for line in saved["lines"]} if saved else {}
    for row, segments in source_rows(cursor, table, employee, start, end):
        line = calculate_line(row, segments, rates, zones, start, end, overrides, reprice)
        if line is None:
            continue
        old = prior.get(line["sync_id"])
        if old and not reprice and old["source"] == line["source"]:
            line = old
        lines.append(line)
    overlap = any(left <= end and start <= right and (left, right) != (start, end)
                  for name, left, right in periods if name == employee)
    result = dict(
        employee=employee, date_from=start, date_to=end, parameters=parameters,
        rules_version=RULES_VERSION, ob_rules_hash=digest(load_ob_rules()),
        rounding="HALF_UP_0.01_per_segment", ob_rules=load_ob_rules(),
        lines=lines, amounts=amounts(lines, parameters), status="draft",
        paid=saved["paid"] if saved else False, overlap=overlap,
        source_hash=digest([line["source"] for line in lines]),
    )
    result["preview_hash"] = digest(result)
    if saved:
        result["previous"] = saved
        result["source_changed"] = saved["source_hash"] != result["source_hash"]
    return result


def apply_action(cursor, table, request, actor, writes_enabled=None):
    if not (WRITES_ENABLED if writes_enabled is None else writes_enabled):
        raise ValueError("Совместимая версия восстановления: зарплатная история только для чтения.")
    operation = request.get("operation_id")
    if not isinstance(operation, str) or not 1 <= len(operation) <= 120:
        raise ValueError("Требуется стабильный идентификатор операции.")
    encoded = encode(request)
    old = cursor.execute("SELECT * FROM payroll_events WHERE operation_id=?", (operation,)).fetchone()
    if old:
        if old["request_json"] != encoded:
            raise ValueError("Идентификатор уже использован с другими данными.")
        return dict(old)
    history = events(cursor)
    if request.get("expected_seq") != len(history):
        raise ValueError("История изменилась: обновите просмотр.")
    kind = request.get("kind")
    employee = str(request.get("employee") or "").strip()
    now = datetime.now(timezone.utc).isoformat()
    if kind == "zone":
        zone_name = str(request["timezone"])
        try:
            ZoneInfo(zone_name)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("Неизвестная часовая зона IANA.") from error
        policy = request.get("ambiguous_policy", "reject")
        if policy not in ("reject", "earlier", "later"):
            raise ValueError("Некорректная политика перехода времени.")
        event = dict(kind=kind, company=request["company"], object_name=request["object_name"],
                     timezone=zone_name, ambiguous_policy=policy)
    elif kind == "rate":
        date.fromisoformat(request["effective_from"])
        event = dict(kind=kind, employee=employee, effective_from=request["effective_from"],
                     rate=number(request["rate"]))
    elif kind in ("confirm", "close", "adjust"):
        if request.get("reprice") and kind != "adjust":
            raise ValueError("Переоценка возможна только явной корректировкой.")
        draft = preview(cursor, table, history, employee, request["date_from"], request["date_to"],
                        request["parameters"], request.get("overrides"), bool(request.get("reprice")))
        if draft["preview_hash"] != request.get("preview_hash"):
            raise ValueError("Расчёт изменился после просмотра. Подтверждение отменено.")
        if not draft["lines"] or draft["amounts"] is None or draft["overlap"]:
            raise ValueError("Пустой, пересекающийся или неподтверждённый период нельзя закрыть.")
        previous = draft.pop("previous", None)
        same = previous and all(previous[k] == draft[k] for k in ("source_hash", "parameters", "amounts"))
        if previous and previous["status"] == "closed" and kind != "adjust" and not same:
            raise ValueError("Закрытый расчёт изменяется только явной корректировкой.")
        if kind == "adjust" and (not previous or not str(request.get("reason") or "").strip()):
            raise ValueError("Для корректировки нужны прежняя версия и причина.")
        if same and (kind != "close" or previous["status"] == "closed"):
            return {"noop": True}
        snapshot = {k: v for k, v in draft.items() if k not in ("preview_hash", "overlap", "source_changed")}
        snapshot.update(status="closed" if kind in ("close", "adjust") else "confirmed",
                        revision=previous["revision"]+1 if previous else 1, confirmed_at=now)
        if previous and previous["paid"] and previous["amounts"] != snapshot["amounts"]:
            snapshot.update(paid=False, previously_paid_revision=previous["revision"])
        event = dict(kind=kind, snapshot=snapshot, reason=str(request.get("reason") or "").strip())
        if previous:
            event["previous_revision"] = previous["revision"]
            event["difference"] = {key: money(Decimal(str(value))-Decimal(str(previous["amounts"][key])))
                                   for key, value in snapshot["amounts"].items()}
    elif kind == "payment-status":
        _, _, periods = state(history)
        key = (employee, request["date_from"], request["date_to"])
        if key not in periods or not isinstance(request.get("paid"), bool) or not str(request.get("reason") or "").strip():
            raise ValueError("Для сверки выплаты нужны сохранённый расчёт, отметка и комментарий.")
        if periods[key]["paid"] == request["paid"]:
            return {"noop": True}
        event = {key: request[key] for key in ("kind", "employee", "date_from", "date_to", "paid", "reason")}
    else:
        raise ValueError("Неизвестная зарплатная операция.")
    row = dict(seq=len(history)+1, operation_id=operation, request_json=encoded, event_json=encode(event),
               actor=actor, created_at=now)
    cursor.execute("INSERT INTO payroll_events VALUES(?,?,?,?,?,?)", tuple(row.values()))
    return row


def customer_payroll(cursor, company, object_names, start, end):
    """Read frozen line outputs, never infer salary from a customer price."""
    history = events(cursor)
    rates, zones, periods = state(history)
    employees = {r[0] for r in cursor.execute(
        "SELECT DISTINCT employee FROM shifts WHERE date>=? AND date<=?",
        ((date.fromisoformat(start)-timedelta(days=1)).isoformat(), end))}
    employees.update(name for name, left, right in periods if left <= end and start <= right)
    result = {}
    for employee in sorted(employees):
        frozen = [p for (name, left, right), p in periods.items()
                  if name == employee and left <= end and start <= right]
        lines = [line for p in frozen for line in p["lines"]]
        excluded = []
        for row, segments in source_rows(cursor, "shifts", employee, start, end):
            if any(p["date_from"] <= row["date"] <= p["date_to"] for p in frozen):
                excluded.append(row["sync_id"])
                continue
            line = calculate_line(row, segments, rates, zones, start, end)
            if line:
                # A night shift may enter a frozen period from the preceding day.
                line = dict(line, segments=[s for s in line["segments"] if not any(
                    p["date_from"] <= s["date"] <= p["date_to"] for p in frozen)])
                lines.append(line)
        item = dict(base_amount=Decimal(0), ob_premium=Decimal(0), rates=set(), warnings=[])
        relevant = False
        for line in lines:
            source = line["source"]
            if (line["kind"] not in ("work", "ob") or source["company"] != company
                    or source["object_name"] not in object_names):
                continue
            for segment in line["segments"]:
                if not start <= segment["date"] <= end:
                    continue
                relevant = True
                if segment["rate"] is None or line["warnings"]:
                    item["base_amount"] = item["ob_premium"] = None
                    item["warnings"].extend(line["warnings"] or ["Требует подтверждения исторической ставки."])
                    continue
                base = Decimal(str(segment["hours"])) * Decimal(str(segment["rate"]))
                if item["base_amount"] is not None:
                    item["base_amount"] += base
                    item["ob_premium"] += Decimal(str(segment["amount"]))-base
                item["rates"].add(f"{segment['rate']:.2f} SEK/h ({segment['source']})")
        if not relevant:
            continue
        if frozen and excluded:
            item["warnings"].append("Показаны сохранённые зарплатные расчёты; изменения смен требуют отдельной корректировки.")
        for key in ("base_amount", "ob_premium"):
            item[key] = None if item[key] is None else money(item[key])
        item["total"] = None if item["base_amount"] is None else money(item["base_amount"]+item["ob_premium"])
        item["rates"] = sorted(item["rates"])
        item["warnings"] = sorted(set(item["warnings"]))
        result[employee] = item
    return result
