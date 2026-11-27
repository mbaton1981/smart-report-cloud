"""Read-only project history for numbered invoices and older invoiced shifts."""
from collections import defaultdict
from contextlib import closing
from decimal import Decimal

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QLabel, QMessageBox, QPushButton, QSplitter, QTableWidgetItem, QVBoxLayout,
)

from analytics_ui import _run_correction
from desktop_ui import ReportTable
from expense_documents import parse_decimal


def money(value, currency):
    return f"{value:,.2f} {currency}".replace(",", " ") if value is not None else "Не сохранена"


def describe(batch):
    summary = batch.get("summary") or {}
    details, totals = [], defaultdict(Decimal)
    hours = Decimal(0)
    complete_expenses = True
    has_work = False
    for row in batch["rows"]:
        if row["kind"] == "shift":
            shift = row["signature"]
            hours += Decimal(str(shift["hours"]))
            kind = "Смена OB" if shift.get("entry_type") == "ob" else "Смена"
            trips = shift.get("transport") or 0
            detail = f"{shift['hours']:g} ч"
            if trips:
                detail += f" · поездок по смене: {trips:g}"
            details.append((shift["date"], kind, shift["employee"], detail, "", ""))
            has_work = True
        else:
            saved = summary.get("expense_details", {}).get(row["key"], {})
            if saved.get("fingerprint") != row.get("fingerprint"):
                saved = {}
            quote = row.get("material_pricing") or {}
            currency = row.get("currency") or ""
            net = parse_decimal(quote.get("base", row.get("net_amount")))
            if net is None:
                net = parse_decimal(saved.get("net"))
            customer = parse_decimal(quote.get("customer_net")) if quote else net
            if customer is None or currency in ("", "UNKNOWN"):
                complete_expenses = False
            else:
                totals[currency] += customer
            markup = f"{quote['percent']}% · {money(parse_decimal(quote['markup']), currency)}" if quote else ""
            label = saved.get("label") or f"{row.get('supplier') or 'Документ'} · №{row.get('number') or row['document_id']}"
            details.append((saved.get("date") or row.get("date") or "", f"Расход · доля {row['index'] + 1}",
                            label, money(net, currency), markup, money(customer, currency)))
    details.sort()
    days = [item[0] for item in details if item[0]]
    period = " — ".join(dict.fromkeys((days[0], days[-1]))) if days else "Не указан"
    if summary.get("start") and summary.get("end"):
        period = f"{summary['start']} — {summary['end']}"
    total = parse_decimal(summary.get("total_sek"))
    if batch.get("corrected"):
        total = None
    amount = money(total, "SEK")
    if total is None and not has_work and complete_expenses and totals:
        amount = " + ".join(money(value, code) for code, value in sorted(totals.items()))
    for label, count, unit, line_amount in summary.get("work_lines", []):
        details.append(("", "Расчёт счёта", label, f"{count} {unit}" if count is not None else "",
                        "", money(parse_decimal(line_amount), "SEK")))
    return period, amount, hours, details


def load_history(desktop, local_id):
    with closing(desktop.get_db_connection()) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        obj = conn.execute("SELECT * FROM objects WHERE id=?", (local_id,)).fetchone()
        if obj is None:
            raise ValueError("Выбранный объект исчез из справочника.")
        names = desktop.invoice_object_names(conn.cursor(), obj)
        placeholders = ",".join("?" for _ in names)
        rows = [dict(row) for row in conn.execute(f"""
            SELECT id,date,employee,entry_type,hours,transport,sync_id,object_name,sync_version,
                   EXISTS(SELECT 1 FROM shift_sync_outbox q WHERE q.sync_id=shifts.sync_id
                          AND q.state='pending' AND q.operation<>'delete') AS pending
            FROM shifts
            WHERE invoiced=1 AND entry_type IN ('work','ob') AND company=? AND object_name IN ({placeholders})
            ORDER BY date,employee,id""", (obj["company"], *names))]
    batches, cloud_rows = [], None
    if obj["server_id"] is not None:
        state = desktop.request_expense_api("GET", "/api/customer-invoices",
                                            params={"object_id": obj["server_id"], "view": "issued_history"})
        if "history" not in state and state.get("batches") != []:
            raise ValueError("Для просмотра сохранённых счетов требуется обновление сервера SmartReport.")
        batches = state.get("history", [])
        cloud_rows = state.get("legacy")
    linked = {member["sync_id"] for batch in batches for member in batch["rows"] if member["kind"] == "shift"}
    legacy = defaultdict(list)
    by_id = {row["sync_id"]: row for row in cloud_rows or []}
    for row in rows:
        if cloud_rows is None or not row["sync_version"] or row["pending"] or row["object_name"] != obj["name"]:
            by_id.setdefault(row["sync_id"] or ("local", row["id"]), row)
    for row in by_id.values():
        if row["sync_id"] not in linked:
            legacy[row["date"][:7]].append(dict(kind="shift", signature=row))
    return obj["name"], batches + [
        dict(number="", date="", month=month, rows=legacy[month]) for month in sorted(legacy, reverse=True)]


def open_history(parent):
    import app as desktop
    local_id = parent.inv_obj_cb.currentData()
    if local_id is None:
        QMessageBox.information(parent, "История выставленных счетов", "Выберите объект.")
        return
    try:
        name, batches = _run_correction(parent, "Загружаем историю выставленных счетов…",
                                        lambda: load_history(desktop, local_id))
    except Exception as error:
        QMessageBox.warning(parent, "Не удалось открыть историю", str(error))
        return
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"История выставленных счетов · {name}")
    dialog.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
    dialog.resize(1080, 680)
    layout = QVBoxLayout(dialog)
    if batches:
        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter)
        def table(headers, widths, stretch):
            widget = ReportTable(0, len(headers), dialog, column_widths=widths, stretch_columns=stretch)
            widget.setHorizontalHeaderLabels(headers)
            widget.setEditTriggers(ReportTable.EditTrigger.NoEditTriggers)
            widget.setSelectionBehavior(ReportTable.SelectionBehavior.SelectRows)
            widget.setSelectionMode(ReportTable.SelectionMode.SingleSelection)
            splitter.addWidget(widget)
            return widget
        invoices = table(("Счёт", "Дата счёта", "Период позиций", "Часы", "Сумма без НДС"),
                         {0: 280, 1: 125, 2: 238, 3: 95, 4: 190}, (0,))
        lines = table(("Дата", "Позиция", "Сотрудник / документ", "Часы / закупка без НДС",
                       "Наценка", "Клиенту без НДС"), {0: 110, 1: 135, 2: 250, 3: 190, 4: 160, 5: 145}, (2,))
        note = QLabel()
        note.setWordWrap(True)
        layout.addWidget(note)
        def fill(widget, data):
            widget.setRowCount(len(data))
            for index, values in enumerate(data):
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setToolTip(value)
                    widget.setItem(index, col, item)
        descriptions = [describe(batch) for batch in batches]
        fill(invoices, [(f"№ {batch['number']}" if batch["number"] else "Выставлено без номера счёта",
                         batch["date"] or "—", period, f"{hours:g}", amount)
                        for batch, (period, amount, hours, _) in zip(batches, descriptions)])
        def choose(index, *_):
            if index < 0:
                return
            batch = batches[index]
            fill(lines, descriptions[index][3])
            if not batch["number"]:
                note.setText("Прежние отметки по сменам сгруппированы по месяцу работы. Номер и дата счёта не сохранены.")
            elif batch.get("corrected"):
                note.setText("Показан состав после исправления расходов. Первоначальная сумма могла измениться.")
            elif descriptions[index][1] == "Не сохранена":
                note.setText("Для этого состава полная сумма не сохранена. Доступные суммы указаны в строках.")
            else:
                note.clear()
        invoices.currentCellChanged.connect(choose)
        invoices.setCurrentCell(0, 0)
        splitter.setSizes([220, 370])
    else:
        layout.addWidget(QLabel("По этому объекту пока нет сохранённых счетов или смен с отметкой «выставлено»."))
    close = QPushButton("Закрыть")
    close.clicked.connect(dialog.accept)
    layout.addWidget(close)
    dialog.exec()
