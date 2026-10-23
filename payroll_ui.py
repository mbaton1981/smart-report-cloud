"""Desktop views of explicit payroll evidence; never posts to Balance."""
import json
import logging
from datetime import date, timedelta
from pathlib import Path
import sqlite3
import uuid
from xml.sax.saxutils import escape

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidgetItem, QTextEdit, QVBoxLayout,
)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, TableStyle
from reportlab.platypus.doctemplate import LayoutError

import payroll_history as history

logger = logging.getLogger(__name__)


def enabled(cursor):
    return cursor.execute("SELECT 1 FROM sqlite_master WHERE name='payroll_events'").fetchone() is not None


def parameters(window):
    return {key: getattr(window, f"sal_{key}_percent").value() for key in history.PARAMETERS}


def number(value):
    return "Требует подтверждения" if value is None else f"{value:,.2f}"


def cache(app, incoming):
    conn = app.get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        history.replace_cache(conn.cursor(), incoming)
        conn.commit()
    finally:
        conn.close()


def reports(window, app):
    conn = app.get_db_connection()
    try:
        cursor = conn.cursor()
        entries = history.events(cursor)
        start, end = window.sal_date_from.text(), window.sal_date_to.text()
        employees = {r[0] for r in cursor.execute(
            "SELECT DISTINCT employee FROM shifts WHERE date>=? AND date<=?",
            ((date.fromisoformat(start)-timedelta(days=1)).isoformat(), end))}
        _, _, periods = history.state(entries)
        employees.update(name for name, left, right in periods if left <= end and start <= right)
        result = []
        for employee in sorted(employees):
            draft = history.preview(cursor, "shifts", entries, employee, start, end, parameters(window))
            if not draft["lines"] and not draft.get("previous") and not draft["overlap"]:
                continue
            report = dict(draft.get("previous", draft))
            report["source_changed"] = draft.get("source_changed", False)
            if draft["overlap"]:
                report.update(amounts=None, status="overlap")
            report["overtime_hours"] = max(0, sum(l["hours"] for l in report["lines"]
                                                   if l["kind"] in ("work", "ob")) -
                                           app.swedish_workday_norm(start, end))
            result.append(report)
        return result
    finally:
        conn.close()


def status(report):
    text = {
        "draft": "Предварительно; история не подтверждена",
        "confirmed": "Расчёт подтверждён",
        "closed": "Зарплатный период закрыт",
        "overlap": "Выберите точные границы сохранённого зарплатного периода",
    }[report["status"]]
    text += " · Выплата сверена" if report["paid"] else " · Выплата не подтверждена"
    if report.get("source_changed"):
        text += " · Смены изменились: показан прежний расчёт; нужна явная корректировка"
    return text


def render(window, app):
    items = reports(window, app)
    window.table_salary.blockSignals(True)
    try:
        window.table_salary.setRowCount(len(items))
        for index, report in enumerate(items):
            lines = report["lines"]
            hours = {kind: sum(line["hours"] for line in lines if line["kind"] == kind)
                     for kind in ("work", "ob", "atf", "sick", "vacation", "holiday")}
            rates = sorted({s["rate"] for line in lines for s in line["segments"] if s["rate"] is not None})
            rate_text = " / ".join(number(rate) for rate in rates)
            if not rate_text or any(s["rate"] is None for line in lines for s in line["segments"]):
                rate_text = (rate_text+" / " if rate_text else "")+"Требует подтверждения"
            values = [
                report["employee"], f"{hours['work']:g}", f"{hours['atf']:g}", f"{hours['sick']:g}",
                rate_text,
                f"{hours['ob']:g} tim", f"{max(0, hours['work']+hours['ob']-app.swedish_workday_norm(report['date_from'], report['date_to'])):g}",
                *[number(report["amounts"][key] if report["amounts"] else None) for key in (
                    "base_salary", "vacation_pay", "atf_pay", "sick_pay", "tax",
                    "net_pay", "employer_fee", "employer_total_cost")],
                f"{hours['vacation']:g}", f"{hours['holiday']:g}",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setToolTip(status(report))
                window.table_salary.setItem(index, column, item)
    finally:
        window.table_salary.blockSignals(False)
    window.sal_summary_hours_lbl.setText("Рабочие часы: " + f"{sum(l['hours'] for r in items for l in r['lines'] if l['kind']=='work'):.1f}")
    window.sal_summary_ob_lbl.setText("OB: " + f"{sum(l['hours'] for r in items for l in r['lines'] if l['kind']=='ob'):.1f} tim · база уже включена")
    window.sal_summary_vacation_lbl.setText("Отпуск: " + f"{sum(l['hours'] for r in items for l in r['lines'] if l['kind']=='vacation'):.1f} tim")
    for attribute, key, label in (("net", "net_pay", "Nettolön"), ("employer", "employer_total_cost", "Arbetskostnad")):
        total = None if any(r["amounts"] is None for r in items) else sum(r["amounts"][key] for r in items)
        getattr(window, f"sal_summary_{attribute}_lbl").setText(f"{label}: {number(total)}")
    window.payroll_history_note.setText(
        "Закрытие расчёта не означает выплату. «Баланс» автоматически не меняется.\n" +
        "\n".join(f"{r['employee']}: {status(r)}" for r in items))


def preview_text(draft):
    parts = [f"{draft['employee']} · {draft['date_from']} — {draft['date_to']}",
             f"Правила: {draft['rules_version']}. Предпросмотр — не выплата."]
    previous = draft.get("previous")
    if previous:
        parts.append(f"Сохранённая версия {previous['revision']}: {status(previous)}")
    parts.append("Параметры, %: " + " · ".join(
        f"{label}: {draft['parameters'][key]:g}" for key, label in (
            ("vacation", "Отпускные"), ("tax", "Налог"), ("employer_fee", "Взносы работодателя"),
            ("atf", "ATF"), ("sick", "Больничные"))))
    for line in draft["lines"]:
        parts.append(f"\n{line['sync_id']} · {line['kind']} · {line['hours']:g} ч · зона: {line['timezone'] or 'Не подтверждена'}")
        for s in line["segments"]:
            parts.append(f"  {s['date']}: {s['hours']:g} × {number(s['rate'])} × {s['multiplier']:g} = {number(s['amount'])} · {s['source']}")
        parts.extend(line["warnings"])
    parts.append("\nИТОГИ:")
    for key in ("base_salary", "vacation_pay", "atf_pay", "sick_pay", "taxable_gross",
                "tax", "net_pay", "employer_fee", "employer_total_cost"):
        value = draft["amounts"][key] if draft["amounts"] else None
        text = f"{key}: {number(value)}"
        if previous and value is not None:
            text += f" · было {number(previous['amounts'][key])} · разница {value-previous['amounts'][key]:+.2f}"
        parts.append(text)
    if draft["overlap"]:
        parts.append("Период пересекает сохранённый расчёт. Выберите его точные границы.")
    return "\n".join(parts)


def ensure_pending(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS payroll_pending(operation_id TEXT PRIMARY KEY,request_json TEXT NOT NULL)")


def open_history(window, app):
    dialog = QDialog(window)
    dialog.setWindowTitle("Зарплата: ставки, подтверждение и корректировки")
    dialog.resize(1000, 820)
    layout = QVBoxLayout(dialog)
    form = QFormLayout()
    employee, objects = QComboBox(), QComboBox()
    employee.setObjectName("payroll_employee")
    objects.setObjectName("payroll_object")
    conn = app.get_db_connection()
    try:
        employee.addItems([r[0] for r in conn.execute("SELECT name FROM employees ORDER BY name")])
        for row in conn.execute("SELECT company,name FROM objects ORDER BY company,name"):
            objects.addItem(f"{row[0]} | {row[1]}", (row[0], row[1]))
        ensure_pending(conn)
        conn.commit()
    finally:
        conn.close()
    start, end = app.create_date_field(window.sal_date_from.text()), app.create_date_field(window.sal_date_to.text())
    effective = app.create_date_field(window.sal_date_from.text())
    rate = QDoubleSpinBox()
    rate.setRange(-1, 100000)
    rate.setDecimals(2)
    rate.setSpecialValueText("Не задана")
    rate.setValue(-1)
    rate.setObjectName("payroll_rate")
    start.setObjectName("payroll_start")
    end.setObjectName("payroll_end")
    effective.setObjectName("payroll_effective")
    zone = QLineEdit()
    zone.setPlaceholderText("Подтверждённая зона IANA, например Europe/Stockholm")
    policy = QComboBox()
    for label, value in (("Неоднозначное время: запросить уточнение", "reject"),
                         ("Первое вхождение", "earlier"), ("Второе вхождение", "later")):
        policy.addItem(label, value)
    proposal = QCheckBox("Предложить ставку только для неизвестных строк периода")
    reprice = QCheckBox("Корректировка: явно переоценить ранее подтверждённый расчёт")
    proposal.setObjectName("payroll_proposal")
    reprice.setObjectName("payroll_reprice")
    reason = QLineEdit()
    reason.setObjectName("payroll_reason")
    for label, widget in (("Сотрудник", employee), ("Период с", start), ("По", end),
                          ("Ставка действует с", effective), ("Зарплатная ставка SEK/ч", rate),
                          ("Исторические ставки", proposal), ("Переоценка", reprice), ("Причина", reason),
                          ("Объект", objects), ("Часовая зона", zone), ("Переход времени", policy)):
        form.addRow(label, widget)
    layout.addLayout(form)
    note = QLabel("Ставки зарплаты не связаны с клиентскими ценами. Изменение здесь не меняет ставку справочника.\n"
                  "Старые ставки подтверждаются только после просмотра. Закрытие и выплата — разные признаки.\n"
                  "«Баланс» не меняется. При потере связи сохранённый запрос можно повторить без дублирования.")
    note.setWordWrap(True)
    layout.addWidget(note)
    output = QTextEdit()
    output.setReadOnly(True)
    layout.addWidget(output)
    actions = QHBoxLayout()
    layout.addLayout(actions)
    pending = {}

    def payload():
        return dict(employee=employee.currentText(), date_from=start.text(), date_to=end.text(),
                    parameters=parameters(window), reprice=reprice.isChecked(), overrides={})

    def invalidate(*_):
        pending.clear()
        output.setPlainText("Параметры изменены; требуется новый предварительный просмотр.")

    for signal in (employee.currentTextChanged, start.dateChanged, end.dateChanged, rate.valueChanged,
                   proposal.toggled, reprice.toggled):
        signal.connect(invalidate)

    def preview():
        body = payload()
        remote = app.request_expense_api("POST", "/sync/payroll", json=dict(body, kind="preview"))
        if proposal.isChecked():
            if rate.value() < 0:
                raise ValueError("Укажите предлагаемую ставку.")
            body["overrides"] = {
                f"{line['sync_id']}:{s['date']}": rate.value()
                for line in remote["preview"]["lines"] for s in line["segments"]
                if s["rate"] is None or reprice.isChecked()
            }
            remote = app.request_expense_api("POST", "/sync/payroll", json=dict(body, kind="preview"))
        cache(app, remote["events"])
        conn = app.get_db_connection()
        try:
            local = history.preview(conn.cursor(), "shifts", remote["events"], body["employee"],
                                    body["date_from"], body["date_to"], body["parameters"],
                                    body["overrides"], body["reprice"])
        finally:
            conn.close()
        if local["preview_hash"] != remote["preview"]["preview_hash"]:
            raise ValueError("Локальные и серверные данные расходятся. Сначала выполните синхронизацию.")
        pending.update(body=body, draft=remote["preview"], entries=remote["events"])
        output.setPlainText(preview_text(remote["preview"]))

    def send_saved():
        conn = app.get_db_connection()
        try:
            row = conn.execute("SELECT * FROM payroll_pending").fetchone()
            if row is None:
                raise ValueError("Нет сохранённого запроса для повтора.")
            body = json.loads(row["request_json"])
        finally:
            conn.close()
        remote = app.request_expense_api("POST", "/sync/payroll", json=body)
        cache(app, remote["events"])
        conn = app.get_db_connection()
        try:
            conn.execute("DELETE FROM payroll_pending WHERE operation_id=?", (body["operation_id"],))
            conn.commit()
        finally:
            conn.close()
        pending.clear()
        output.setPlainText("Сохранено. Выплаты не создавались; «Баланс» не изменён.")
        window.load_salary_table_template()

    def perform(kind):
        if not history.WRITES_ENABLED:
            raise ValueError("Версия восстановления: зарплатные операции запрещены.")
        if kind in ("rate", "zone"):
            current = app.request_expense_api("GET", "/sync/payroll")
            body = dict(kind=kind, expected_seq=len(current["events"]))
            if kind == "rate":
                if rate.value() < 0:
                    raise ValueError("Укажите ставку.")
                body.update(employee=employee.currentText(), rate=rate.value(), effective_from=effective.text())
                description = f"{employee.currentText()}: {rate.value():.2f} SEK/ч с {effective.text()}.\nЗакрытые расчёты не изменятся."
            else:
                company, object_name = objects.currentData()
                body.update(company=company, object_name=object_name, timezone=zone.text().strip(),
                            ambiguous_policy=policy.currentData())
                description = f"Подтвердить зону {zone.text()} для {company} | {object_name}?\nИсходные интервалы и закрытые расчёты не меняются."
        else:
            if "draft" not in pending:
                raise ValueError("Сначала просмотрите расчёт.")
            body = dict(pending["body"], kind=kind, expected_seq=len(pending["entries"]),
                        preview_hash=pending["draft"]["preview_hash"], reason=reason.text().strip())
            description = preview_text(pending["draft"])
            if kind in ("adjust", "payment-status") and not body["reason"]:
                raise ValueError("Укажите причину.")
            if kind == "payment-status":
                previous = pending["draft"].get("previous")
                if previous is None:
                    raise ValueError("Нужен сохранённый расчёт.")
                body["paid"] = not previous["paid"]
                description = "Изменить отметку сверки ранее выполненной выплаты?\nНовая выплата и операции «Баланса» не создаются."
        if QMessageBox.question(dialog, "Явное подтверждение", description,
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                               QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        body["operation_id"] = str(uuid.uuid4())
        conn = app.get_db_connection()
        try:
            if conn.execute("SELECT 1 FROM payroll_pending").fetchone():
                raise ValueError("Есть неразрешённый запрос. Повторите его перед новой операцией.")
            conn.execute("INSERT INTO payroll_pending VALUES(?,?)", (body["operation_id"], history.encode(body)))
            conn.commit()
        finally:
            conn.close()
        send_saved()

    def clear_pending():
        remote = app.request_expense_api("GET", "/sync/payroll")
        cache(app, remote["events"])
        conn = app.get_db_connection()
        try:
            row = conn.execute("SELECT * FROM payroll_pending").fetchone()
            if row is None:
                raise ValueError("Нет сохранённого запроса.")
            already_applied = any(r["operation_id"] == row["operation_id"] for r in remote["events"])
            explanation = ("Сервер уже применил операцию; история обновлена." if already_applied else
                           "Сервер пока не подтвердил операцию. Проверяйте историю перед повторным вводом.")
            if QMessageBox.question(dialog, "Снять локальный запрос", explanation +
                                   "\nУдалить только локальный запрос? Это не отменяет серверную операцию.",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                conn.execute("DELETE FROM payroll_pending WHERE operation_id=?", (row["operation_id"],))
                conn.commit()
                pending.clear()
                window.load_salary_table_template()
                output.setPlainText(explanation)
        finally:
            conn.close()

    def audit():
        remote = app.request_expense_api("GET", "/sync/payroll")
        cache(app, remote["events"])
        parts = ["Журнал зарплаты. Прежние версии не удаляются; записи «Баланса» здесь не создаются."]
        for row in remote["events"]:
            event = json.loads(row["event_json"])
            snapshot = event.get("snapshot")
            if event.get("employee", snapshot["employee"] if snapshot else None) != employee.currentText():
                continue
            parts.append(f"\n#{row['seq']} · {row['created_at']} · {row['actor']} · {event['kind']}")
            if snapshot:
                parts.append(f"{snapshot['date_from']} — {snapshot['date_to']} · версия {snapshot['revision']} · "
                             f"начислено {number(snapshot['amounts']['taxable_gross'])} · "
                             f"к выплате {number(snapshot['amounts']['net_pay'])}")
            if event.get("kind") == "rate":
                parts.append(f"С {event['effective_from']}: {number(event['rate'])} SEK/ч")
            if "difference" in event:
                parts.append("Изменение к выплате: " + number(event["difference"]["net_pay"]))
            if event.get("reason"):
                parts.append("Причина: "+event["reason"])
        output.setPlainText("\n".join(parts))

    def safe(action):
        try:
            action()
        except (ValueError, KeyError, RuntimeError, app.sqlite3.Error, app.requests.RequestException) as error:
            QMessageBox.warning(dialog, "История зарплаты", str(error))

    for index, (label, action) in enumerate((
        ("Просмотр", preview), ("Ставка с даты", lambda: perform("rate")),
        ("Зона объекта", lambda: perform("zone")), ("Подтвердить", lambda: perform("confirm")),
        ("Закрыть зарплату", lambda: perform("close")), ("Корректировка", lambda: perform("adjust")),
        ("Сверка выплаты", lambda: perform("payment-status")), ("Повтор запроса", send_saved),
        ("Снять запрос", clear_pending), ("Журнал", audit),
    )):
        if index == 5:
            actions = QHBoxLayout()
            layout.addLayout(actions)
        button = QPushButton(label)
        button.clicked.connect(lambda checked=False, action=action: safe(action))
        actions.addWidget(button)
    dialog.exec()


def write_pdf(path, items, font, bold):
    style = ParagraphStyle("payroll", fontName=font, fontSize=9, leading=13)
    heading = ParagraphStyle("payroll-heading", parent=style, fontName=bold, fontSize=14, spaceAfter=10)
    story = []
    labels = dict(base_salary="Grundlön inklusive OB", vacation_pay="Semesterersättning", atf_pay="ATF/KLS",
                  sick_pay="Sjuklön", taxable_gross="Bruttolön", tax="Preliminär skatt",
                  net_pay="Nettolön", employer_fee="Arbetsgivaravgifter", employer_total_cost="Total arbetskostnad")
    for index, report in enumerate(items):
        if index:
            story.append(PageBreak())
        def p(text):
            return Paragraph(escape(str(text)), style)
        story.extend([Paragraph("Löneunderlag", heading), p(report["employee"]),
                      p(f"{report['date_from']} — {report['date_to']}"), p(status(report)),
                      p(f"Beräkningsversion: {report['rules_version']} · Revision: {report.get('revision', 'utkast')}"),
                      p("Löneberäkning är inte en utbetalning. Balans ändras inte."), Spacer(1, 12)])
        hours = {kind: sum(line["hours"] for line in report["lines"] if line["kind"] == kind)
                 for kind in ("work", "ob", "atf", "sick", "vacation", "holiday")}
        story.append(p(" · ".join(f"{label}: {hours[kind]:g} tim" for kind, label in (
            ("work", "Arbete"), ("ob", "OB"), ("atf", "ATF"), ("sick", "Sjukfrånvaro"),
            ("vacation", "Semestertimmar"), ("holiday", "Röda dagar")))))
        if "overtime_hours" in report:
            story.append(p(f"Сверх нормы, ч: {report['overtime_hours']:g}"))
        rows = [[p(v) for v in ("Datum / post", "Tim", "Timlön", "Faktor", "Belopp SEK")]]
        for line in report["lines"]:
            for s in line["segments"]:
                rows.append([p(f"{s['date']} · {line['kind']}"), p(f"{s['hours']:g}"), p(number(s["rate"])),
                             p(f"{s['multiplier']:g}"), p(number(s["amount"]))])
            for warning in line["warnings"]:
                rows.append([p(warning), "", "", "", ""])
        rows += [[p(label), "", "", "", p(number(report["amounts"][key] if report["amounts"] else None))]
                 for key, label in labels.items()]
        table = LongTable(rows, colWidths=[200, 40, 100, 45, 140], repeatRows=1)
        table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8edf2")),
                                   ("GRID", (0, 0), (-1, -1), .3, colors.lightgrey)]))
        story.append(table)
    # Landscape keeps explicit unknown values legible without converting them to zero.
    SimpleDocTemplate(path if hasattr(path, "write") else str(path), pagesize=(A4[1], A4[0]), leftMargin=30, rightMargin=30,
                      topMargin=25, bottomMargin=25).build(story)


def export_issues(items):
    issues = []
    for report in items:
        employee = report["employee"]
        if report["status"] == "overlap":
            issues.append(f"{employee}: выберите точные границы сохранённого зарплатного периода.")
        elif report["status"] == "draft":
            issues.append(f"{employee}: расчёт за {report['date_from']} — {report['date_to']} не подтверждён.")
        if report.get("source_changed"):
            issues.append(f"{employee}: смены изменились; показана сохранённая версия, нужна отдельная корректировка.")
        for line in report["lines"]:
            source = line["source"]
            prefix = f"{employee} · {source['date']} · {source['object_name']} · запись {line['sync_id']}"
            issues.extend(f"{prefix}: {warning}" for warning in line["warnings"])
    return list(dict.fromkeys(issues))


def export(window, app):
    path = None
    try:
        items = reports(window, app)
        if not items:
            QMessageBox.warning(window, "Löneunderlag", "Нет данных для выбранного периода.")
            return
        issues = export_issues(items)
        filename = f"Loneunderlag_{window.sal_date_from.text()}_till_{window.sal_date_to.text()}.pdf"
        suggested = (Path(app.REPORTS_DIR) / filename).resolve()
        title = "Сохранить предварительный расчёт зарплаты" if issues else "Сохранить расчёт зарплаты"
        selected, _ = QFileDialog.getSaveFileName(window, title, str(suggested), "PDF (*.pdf)")
        if not selected:
            logger.info("Salary PDF export cancelled for %s — %s",
                        window.sal_date_from.text(), window.sal_date_to.text())
            return
        path = Path(selected).resolve()
        if not path.suffix:
            path = path.with_suffix(".pdf")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_pdf(path, items, app.DEFAULT_FONT, app.DEFAULT_FONT_BOLD)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error, LayoutError) as error:
        logger.exception("Salary PDF export failed; destination=%s", path)
        QMessageBox.critical(window, "Ошибка экспорта зарплаты",
                             f"Не удалось сформировать PDF.\n{error}\n" +
                             (f"Файл: {path}\n" if path else "") +
                             "Выплату отмечать не нужно. «Баланс» не изменён.")
        return
    logger.info("Salary PDF saved: %s; preliminary=%s; employees=%d", path, bool(issues), len(items))
    message = f"PDF сохранён:\n{path}"
    if issues:
        message += ("\n\nЭто предварительный расчёт. Экспорт разрешён без подтверждения выплаты.\n"
                    "Необходимые подтверждения / уточнения:\n" + "\n".join(issues))
    message += "\n\n«Баланс» и отметки выплаты не изменены."
    QMessageBox.information(window, "Зарплата — PDF сохранён", message)
