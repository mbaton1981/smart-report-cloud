"""Desktop workflow for the next customer invoice and its immutable membership."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import uuid
import tempfile

from PyQt6.QtCore import QEventLoop, QThreadPool, Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QProgressDialog,
)

from customer_billing import prepare_next_invoice
import desktop_theme
from expense_documents import parse_decimal
from expense_categories import (
    CATEGORY_MATERIALS, CATEGORY_EQUIPMENT_RENTAL, CATEGORY_SUBCONTRACTOR_WORK,
    CATEGORY_SUBCONTRACTOR_MIXED, CATEGORY_OTHER, CATEGORY_UNCLASSIFIED, confirmed_expense_category,
)
from object_package_report import collect_object_expenses, expense_document_key, _recover_report_share_tax
from accountant_report import TIME_LABELS, line_details, summarize_accountant, write_accountant_report
import payroll_auto
import payroll_history
import payroll_ui


WORK_CATEGORY = "Наши часы и поездки"
CATEGORY_ORDER = (
    CATEGORY_MATERIALS, CATEGORY_EQUIPMENT_RENTAL, CATEGORY_SUBCONTRACTOR_WORK,
    CATEGORY_SUBCONTRACTOR_MIXED, CATEGORY_OTHER, CATEGORY_UNCLASSIFIED, WORK_CATEGORY,
)


def accountant_payroll(desktop, report, entries):
    selected = defaultdict(list)
    for entry in entries:
        if entry["member"]["kind"] == "shift":
            selected[entry["row"]["employee"]].append(entry["row"])
    if not selected:
        return {}
    settings = payroll_ui.read_auto_settings(desktop)
    conn = desktop.get_db_connection()
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        _, zones, _ = payroll_history.state(payroll_history.events(conn.cursor()))
        classified = {}
        for employee, rows in selected.items():
            start = min(row["date"] for row in rows)
            end = (date.fromisoformat(max(row["date"] for row in rows)) + timedelta(days=1)).isoformat()
            sources = {row["sync_id"]: (row, saved) for row, saved in
                       payroll_history.source_rows(conn.cursor(), "shifts", employee, start, end)}
            for row in rows:
                saved = report.get("journal_segments", {}).get(row["sync_id"], [])
                sources[row["sync_id"]] = (row, [{"segment_date": row["date"], **part} for part in saved])
            result = payroll_auto.classify(list(sources.values()), employee, zones,
                                           desktop.swedish_public_holidays, settings)
            classified.update((row["sync_id"], result[row["sync_id"]]) for row in rows)
        return classified
    finally:
        conn.close()


def edit_accountant_rates(parent, rates, use_saved_terms):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Тарифы для этого пакета")
    form = QFormLayout(dialog)
    note = QLabel(
        "Заполните только ставки, которые хотите заменить. Пустые поля используют сохранённую клиентскую "
        "ставку и проценты из зарплатной разбивки. Ставку для OB и Övertid вводите полностью, с доплатой. "
        "При изменении часовых ставок сохранённые особые условия отключаются для этого пакета."
    )
    note.setWordWrap(True)
    form.addRow(note)
    saved = QCheckBox("Применять сохранённые особые условия клиента")
    saved.setChecked(use_saved_terms)
    saved.setToolTip("Снимите отметку, чтобы применить тарифы пакета вместо сохранённых доплат. Fast pris сохраняется.")
    form.addRow(saved)
    fields = {}
    for key, label in [*TIME_LABELS.items(), ("trip", "Поездка")]:
        if key == "unknown":
            continue
        edit = QLineEdit()
        edit.setText(str(rates[key]) if key in rates else "")
        edit.setPlaceholderText("Автоматически")
        if key in payroll_auto.PREMIUM and payroll_auto.PREMIUM[key]:
            label += f" (+{payroll_auto.PREMIUM[key]:.0%})"
        form.addRow(label + (" · SEK/поездка" if key == "trip" else " · SEK/ч"), edit)
        if key != "trip":
            edit.textEdited.connect(lambda _: saved.setChecked(False))
        fields[key] = edit
    actions = QHBoxLayout()
    apply = QPushButton("Применить")
    close = QPushButton("Отмена")
    actions.addWidget(apply)
    actions.addWidget(close)
    form.addRow(actions)
    result = {}

    def accept():
        result.clear()
        for key, edit in fields.items():
            text = edit.text().strip()
            value = parse_decimal(text) if text else None
            if text and (value is None or not value.is_finite() or not 0 <= value <= 100000):
                QMessageBox.warning(dialog, "Тарифы", "Введите ставку от 0 до 100 000 SEK или оставьте поле пустым.")
                edit.setFocus()
                return
            if value is not None:
                result[key] = value
        dialog.accept()

    apply.clicked.connect(accept)
    close.clicked.connect(dialog.reject)
    return (result, saved.isChecked()) if dialog.exec() == QDialog.DialogCode.Accepted else None


def open_next_invoice(host):
    import app as desktop

    object_id = host.inv_obj_cb.currentData()
    if object_id is None:
        QMessageBox.information(host, "Следующий счёт", "Выберите объект.")
        return
    connection = desktop.get_db_connection()
    try:
        row = connection.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
        if row is None:
            raise ValueError("Объект исчез из справочника.")
        obj = dict(row)
    finally:
        connection.close()
    tab_cutoff = host.inv_date_to.date().toString("yyyy-MM-dd")
    today = desktop.stockholm_today().isoformat()
    month_start, month_end = desktop.stockholm_month_bounds()
    previous_start, previous_end = desktop.stockholm_month_bounds(-1)
    periods = [
        ("За весь проект", None, today),
        ("Этот месяц", month_start, month_end),
        ("Прошлый месяц", previous_start, previous_end),
        ("По дату отчёта", None, tab_cutoff),
    ]
    latest = max(end for _, _, end in periods)

    def fetch():
        report = host.collect_invoice_report(selection={
            "company": obj["company"], "object_id": obj["id"], "sync_id": obj["sync_id"],
            "date_from": "1900-01-01", "date_to": latest,
        })
        capabilities = desktop.request_expense_api("GET", "/api/expense-documents/capabilities")
        supported = "accountant_invoice_batches_v1" in capabilities.get("features", [])
        if obj.get("server_id") is None:
            raise ValueError("Сначала синхронизируйте объект с сервером.")
        state = (desktop.request_expense_api("GET", "/api/customer-invoices", params={"object_id": obj["server_id"]})
                 if "customer_invoice_batches_v1" in capabilities.get("features", []) else {"members": {}, "batches": []})
        documents = desktop.fetch_all_expense_documents(None, None)
        entries, warnings = prepare_next_invoice(report["journal_rows"], documents, obj["server_id"], latest,
                                                 state["members"], accountant=True)
        report["accountant_payroll"] = accountant_payroll(desktop, report, entries)
        needed = {entry["document"]["id"]: entry["document"] for entry in entries
                  if entry["member"]["kind"] == "expense" and entry.get("net") is None}
        download = getattr(host, "_download_object_report_originals", None)
        if needed and download:
            recovered, _excluded = collect_object_expenses(list(needed.values()), obj["server_id"])
            with tempfile.TemporaryDirectory(prefix="smartreport-accountant-originals-") as directory:
                paths, errors = download(list(needed.values()), directory, lambda _message: None)
                _recover_report_share_tax({"object": dict(obj, id=obj["server_id"]), "expenses": recovered}, paths, errors)
            by_part = {(doc["id"], index): allocation for doc in recovered
                       for index, allocation in zip(doc["object_allocation_indexes"], doc["object_allocations"])}
            for entry in entries:
                if entry["member"]["kind"] == "expense":
                    entry["display_allocation"] = by_part.get((entry["document"]["id"], entry["member"]["index"]), entry["allocation"])
        return report, entries, warnings, state, supported

    loop = QEventLoop()
    outcome = []
    task = desktop.ObjectOverviewTask(0, fetch)

    def ready(_request_id, result, error):
        outcome.append((result, error))
        loop.quit()

    task.signals.completed.connect(ready)
    progress = QProgressDialog("Готовим часы, поездки и расходы…", "", 0, 0, host)
    progress.setCancelButton(None)
    progress.setWindowModality(Qt.WindowModality.WindowModal)
    progress.show()
    QThreadPool.globalInstance().start(task)
    loop.exec()
    progress.close()
    result, error = outcome[0]
    if error:
        QMessageBox.warning(host, "Следующий счёт", error)
        return
    report, entries, warnings, state, supported = result
    groups = defaultdict(list)
    for entry in entries:
        if entry["member"]["kind"] == "shift":
            category = WORK_CATEGORY
        else:
            allocation = entry["allocation"]
            category, _ = confirmed_expense_category(
                allocation.get("expense_category"), allocation.get("expense_category_source"),
            )
        entry["category"] = category
        groups[category].append(entry)
    dialog = QDialog(host)
    dialog.setWindowTitle(f"Документы бухгалтеру к следующему счёту · {obj['name']}")
    dialog.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
    dialog.resize(1180, 760)
    layout = QVBoxLayout(dialog)
    period_bar = QHBoxLayout()
    period_buttons = QButtonGroup(dialog)
    for index, (label, start, end) in enumerate(periods):
        button = QPushButton(label)
        button.setCheckable(True)
        button.setToolTip(f"{start} — {end}" if start else f"Все невыставленные позиции по {end} включительно")
        desktop_theme.apply_style(button, "QPushButton:checked { background-color: #24445f; color: #dbeafe; }")
        period_buttons.addButton(button, index)
        period_bar.addWidget(button)
    period_buttons.button(0).setChecked(True)
    period_bar.addStretch()
    layout.addLayout(period_bar)
    note = QLabel()
    note.setObjectName("accountantInvoicePeriodNote")
    note.setWordWrap(True)
    layout.addWidget(note)
    if not supported:
        warnings.insert(0, "Пробный просмотр. Для сохранения состава требуется обновление сервера.")
    if not state["batches"]:
        warnings.insert(0, "Это первый состав. Сверьте расходы с прежними клиентскими счетами; история их выставления ещё не записана.")
    warnings_label = QLabel("\n".join(warnings))
    warnings_label.setWordWrap(True)
    layout.addWidget(warnings_label)
    if state["batches"]:
        history = QLabel("Сохранённые фактуры · " + " · ".join(
            f"№{item['invoice_number']} от {item['invoice_date']}" for item in state["batches"]
        ))
        history.setWordWrap(True)
        layout.addWidget(history)
    total_label = QLabel()
    total_label.setObjectName("nextInvoiceCategoryTotals")
    total_label.setWordWrap(True)
    font = total_label.font()
    font.setBold(True)
    total_label.setFont(font)
    layout.addWidget(total_label)
    options = QHBoxLayout()
    include_trips = QCheckBox("Включить поездки")
    include_trips.setChecked(True)
    include_journal = QCheckBox("Включить Dagbok")
    include_journal.setChecked(True)
    rate_button = QPushButton("Тарифы пакета…")
    rate_button.setToolTip("Клиентские ставки для PDF и выбор сохранённых особых условий.")
    for control in (include_trips, include_journal, rate_button):
        options.addWidget(control)
    jump = QPushButton("Перейти к разделу…")
    jump.setObjectName("accountantInvoiceGroupNavigation")
    sections = QMenu(jump)
    jump.setMenu(sections)
    options.addWidget(jump)
    options.addStretch()
    layout.addLayout(options)
    table = QTableWidget(len(entries) + len(groups), 7)
    table.setHorizontalHeaderLabels(["Включить", "Дата", "Категория", "Часы / OB / документ", "Сумма документа", "Состояние", "Копия PDF"])
    table.horizontalHeaderItem(6).setToolTip("Добавить оригинал поставщика в приложения к этому пакету.")
    table.setColumnWidth(0, 80)
    table.setColumnWidth(1, 115)
    table.setColumnWidth(2, 190)
    table.setColumnWidth(3, 285)
    table.setColumnWidth(4, 155)
    table.setColumnWidth(5, 175)
    table.setColumnWidth(6, 95)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.verticalHeader().hide()
    table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    table.verticalScrollBar().setSingleStep(24)
    empty = QLabel("Нет позиций к выставлению за выбранный период.")
    empty.setWordWrap(True)
    layout.addWidget(empty)
    layout.addWidget(table, 1)

    entry_rows = {}
    headings = {}
    next_row = 0
    heading_font = table.font()
    heading_font.setBold(True)
    for category in CATEGORY_ORDER:
        group = groups.get(category)
        if not group:
            continue
        heading = QTableWidgetItem(f"{category} · {len(group)}")
        heading.setFlags(Qt.ItemFlag.ItemIsEnabled)
        heading.setFont(heading_font)
        heading.setBackground(desktop_theme.color("#26394b"))
        heading.setForeground(desktop_theme.color("#dbeafe"))
        table.setSpan(next_row, 0, 1, table.columnCount())
        table.setItem(next_row, 0, heading)
        action = sections.addAction(category)
        action.triggered.connect(
            lambda _checked=False, item=heading: table.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtTop)
        )
        headings[category] = heading, action
        for index, entry in enumerate(group, start=next_row + 1):
            entry_rows[entry["member"]["key"]] = index
        next_row += len(group) + 1

    for entry in entries:
        index = entry_rows[entry["member"]["key"]]
        item = QTableWidgetItem()
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        shift = entry["member"]["kind"] == "shift"
        allocation = entry.get("display_allocation", entry.get("allocation", {}))
        available = shift or (parse_decimal(allocation.get("gross_amount")) is not None and entry["currency"] != "UNKNOWN")
        if not available:
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setCheckState(Qt.CheckState.Checked if available else Qt.CheckState.Unchecked)
        table.setItem(index, 0, item)
        description = entry["label"]
        if shift:
            row = entry["row"]
            description += f"\nПоездки · {row.get('transport') or 0:g}"
            description += "\n" + line_details(report, row)
        net = parse_decimal(allocation.get("net_amount"))
        gross = parse_decimal(allocation.get("gross_amount"))
        amount_text = "—" if shift else (
            f"{net:,.2f} {entry['currency']} без НДС" if net is not None else
            f"{gross:,.2f} {entry['currency']} с НДС" if gross is not None else "Сумма не указана")
        for column, text in enumerate((entry["date"], entry["category"], description,
                                       amount_text, "К передаче бухгалтеру" if available else "Нет суммы документа или валюты"), 1):
            table.setItem(index, column, QTableWidgetItem(text))
        copy = QTableWidgetItem()
        if not shift:
            copy.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            document = entry["document"]
            has_copy = document.get("has_original") or document.get("attachments") or document.get("local_attachment_paths")
            copy.setCheckState(Qt.CheckState.Checked if has_copy else Qt.CheckState.Unchecked)
        else:
            copy.setFlags(Qt.ItemFlag.NoItemFlags)
        table.setItem(index, 6, copy)
    table.resizeRowsToContents()
    form = QFormLayout()
    number = QLineEdit()
    number.setPlaceholderText("Номер фактуры, которую вы выставили клиенту")
    invoice_date = desktop.create_date_field(today)
    form.addRow("Номер клиентской фактуры", number)
    form.addRow("Дата фактуры", invoice_date)
    layout.addLayout(form)
    reviewed = QCheckBox("Сверил состав и прежние счета клиенту")
    reviewed.setObjectName("accountantInvoiceReviewed")
    layout.addWidget(reviewed)
    actions = QHBoxLayout()
    pdf_button = QPushButton("Предварительный PDF")
    save_button = QPushButton("Сохранить как выставленное")
    close = QPushButton("Закрыть")
    for button in (pdf_button, save_button, close):
        actions.addWidget(button)
    layout.addLayout(actions)
    close.clicked.connect(dialog.reject)
    operation_id = uuid.uuid4().hex
    committed = False
    submitted_payload = None
    rates = {}
    use_saved_terms = True
    visible_entries = []

    def selected():
        return [entry for entry in visible_entries
                if table.item(entry_rows[entry["member"]["key"]], 0).checkState() == Qt.CheckState.Checked]

    def refresh(*_):
        categories = {}
        selection = selected()
        summary = summarize_accountant(report, selection, include_trips=include_trips.isChecked(),
                                       rates=rates, use_saved_terms=use_saved_terms)
        for entry in selection:
            if entry["member"]["kind"] == "shift":
                continue
            allocation = entry.get("display_allocation", entry["allocation"])
            net = parse_decimal(allocation.get("net_amount"))
            amount = net if net is not None else parse_decimal(allocation.get("gross_amount"))
            if amount is not None:
                key = (entry["category"], entry["currency"], "без НДС" if net is not None else "с НДС")
                categories[key] = categories.get(key, Decimal(0)) + amount
        hours, trips, total = summary["hours"], summary["trips"], summary["total"]
        lines = [f"Часы · {hours:g} ч" + (f" · Поездки · {trips:g}" if include_trips.isChecked() else "")]
        lines.append(
            f"К выставлению · {total:,.2f} SEK без НДС" if total is not None else
            f"К выставлению · требуется сверка · известная часть {summary['known']:,.2f} SEK без НДС"
        )
        if summary["stored_terms"]:
            lines.append("В расчёте есть сохранённые особые условия клиента.")
        amounts = sorted(categories.items(), key=lambda item: (CATEGORY_ORDER.index(item[0][0]), item[0]))
        lines += [f"{category} · {value:,.2f} {currency} {basis}"
                  for (category, currency, basis), value in amounts]
        total_label.setText("\n".join(lines))
        valid = bool(selection)
        save_button.setEnabled(supported and valid and reviewed.isChecked()
                               and bool(number.text().strip()) and not committed)
        pdf_button.setEnabled(valid)

    table.itemChanged.connect(refresh)
    number.textChanged.connect(refresh)
    reviewed.toggled.connect(refresh)
    include_trips.toggled.connect(refresh)

    def apply_period(index):
        _label, start, end = periods[index]
        note.setText(
            f"Позиции к выставлению за {start} — {end}." if start else
            f"Все невыставленные позиции по {end} включительно."
        )
        counts = defaultdict(int)
        visible_entries.clear()
        signals_blocked = table.blockSignals(True)
        try:
            for entry in entries:
                day = entry["date"]
                visible = (start is None or day >= start) and day <= end
                table.setRowHidden(entry_rows[entry["member"]["key"]], not visible)
                if visible:
                    visible_entries.append(entry)
                    counts[entry["category"]] += 1
            for category, (heading, action) in headings.items():
                count = counts.get(category, 0)
                heading.setText(f"{category} · {count}")
                table.setRowHidden(heading.row(), not count)
                action.setVisible(bool(count))
        finally:
            table.blockSignals(signals_blocked)
        jump.setVisible(len(counts) > 1)
        empty.setVisible(not visible_entries)
        table.scrollToTop()
        if reviewed.isChecked():
            reviewed.setChecked(False)
        else:
            refresh()

    period_buttons.idToggled.connect(lambda index, checked: apply_period(index) if checked else None)

    def change_rates():
        nonlocal rates, use_saved_terms
        updated = edit_accountant_rates(dialog, rates, use_saved_terms)
        if updated is not None:
            rates, use_saved_terms = updated
            refresh()

    rate_button.clicked.connect(change_rates)

    def export_pdf():
        selection = selected()
        _label, period_start, cutoff = periods[period_buttons.checkedId()]
        copies = {}
        for entry in selection:
            if entry["member"]["kind"] != "expense":
                continue
            index = entry_rows[entry["member"]["key"]]
            if table.item(index, 6).checkState() == Qt.CheckState.Checked:
                document = entry["document"]
                copies[expense_document_key(document)] = document
        settings = dict(include_trips=include_trips.isChecked(), include_journal=include_journal.isChecked(),
                        rates=dict(rates), use_saved_terms=use_saved_terms)
        try:
            from payroll_preview import open_preview
            translations = {}
            if settings["include_journal"]:
                journal_rows = [entry["row"] for entry in selection
                                if entry["member"]["kind"] == "shift" and (entry["row"].get("comment") or "").strip()]
                if journal_rows:
                    translations = host._collect_journal_translations({"journal_rows": journal_rows}, parent=dialog)
                    if translations is None:
                        return
            pdf_report = {**report, "journal_translations": translations}
            filename = "Следующий-счёт.pdf"
            with tempfile.TemporaryDirectory(prefix="SmartReport-accountant-preview-") as folder:
                path = Path(folder) / filename
                def build(update):
                    documents = list(copies.values())
                    download = getattr(host, "_download_object_report_originals", None)
                    sources, errors = download(documents, folder, update) if documents and download else ({}, {})
                    write_accountant_report(path, obj, selection, pdf_report, cutoff,
                                            period_start=period_start, copy_documents=documents,
                                            source_paths=sources, source_errors=errors,
                                            font=desktop.DEFAULT_FONT, bold_font=desktop.DEFAULT_FONT_BOLD, **settings)

                build_loop = QEventLoop()
                result = []
                task = desktop.ObjectPackageTask(0, build)
                progress = QProgressDialog("Собираем PDF для бухгалтера…", "", 0, 0, dialog)
                progress.setCancelButton(None)
                progress.setWindowModality(Qt.WindowModality.WindowModal)
                task.signals.progress.connect(lambda _, message: progress.setLabelText(message))

                def finished(_, _value, error):
                    result.append(error)
                    build_loop.quit()

                task.signals.completed.connect(finished)
                progress.show()
                QThreadPool.globalInstance().start(task)
                build_loop.exec()
                progress.close()
                if result[0]:
                    raise RuntimeError(result[0])
                open_preview(path, (Path(desktop.REPORTS_DIR) / filename).resolve(), dialog,
                             title="Предпросмотр документа бухгалтеру")
        except Exception as error:
            QMessageBox.warning(dialog, "PDF не сформирован", str(error))

    def commit():
        nonlocal committed, submitted_payload
        payload = {"operation_id": operation_id, "object_id": obj["server_id"], "purpose": "accountant",
                   "invoice_number": number.text().strip(), "invoice_date": invoice_date.date().toString("yyyy-MM-dd"),
                   "members": [entry["member"] for entry in selected()]}
        if submitted_payload is not None and submitted_payload != payload:
            QMessageBox.warning(dialog, "Проверьте результат предыдущей попытки", "Закройте окно и откройте следующий счёт заново для проверки сохранённого состава.")
            return
        if QMessageBox.question(dialog, "Сохранить состав фактуры?",
                                f"Фактура №{payload['invoice_number']} · строк {len(payload['members'])}.\n"
                                "Эти строки будут исключены из следующего счёта.",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        submitted_payload = payload
        save_button.setEnabled(False)
        try:
            desktop.request_expense_api("POST", "/api/customer-invoices", json=payload)
            committed = True
            synchronize = getattr(host, "background_sync_with_cloud", None)
            if synchronize:
                synchronize()
            QMessageBox.information(dialog, "Состав сохранён", "Часы и расходы связаны с клиентской фактурой. Обновите данные для отображения статуса часов.")
            dialog.accept()
        except Exception as error:
            QMessageBox.warning(dialog, "Сохранение требует проверки", str(error))
            refresh()

    pdf_button.clicked.connect(export_pdf)
    save_button.clicked.connect(commit)
    apply_period(period_buttons.checkedId())
    dialog.exec()
