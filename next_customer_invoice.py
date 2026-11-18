"""Desktop workflow for the next customer invoice and its immutable membership."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import uuid

from PyQt6.QtCore import QEventLoop, QDate, QThreadPool, Qt
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QFileDialog, QProgressDialog,
)

from customer_billing import prepare_next_invoice, priced_report
from expense_documents import parse_decimal
from expense_categories import confirmed_expense_category
from object_package_report import collect_object_expenses, write_object_report_package


def price_shift_entry(report, row):
    calculation = priced_report(report, [row])
    totals = calculation["totals"]
    total = parse_decimal(totals.get("invoice_total"))
    work = parse_decimal(totals.get("work_amount"))
    ob = parse_decimal(totals.get("ob_amount"))
    travel = parse_decimal(totals.get("transport_amount"))
    incomplete = [line["label"] for line in calculation["cost_lines"] if line.get("amount") is None]
    reason = ("Не указана цена поездки" if incomplete and all(label.startswith("Resa") for label in incomplete)
              else "Требуется уточнить клиентские цены" if incomplete else "К включению в счёт")
    return {"net": total if total is not None else parse_decimal(totals.get("known_subtotal")),
            "complete": total is not None, "work_net": work + ob if work is not None and ob is not None else None,
            "travel_net": travel, "reason": reason}


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
    cutoff = host.inv_date_to.date().toString("yyyy-MM-dd")

    def fetch():
        report = host.collect_invoice_report(selection={
            "company": obj["company"], "object_id": obj["id"], "sync_id": obj["sync_id"],
            "date_from": "1900-01-01", "date_to": cutoff,
        })
        capabilities = desktop.request_expense_api("GET", "/api/expense-documents/capabilities")
        supported = "customer_invoice_batches_v1" in capabilities.get("features", [])
        if obj.get("server_id") is None:
            raise ValueError("Сначала синхронизируйте объект с сервером.")
        state = (desktop.request_expense_api("GET", "/api/customer-invoices", params={"object_id": obj["server_id"]})
                 if supported else {"members": {}, "batches": []})
        documents = desktop.fetch_all_expense_documents(None, None)
        entries, warnings = prepare_next_invoice(report["journal_rows"], documents, obj["server_id"], cutoff, state["members"])
        for entry in entries:
            if entry["member"]["kind"] == "shift":
                entry.update(price_shift_entry(report, entry["row"]))
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
    dialog = QDialog(host)
    dialog.setWindowTitle(f"Следующий счёт · {obj['name']}")
    dialog.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
    dialog.resize(1180, 760)
    layout = QVBoxLayout(dialog)
    note = QLabel(
        f"Состав по {cutoff} включительно. Включены ранние расходы, ещё не связанные с клиентской фактурой. "
        "Номер выставленной фактуры сохранит состав для следующего счёта."
    )
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
    table = QTableWidget(len(entries), 6)
    table.setHorizontalHeaderLabels(["Включить", "Дата", "Категория", "Часы / документ", "Сумма без НДС", "Состояние"])
    table.setColumnWidth(0, 80)
    table.setColumnWidth(1, 115)
    table.setColumnWidth(2, 190)
    table.setColumnWidth(3, 330)
    table.setColumnWidth(4, 160)
    table.setColumnWidth(5, 235)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    layout.addWidget(table, 1)
    for index, entry in enumerate(entries):
        item = QTableWidgetItem()
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        net = entry.get("net")
        if net is None:
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setCheckState(Qt.CheckState.Checked if net is not None else Qt.CheckState.Unchecked)
        table.setItem(index, 0, item)
        entry["category"] = (
            "Наши часы и поездки" if entry["member"]["kind"] == "shift" else
            confirmed_expense_category(entry["allocation"].get("expense_category"),
                                       entry["allocation"].get("expense_category_source"))[0]
        )
        work_text = (f"{entry['work_net']:,.2f} SEK" if entry.get("work_net") is not None else
                     "Неизвестно" if entry["member"]["kind"] == "shift" else "")
        travel_text = (f"{entry['travel_net']:,.2f} SEK" if entry.get("travel_net") is not None else
                       "Цена не указана" if entry["member"]["kind"] == "shift" else "")
        description = entry["label"]
        if entry["member"]["kind"] == "shift":
            description += f"\nРабота · {work_text}\nПоездки · {travel_text}"
        for column, text in enumerate((entry["date"], entry["category"], description,
                                       f"{net:,.2f} {entry['currency']}" if net is not None else "Неизвестно",
                                       entry.get("reason") or ("Нет достоверной суммы или валюты" if net is None else "К включению в счёт")), 1):
            table.setItem(index, column, QTableWidgetItem(text))
    table.resizeRowsToContents()
    form = QFormLayout()
    number = QLineEdit()
    number.setPlaceholderText("Номер фактуры, которую вы выставили клиенту")
    invoice_date = desktop.create_date_field(QDate.currentDate().toString("yyyy-MM-dd"))
    form.addRow("Номер клиентской фактуры", number)
    form.addRow("Дата фактуры", invoice_date)
    layout.addLayout(form)
    reviewed = QCheckBox("Сверил состав и прежние счета клиенту")
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

    def selected():
        return [entry for index, entry in enumerate(entries)
                if table.item(index, 0).checkState() == Qt.CheckState.Checked]

    def refresh(*_):
        totals, categories = {}, {}
        for entry in selected():
            if entry.get("net") is not None:
                totals[entry["currency"]] = totals.get(entry["currency"], Decimal(0)) + entry["net"]
                key = (entry["category"], entry["currency"])
                categories[key] = categories.get(key, Decimal(0)) + entry["net"]
        lines = [f"{category} · {value:,.2f} {currency} без НДС" for (category, currency), value in sorted(categories.items())]
        incomplete = any(entry.get("complete") is False for entry in selected())
        lines.append(("Известная часть без НДС · " if incomplete else "Итого без НДС · ")
                     + " · ".join(f"{value:,.2f} {currency}" for currency, value in sorted(totals.items())))
        if incomplete:
            lines.append("Стоимость часов включена. Для сохранения выбранного состава уточните недостающие цены.")
        total_label.setText("\n".join(lines))
        valid = bool(selected()) and all(entry.get("net") is not None for entry in selected())
        save_button.setEnabled(supported and valid and not incomplete and len(totals) == 1 and reviewed.isChecked()
                               and bool(number.text().strip()) and not committed)
        if len(totals) > 1:
            total_label.setText(total_label.text() + " · Для выставления выберите одну валюту.")
        pdf_button.setEnabled(valid)

    table.itemChanged.connect(refresh)
    number.textChanged.connect(refresh)
    reviewed.toggled.connect(refresh)

    def export_pdf():
        selection = selected()
        path, _filter = QFileDialog.getSaveFileName(dialog, "Предварительный отчёт к счёту", "Следующий-счёт.pdf", "PDF (*.pdf)")
        if not path:
            return
        try:
            shift_rows = [entry["row"] for entry in selection if entry["member"]["kind"] == "shift"]
            expense_docs = {}
            for entry in selection:
                if entry["member"]["kind"] != "expense":
                    continue
                document_id = entry["document"]["id"]
                if document_id not in expense_docs:
                    expense_docs[document_id] = deepcopy(entry["document"])
                    document = expense_docs[document_id]
                    document["allocations"] = []
                document = expense_docs[document_id]
                document["allocations"].append(entry["allocation"])
            expenses, excluded = collect_object_expenses(list(expense_docs.values()), obj["server_id"])
            data = {"object": obj, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "period_label": f"К следующему счёту · по {cutoff}", "language": "ru",
                    "customer_report": priced_report(report, shift_rows), "expenses": expenses,
                    "excluded_expenses": excluded, "expense_snapshot_available": True,
                    "include_copies": False, "include_customer_cost": True,
                    "incomplete_reasons": ["Предварительный состав клиентской фактуры."],
                    }
            write_object_report_package(Path(path), data, font=desktop.DEFAULT_FONT, bold_font=desktop.DEFAULT_FONT_BOLD)
            QMessageBox.information(dialog, "Отчёт сохранён", path)
        except Exception as error:
            QMessageBox.warning(dialog, "PDF не сформирован", str(error))

    def commit():
        nonlocal committed, submitted_payload
        payload = {"operation_id": operation_id, "object_id": obj["server_id"],
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
    refresh()
    dialog.exec()
