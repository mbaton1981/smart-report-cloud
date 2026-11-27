"""Read-only project history for numbered invoices and older invoiced shifts."""
from collections import defaultdict
from contextlib import closing
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import sqlite3
import tempfile

from pypdf import PdfReader
import accountant_mail as mail
from object_records import MAX_FILE_BYTES
from payroll_preview import open_preview

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QMenu, QMessageBox, QPushButton, QTableWidgetItem, QVBoxLayout,
)

from analytics_ui import _run_correction
from desktop_ui import ReportTable
from expense_documents import parse_decimal
from expense_categories import CATEGORY_MATERIALS, EXPENSE_CATEGORIES


def money(value, currency):
    return f"{value:,.2f} {currency}".replace(",", " ").replace(".", ",")


def invoice_amounts(batch):
    summary = batch.get("summary") or {}
    days = []
    groups = defaultdict(lambda: defaultdict(Decimal))
    missing = set()
    has_shifts = False
    hours = Decimal(0)
    for row in batch["rows"]:
        if row["kind"] == "shift":
            has_shifts = True
            days.append(row["signature"]["date"])
            hours += Decimal(str(row["signature"]["hours"]))
            continue
        saved = summary.get("expense_details", {}).get(row["key"], {})
        if saved.get("fingerprint") != row.get("fingerprint"):
            saved = {}
        days.append(saved.get("date") or row.get("date") or "")
        quote = row.get("material_pricing") or {}
        category = CATEGORY_MATERIALS if quote else saved.get("category")
        if category not in EXPENSE_CATEGORIES:
            category = "Расходы без сохранённой категории"
        code = row.get("currency")
        base = parse_decimal(quote.get("base", row.get("net_amount")))
        if base is None:
            base = parse_decimal(saved.get("net"))
        if base is None or code in (None, "", "UNKNOWN"):
            missing.add(category)
        else:
            groups[category][code] += base
        if quote:
            extra = parse_decimal(quote.get("markup"))
            if extra is None or code in (None, "", "UNKNOWN"):
                missing.add("Наценка")
            else:
                groups["Наценка"][code] += extra
    work_lines = summary.get("work_lines", [])
    if has_shifts and not work_lines:
        missing.add("Работы и поездки")
    for label, _, _, line_amount in work_lines:
        amount = parse_decimal(line_amount)
        group = "Поездки" if label.startswith(("Resor", "Resa")) else "Работы"
        if amount is None:
            missing.add(group)
        else:
            groups[group]["SEK"] += amount
    days = sorted(day for day in days if day)
    period = " — ".join(dict.fromkeys((days[0], days[-1]))) if days else "—"
    if summary.get("start") and summary.get("end"):
        period = f"{summary['start']} — {summary['end']}"
    totals = defaultdict(Decimal)
    parts = []
    for label in ("Работы", "Работы и поездки", "Поездки", *EXPENSE_CATEGORIES, "Расходы без сохранённой категории", "Наценка"):
        amounts = groups.get(label)
        if amounts:
            values = " + ".join(money(value, code) for code, value in sorted(amounts.items()))
            parts.append(f"{label} · {values}" + (" (известная часть)" if label in missing else ""))
            for code, value in amounts.items():
                totals[code] += value
        elif label in missing:
            parts.append(f"{label} · сумма не сохранена")
    saved_total = parse_decimal(summary.get("total_sek"))
    if saved_total is not None:
        total = money(saved_total, "SEK")
    elif not batch.get("corrected") and not missing and totals:
        total = " + ".join(money(value, code) for code, value in sorted(totals.items()))
    else:
        total = "Сумма не сохранена"
    if batch.get("corrected"):
        parts.append("Состав расходов показан с учётом исправлений.")
    return period, total, format(hours.normalize(), "f").replace(".", ",") + " ч", "   ·   ".join(parts)

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
    return obj["name"], obj["server_id"], batches + [
        dict(number="", date="", month=month, rows=legacy[month]) for month in sorted(legacy, reverse=True)]


def open_history(parent):
    import app as desktop
    local_id = parent.inv_obj_cb.currentData()
    if local_id is None:
        QMessageBox.information(parent, "История выставленных счетов", "Выберите объект.")
        return
    try:
        name, project, batches = _run_correction(parent, "Загружаем историю выставленных счетов…",
                                                lambda: load_history(desktop, local_id))
    except Exception as error:
        QMessageBox.warning(parent, "Не удалось открыть историю", str(error))
        return
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"История выставленных счетов · {name}")
    dialog.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
    dialog.resize(900, min(620, 235 + 42 * len(batches)))
    layout = QVBoxLayout(dialog)
    if batches:
        heading = QHBoxLayout()
        status = QLabel("✓ Выставлено")
        status.setStyleSheet("background:#176343;color:white;border-radius:5px;padding:4px 9px;")
        heading.addWidget(status)
        title = QLabel()
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setStyleSheet("font-size:16pt;font-weight:bold")
        heading.addWidget(title)
        heading.addStretch()
        layout.addLayout(heading)
        headline = QLabel()
        headline.setStyleSheet("font-size:13pt;font-weight:bold")
        headline.setWordWrap(True)
        layout.addWidget(headline)
        amounts = QLabel()
        amounts.setWordWrap(True)
        layout.addWidget(amounts)
        period_label = QLabel()
        layout.addWidget(period_label)
        table = ReportTable(len(batches), 5, dialog, stretch_columns=(0,), column_widths={0: 150, 1: 115, 2: 215, 3: 85, 4: 170})
        table.setHorizontalHeaderLabels(("Счёт", "Дата счёта", "Период позиций", "Часы", "Итого без НДС"))
        table.setEditTriggers(ReportTable.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(ReportTable.SelectionBehavior.SelectRows)
        table.setSelectionMode(ReportTable.SelectionMode.SingleSelection)
        table.verticalHeader().hide()
        layout.addWidget(table)
        table.setVisible(len(batches) > 1)
        summaries = []
        for index, batch in enumerate(batches):
            period, total, hours, parts = invoice_amounts(batch)
            summaries.append((period, total, hours, parts))
            for col, value in enumerate((f"№ {batch['number']}" if batch['number'] else "Выставлено без номера счёта",
                                         batch['date'] or "—", period, hours, total)):
                table.setItem(index, col, QTableWidgetItem(value))
        source = QLabel()
        source.setTextFormat(Qt.TextFormat.PlainText)
        source.setWordWrap(True)
        layout.addWidget(source)
        bar = QHBoxLayout()
        layout.addLayout(bar)
        view = QPushButton("Открыть PDF")
        attach = QPushButton("Прикрепить отчёт…")
        menu = QMenu(attach)
        from_mail = menu.addAction("Из отправленных бухгалтеру…")
        from_file = menu.addAction("Выбрать PDF на компьютере…")
        attach.setMenu(menu)
        bar.addWidget(view)
        bar.addWidget(attach)
        bar.addStretch()

        def refresh(*_):
            index = table.currentRow()
            if index < 0:
                return
            batch = batches[index]
            period, total, hours, parts = summaries[index]
            title.setText(f"Счёт № {batch['number']} от {batch['date']}" if batch['number'] else "Без номера счёта")
            headline.setText(f"{hours}     ·     Итого без НДС — {total}")
            amounts.setText(parts)
            amounts.setVisible(bool(parts))
            period_label.setText(f"Период позиций   {period}")
            attach.setVisible(bool(batch['number']))
            info = None
            if batch['number']:
                try:
                    with mail.archive() as db:
                        info = mail.invoice_attachment(db, project, batch['number'], batch['summary'].get("sent_pdf_ref"))
                except OSError as error:
                    source.setText(str(error))
                    view.hide()
                    return
                except sqlite3.Error as error:
                    source.setText(f"Архив PDF недоступен: {error}")
                    view.hide()
                    return
            view.setVisible(info is not None)
            attach.setText("Заменить отчёт…" if info else "Прикрепить отчёт…")
            if info and info["created"]:
                source.setText(f"Отправлено {info['created'][:16].replace('T', ' ')} · {info['recipient']}")
            elif info:
                source.setText(info["filename"])
            else:
                source.setText("PDF не прикреплён." if batch['number'] else "Старые отметки сгруппированы по месяцу работы.")

        def handle(action):
            index = table.currentRow()
            if index < 0 or not batches[index]["number"]:
                return
            batch = batches[index]
            number = batch["number"]
            try:
                if action == "view":
                    with mail.archive() as db:
                        item = mail.invoice_attachment(db, project, number, batch["summary"].get("sent_pdf_ref"), content=True)
                    if item:
                        with tempfile.TemporaryDirectory(prefix="smartreport-invoice-pdf-") as folder:
                            path = Path(folder) / Path(item["filename"]).name
                            path.write_bytes(item["pdf"])
                            open_preview(path, Path(desktop.REPORTS_DIR) / path.name, dialog, title=f"Отчёт к счёту № {number}")
                elif action == "mail":
                    with mail.archive() as db:
                        sent = db.execute("SELECT id,created,recipient,period FROM mail WHERE project=? AND status='sent' ORDER BY id DESC", (project,)).fetchall()
                    if not sent:
                        QMessageBox.information(dialog, "Отправленные отчёты", "В этом профиле пока нет отправленных через SmartReport отчётов по объекту. Можно выбрать PDF на компьютере.")
                        return
                    choices = [f"{r['created'][:16].replace('T', ' ')} · {r['period']} · {r['recipient']} · #{r['id']}" for r in sent]
                    choice, ok = QInputDialog.getItem(dialog, f"Отчёт к счёту № {number}", "Отправленный PDF", choices, 0, False)
                    if ok:
                        with mail.archive() as db:
                            db.execute("INSERT OR REPLACE INTO invoice_pdf(project,number,mail_id) VALUES (?,?,?)",
                                       (project, number, sent[choices.index(choice)]["id"]))
                else:
                    filename, _filter = QFileDialog.getOpenFileName(dialog, f"Отчёт к счёту № {number}", "", "PDF (*.pdf)")
                    if filename:
                        path = Path(filename)
                        if path.stat().st_size > MAX_FILE_BYTES:
                            raise ValueError("Размер PDF превышает 100 МБ.")
                        data = path.read_bytes()
                        if not PdfReader(BytesIO(data)).pages:
                            raise ValueError("В PDF нет страниц.")
                        with mail.archive() as db:
                            db.execute("INSERT OR REPLACE INTO invoice_pdf(project,number,filename,pdf) VALUES (?,?,?,?)",
                                       (project, number, path.name, data))
            except Exception as error:
                QMessageBox.warning(dialog, "Отчёт к счёту", str(error))
            refresh()

        table.currentCellChanged.connect(refresh)
        view.clicked.connect(lambda: handle("view"))
        from_mail.triggered.connect(lambda: handle("mail"))
        from_file.triggered.connect(lambda: handle("file"))
        table.setCurrentCell(0, 0)
    else:
        layout.addWidget(QLabel("По этому объекту пока нет выставленных счетов."))
    close = QPushButton("Закрыть")
    close.clicked.connect(dialog.accept)
    if batches:
        bar.addWidget(close)
    else:
        layout.addWidget(close)
    dialog.exec()
