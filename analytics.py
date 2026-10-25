"""Read-only management projection using the report, payroll and expense models."""
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from customer_report import prepare_customer_costs
from expense_documents import allocation_breakdown, parse_decimal
from fixed_pricing import report_agreements
import payroll_history as payroll

ZONE = ZoneInfo("Europe/Stockholm")
WORK = {"work", "ob"}


def rounded(value):
    return float(Decimal(str(value)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def period(preset="month", today=None):
    today = today or datetime.now(ZONE).date()
    if preset in ("week", "previous_week"):
        start = today - timedelta(days=today.weekday())
        if preset == "previous_week":
            return start - timedelta(days=7), start - timedelta(days=1)
    elif preset in ("month", "previous_month"):
        start = today.replace(day=1)
        if preset == "previous_month":
            end = start - timedelta(days=1)
            return end.replace(day=1), end
    else:
        raise ValueError("Неизвестный период.")
    return start, today


def snapshot_segment_costs(snapshot):
    totals, costs = {}, {}
    previous = 0.0
    for line in snapshot["lines"]:
        for index, segment in enumerate(line["segments"]):
            key = line["sync_id"], index
            if segment["amount"] is None:
                costs[key] = None
                continue
            kind = line["kind"]
            totals[kind] = totals.get(kind, Decimal(0)) + Decimal(str(segment["amount"]))
            amounts = payroll.amounts([dict(kind=k, amount=value) for k, value in totals.items()],
                                      snapshot["parameters"])
            current = amounts["employer_total_cost"]
            costs[key] = rounded(current - previous)
            previous = current
    # Allocate stored rounding once in a stable order, independent of the selected dates/objects.
    if costs and all(value is not None for value in costs.values()):
        key = next(reversed(costs))
        costs[key] = rounded(costs[key] + snapshot["amounts"]["employer_total_cost"] - previous)
    return costs


HOW = {
    "hours": "Сумма внесённых часов work/OB по дате смены. OB — часть этих часов, не дополнительные часы. "
             "Транспорт, отсутствие, служебные и удалённые записи исключены.",
    "employees": "Разные имена сотрудников с рабочими записями за даты отбора, включая неактивных. "
                 "При неоднозначной связи со справочником имя не объединяется наугад.",
    "revenue": "Тот же расчёт, что в сводке заказчику: часы × сохранённая клиентская цена + OB-доплата "
               "+ клиентская плата за поездки. Fast pris — один раз, только при полном составе и периоде. "
               "Не счёт, не оплата. Историческая цена без подтверждения не подменяется сегодняшней.",
    "cost": "Зарплатные начисления по сохранённому расчёту (с заданными взносами); вне начислений — "
            "известная часть зарплаты, отдельно помеченная. Подтверждённые расходы по дате документа, "
            "в SEK без НДС при известных net или gross/VAT. Выплаты, баланс и оплаты не добавляются.",
    "result": "Известная расчётная стоимость работ минус известные затраты. При пропусках — только частичная "
              "разность, не прибыль и не движение денег. Общие расходы не распределяются по объектам.",
}


def build(cursor, start, end, documents, company="", object_key="", now=None):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("Начало периода позже окончания.")
    now = now or datetime.now(ZONE)
    objects = [dict(r) for r in cursor.execute("SELECT * FROM meta_objects ORDER BY company,name,id")]
    profiles = [dict(r) for r in cursor.execute("SELECT * FROM meta_employees")]
    by_name = defaultdict(list)
    for obj in objects:
        by_name[(obj["company"], obj["name"])].append(obj)
    groups = {}
    options = {}
    notes = set()

    def identity(customer, name):
        candidates = by_name[(customer or "", name or "")]
        if len(candidates) == 1:
            obj = candidates[0]
            key = "object:" + str(obj["id"])
            label = f"{customer} / {obj['markning'] + ' · ' if obj.get('markning') else ''}{name}"
        else:
            import json
            key = "unlinked:" + json.dumps([customer or "", name or ""], ensure_ascii=False)
            label = f"{customer or 'Заказчик не указан'} / {name or 'Без объекта'}"
            label += " · связь неоднозначна" if candidates else " · связь не установлена"
        options[key] = dict(key=key, company=customer or "", label=label)
        return key, label

    for obj in objects:
        identity(obj["company"], obj["name"])

    def group(customer, name):
        key, label = identity(customer, name)
        return groups.setdefault(key, dict(key=key, label=label, company=customer or "",
                                          hours=0.0, revenue=0.0, cost=0.0,
                                          wages=0.0, expenses=0.0, known_hours=0.0,
                                          unknown_records=0, cost_missing=False, revenue_missing=False,
                                          notes=[], fixed=[], has_expense=False))

    def selected(item):
        return (not company or item["company"] == company) and (not object_key or item["key"] == object_key)

    all_rows = [dict(r) for r in cursor.execute(
        "SELECT * FROM incoming_submissions WHERE is_deleted=0 AND date>=? AND date<=? ORDER BY date,sync_id",
        (start, end))]
    segments = {}
    for row in all_rows:
        row["entry_type"] = row["submission_type"]
        segments[row["sync_id"]] = [dict(s) for s in cursor.execute(
            "SELECT * FROM ob_segments WHERE shift_id=? ORDER BY segment_date,start_time,id", (row["sync_id"],))]
    work_by_group = defaultdict(list)
    daily, employees = defaultdict(float), set()
    row_groups = {}
    for row in all_rows:
        item = group(row["company"], row["object_name"])
        row_groups[row["sync_id"]] = item
        if row["entry_type"] in WORK:
            item["hours"] += float(row["hours"])
            work_by_group[item["key"]].append(row)
            if selected(item):
                daily[row["date"]] += float(row["hours"])
                employees.add(row["employee"])
    for key, rows in work_by_group.items():
        item = groups[key]
        invalid_ob = any(row["entry_type"] == "ob" and (
            not segments[row["sync_id"]] or abs(sum(float(s["hours"]) for s in segments[row["sync_id"]])
                                               - float(row["hours"])) > .000001) for row in rows)
        report = dict(journal_rows=rows, journal_segments=segments, warnings=[],
                      totals={"without_ob_hours": None if invalid_ob else 0.0})
        try:
            agreements, allocations = report_agreements(report)
            prepare_customer_costs(report)
            for agreement in agreements.values():
                attributable = (start <= agreement["date_from"] and agreement["date_to"] <= end
                                and report["fixed_complete"])
                item["fixed"].append(dict(amount=float(agreement["amount"]), start=agreement["date_from"],
                                          end=agreement["date_to"], included=attributable))
                if not attributable:
                    for line in report["cost_lines"]:
                        if line["label"].startswith("Fast pris") and (
                                f"{agreement['date_from']} – {agreement['date_to']}" in line["label"]):
                            line["amount"] = None
                    item["notes"].append("Fast pris вне полного периода/состава: сумма показана отдельно, не распределена.")
            item["revenue"] = rounded(sum(line["amount"] for line in report["cost_lines"] if line["amount"] is not None))
            item["revenue_missing"] = any(line["amount"] is None for line in report["cost_lines"])
            item["customer_lines"] = report["cost_lines"]
            for row in rows:
                single = dict(journal_rows=[row], journal_segments=segments, warnings=[],
                              totals={"without_ob_hours": None if invalid_ob and row["entry_type"] == "ob" else 0.0})
                if row["sync_id"] in allocations:
                    complete = not item["revenue_missing"]
                else:
                    prepare_customer_costs(single)
                    complete = single["totals"]["invoice_total"] is not None and not any(
                        a["date_from"] <= row["date"] <= a["date_to"] for a in agreements.values())
                if complete:
                    item["known_hours"] += float(row["hours"])
                else:
                    item["unknown_records"] += 1
        except (ValueError, TypeError, KeyError) as error:
            item["revenue_missing"] = True
            item["known_hours"] = 0.0
            item["unknown_records"] = len(rows)
            item["notes"].append(f"Клиентская стоимость требует проверки: {error}")

    history = payroll.events(cursor)
    rates, zones, snapshots = payroll.state(history)
    covered = set()
    payroll_snapshots = 0
    estimated_rows = 0
    # Closed/confirmed snapshots are authoritative accruals; payment-status never adds a cost.
    for (employee, left, right), snapshot in snapshots.items():
        if left > end or start > right:
            continue
        snapshot_selected = False
        costs = snapshot_segment_costs(snapshot)
        for line in snapshot["lines"]:
            source = line["source"]
            item = group(source["company"], source["object_name"])
            for segment in line["segments"]:
                day = segment["date"]
                covered.add((line["sync_id"], day))
            subset = [index for index, s in enumerate(line["segments"]) if start <= s["date"] <= end]
            if not subset:
                continue
            snapshot_selected = snapshot_selected or selected(item)
            current_row = cursor.execute(
                "SELECT * FROM incoming_submissions WHERE sync_id=? AND is_deleted=0", (line["sync_id"],)).fetchone()
            current_segments = [dict(s) for s in cursor.execute(
                "SELECT * FROM ob_segments WHERE shift_id=? ORDER BY segment_date,start_time,id", (line["sync_id"],))]
            if current_row is None or payroll.signature(dict(current_row), current_segments) != source:
                item["notes"].append("Смены отличаются от сохранённого начисления; стоимость начисления сохранена без пересчёта.")
                item["cost_missing"] = True
            values = [costs[(line["sync_id"], index)] for index in subset]
            if any(value is None for value in values):
                item["cost_missing"] = True
                item["notes"].append("Сохранённое начисление неполно.")
            item["wages"] += rounded(sum(value for value in values if value is not None))
            item["notes"].append("Сохранённое зарплатное начисление, включая заданные взносы; "
                                 "копейки округления отнесены в сохранённом порядке.")
        payroll_snapshots += int(snapshot_selected)
    previous = (first-timedelta(days=1)).isoformat()
    payroll_rows = [dict(r) for r in cursor.execute(
        "SELECT * FROM incoming_submissions WHERE is_deleted=0 AND date=? ORDER BY sync_id", (previous,))]
    for row in payroll_rows:
        row["entry_type"] = row["submission_type"]
        segments[row["sync_id"]] = [dict(s) for s in cursor.execute(
            "SELECT * FROM ob_segments WHERE shift_id=? ORDER BY segment_date,start_time,id", (row["sync_id"],))]
    for row in [*payroll_rows, *all_rows]:
        line = payroll.calculate_line(row, segments[row["sync_id"]], rates, zones, start, end)
        if line is None:
            continue
        remaining = [s for s in line["segments"] if (row["sync_id"], s["date"]) not in covered]
        if not remaining:
            continue
        item = group(row["company"], row["object_name"])
        item["cost_missing"] = True
        if line["kind"] not in WORK:
            item["notes"].append("Нерабочие категории: нет сохранённого начисления с правилами оплаты.")
            continue
        if line["amount"] is None:
            matches = [p for p in profiles if p["name"] == row["employee"]]
            if len(matches) == 1 and float(matches[0].get("salary_rate") or 0) > 0 and row["entry_type"] == "work":
                overrides = {f"{row['sync_id']}:{s['date']}": matches[0]["salary_rate"] for s in remaining
                             if s["source"] == "unknown"}
                if overrides:
                    line = payroll.calculate_line(row, segments[row["sync_id"]], rates, zones, start, end,
                                                  overrides=overrides)
                    remaining = [s for s in line["segments"] if (row["sync_id"], s["date"]) not in covered]
                    estimated_rows += int(selected(item))
                    item["notes"].append("Оценка зарплаты по текущей ставке; историческое начисление отсутствует.")
        if line["amount"] is not None:
            item["wages"] += rounded(sum(s["amount"] for s in remaining))
            item["notes"].append("Известная часть зарплаты без неподтверждённых отпускных, взносов и прочих правил.")
        else:
            item["notes"].append("Зарплатная стоимость не определена: ставка/OB/зона требуют подтверждения.")
    general = dict(key="general", label="Общие и нераспределённые расходы", company="", hours=0.0,
                   revenue=0.0, cost=0.0, wages=0.0, expenses=0.0, known_hours=0.0,
                   unknown_records=0, cost_missing=False, revenue_missing=False, notes=[], fixed=[],
                   has_expense=False)
    groups["general"] = general
    unknown_expenses, expense_count, undated = [], 0, 0
    for document in documents:
        day = document["document_date"]
        if not day:
            undated += 1
            continue
        if not start <= day <= end:
            continue
        allocations = document["allocations"]
        relevant = (not company and not object_key) or any(
            selected(group(a.get("company"), a.get("object_name")))
            for a in allocations if a.get("object_id") is not None and
            not a.get("company_expense") and a.get("destination") != "company")
        if not relevant:
            continue
        if document["verification_status"] != "confirmed":
            unknown_expenses.append(dict(id=document["id"], reason="Расход не подтверждён",
                                         amount=document["gross_amount"], currency=document["currency_code"]))
            continue
        net = parse_decimal(document["net_amount"])
        gross, vat = parse_decimal(document["gross_amount"]), parse_decimal(document["vat_amount"])
        if net is None and gross is not None and vat is not None:
            net = gross - vat
        if document["currency_code"] != "SEK" or net is None:
            unknown_expenses.append(dict(id=document["id"], reason="Валюта не SEK или сумма без НДС неизвестна",
                                         amount=document["gross_amount"], currency=document["currency_code"]))
            continue
        shares = []
        if document.get("allocation_basis") == "net":
            shares = [(a, parse_decimal(a["net_amount"])) for a in allocations]
        elif allocations and gross is not None:
            breakdown = allocation_breakdown(gross, net, vat, allocations)
            shares = [(a, parse_decimal(a.get("net_amount")) if a.get("net_amount") is not None
                       else b["net_amount"]) for a, b in zip(allocations, breakdown["allocations"])]
        if any(value is None for _, value in shares):
            unknown_expenses.append(dict(id=document["id"], reason="Не определено распределение без НДС",
                                         amount=document["gross_amount"], currency="SEK"))
            continue
        assigned = sum((value for _, value in shares), Decimal(0))
        if (net >= 0 and not Decimal(0) <= assigned <= net) or (net < 0 and not net <= assigned <= 0):
            unknown_expenses.append(dict(id=document["id"], reason="Распределение расхода расходится с документом",
                                         amount=document["gross_amount"], currency="SEK"))
            continue
        expense_count += 1
        general["expenses"] += float(net - assigned)
        general["has_expense"] |= not shares or net != assigned
        for allocation, value in shares:
            is_object = (allocation.get("object_id") is not None and not allocation.get("company_expense")
                         and allocation.get("destination") != "company")
            target = group(allocation.get("company"), allocation.get("object_name")) if is_object else general
            target["expenses"] += float(value)
            target["has_expense"] = True
    if undated:
        notes.add(f"Документов без даты: {undated}; к периоду не отнесены.")
    if company or object_key:
        notes.add("Общие и нераспределённые расходы компании не включены в результат выбранного заказчика/объекта.")
    notes.add("Собственный транспорт, страховки и не внесённые расходы не оценивались. "
              "Клиентская плата за поездку не считается затратой компании.")
    notes.add("Часы и клиентская стоимость — по дате смены; зарплата — по датам её сохранённых сегментов/начислений; "
              "расходы — по дате документа, не по дате оплаты.")
    notes.add("Локальные старые расходы и движения баланса не прибавляются к серверным документам: "
              "это не подтверждённые отдельные затраты без НДС.")
    if not expense_count:
        notes.add("Подтверждённых расходов в отборе нет; это не доказательство отсутствия затрат.")
    if not payroll_snapshots:
        notes.add("Сохранённых зарплатных начислений за период нет. Результат не отражает полную экономику.")
    if unknown_expenses:
        notes.add(f"Вне денежного итога: {len(unknown_expenses)} непроверенных/неприводимых расходов.")
    rows = []
    for item in groups.values():
        if not selected(item) or (item["key"] == "general" and (company or object_key)):
            continue
        if not (item["hours"] or item["revenue"] or item["wages"] or item["expenses"] or
                item["cost_missing"] or item["has_expense"]):
            continue
        item["revenue"], item["wages"], item["expenses"] = map(rounded, (item["revenue"], item["wages"], item["expenses"]))
        item["cost"] = rounded(item["wages"] + item["expenses"])
        item["result"] = rounded(item["revenue"] - item["cost"])
        item["partial"] = item["cost_missing"] or item["revenue_missing"] or bool(unknown_expenses) or bool(undated)
        item["notes"] = sorted(set(item["notes"]))
        rows.append(item)
    rows.sort(key=lambda r: (r["key"] == "general", r["label"]))
    totals = {key: rounded(sum(row[key] for row in rows))
              for key in ("hours", "known_hours", "revenue", "cost", "wages", "expenses", "result")}
    totals["employees"] = len(employees)
    totals["unknown_records"] = sum(row["unknown_records"] for row in rows)
    totals["partial"] = any(row["partial"] for row in rows) or bool(unknown_expenses or undated)
    totals["revenue_partial"] = any(row["revenue_missing"] for row in rows)
    totals["cost_partial"] = any(row["cost_missing"] for row in rows) or bool(unknown_expenses or undated)
    totals["result_partial"] = totals["revenue_partial"] or totals["cost_partial"]
    totals["has_data"] = bool(rows or unknown_expenses)
    totals["payroll_snapshots"] = payroll_snapshots
    totals["expense_documents"] = expense_count
    totals["estimated_wage_records"] = estimated_rows
    for name in employees:
        matches = [p for p in profiles if p["name"] == name]
        if len(matches) != 1:
            notes.add(f"Сотрудник «{name}»: связь со справочником не установлена однозначно; имя сохранено отдельно.")
    daily_points = []
    current = first
    weekly = (last - first).days > 62
    while current <= last:
        boundary = min(last, current + timedelta(days=6-current.weekday())) if weekly else current
        value = sum(hours for day, hours in daily.items() if current.isoformat() <= day <= boundary.isoformat())
        daily_points.append(dict(label=current.isoformat(), end=boundary.isoformat(), hours=rounded(value)))
        current = boundary + timedelta(days=1)
    chart = sorted(rows, key=lambda row: abs(row["revenue"]) + abs(row["cost"]), reverse=True)
    chart_rows = [dict(label=r["label"], revenue=r["revenue"], cost=r["cost"]) for r in chart[:8]]
    if len(chart) > 8:
        chart_rows.append(dict(label="Остальные", revenue=rounded(sum(r["revenue"] for r in chart[8:])),
                               cost=rounded(sum(r["cost"] for r in chart[8:]))))
    if object_key and object_key not in options:
        raise ValueError("Выбранный объект больше не доступен. Обновите отбор.")
    return dict(start=start, end=end, company=company, object_key=object_key,
                scope=options[object_key]["label"] if object_key else company or "Все объекты",
                preliminary=last >= now.date(), calculated_at=now.isoformat(timespec="seconds"),
                source="Серверный снимок; локальные неотправленные изменения не включены",
                totals=totals, rows=rows, hours_chart=daily_points, objects_chart=chart_rows,
                weekly=weekly, notes=sorted(notes), unknown_expenses=unknown_expenses,
                objects=sorted(options.values(), key=lambda obj: obj["label"]),
                companies=sorted({obj["company"] for obj in options.values() if obj["company"]}),
                how=HOW)
