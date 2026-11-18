"""Desktop workflow for the next customer invoice and its immutable membership."""
from decimal import Decimal
from pathlib import Path
import uuid
import tempfile

from PyQt6.QtCore import QEventLoop, QDate, QThreadPool, Qt
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QFileDialog, QProgressDialog,
)

from customer_billing import prepare_next_invoice
from expense_documents import parse_decimal
from expense_categories import confirmed_expense_category
from object_package_report import collect_object_expenses, _recover_report_share_tax
from accountant_report import write_accountant_report


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
        supported = "accountant_invoice_batches_v1" in capabilities.get("features", [])
        if obj.get("server_id") is None:
            raise ValueError("Сначала синхронизируйте объект с сервером.")
        state = (desktop.request_expense_api("GET", "/api/customer-invoices", params={"object_id": obj["server_id"]})
                 if "customer_invoice_batches_v1" in capabilities.get("features", []) else {"members": {}, "batches": []})
        documents = desktop.fetch_all_expense_documents(None, None)
        entries, warnings = prepare_next_invoice(report["journal_rows"], documents, obj["server_id"], cutoff,
                                                 state["members"], accountant=True)
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
    dialog = QDialog(host)
    dialog.setWindowTitle(f"Документы бухгалтеру к следующему счёту · {obj['name']}")
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
    table.setHorizontalHeaderLabels(["Включить", "Дата", "Категория", "Часы / OB / документ", "Сумма документа", "Состояние"])
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
        shift = entry["member"]["kind"] == "shift"
        allocation = entry.get("display_allocation", entry.get("allocation", {}))
        available = shift or (parse_decimal(allocation.get("gross_amount")) is not None and entry["currency"] != "UNKNOWN")
        if not available:
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setCheckState(Qt.CheckState.Checked if available else Qt.CheckState.Unchecked)
        table.setItem(index, 0, item)
        entry["category"] = (
            "Наши часы и поездки" if entry["member"]["kind"] == "shift" else
            confirmed_expense_category(entry["allocation"].get("expense_category"),
                                       entry["allocation"].get("expense_category_source"))[0]
        )
        description = entry["label"]
        if shift:
            row = entry["row"]
            description += f"\nПоездки · {row.get('transport') or 0:g}"
            if row["entry_type"] == "ob":
                description += f"\nOB · {row['hours']:g} ч"
                from customer_report import CATEGORY_LABELS
                for part in report.get("journal_segments", {}).get(row["sync_id"], []):
                    description += f"\n{CATEGORY_LABELS.get(part['category'], part['category'])} · {part['hours']:g} ч"
        net = parse_decimal(allocation.get("net_amount"))
        gross = parse_decimal(allocation.get("gross_amount"))
        amount_text = "—" if shift else (
            f"{net:,.2f} {entry['currency']} без НДС" if net is not None else
            f"{gross:,.2f} {entry['currency']} с НДС" if gross is not None else "Сумма не указана")
        for column, text in enumerate((entry["date"], entry["category"], description,
                                       amount_text, "К передаче бухгалтеру" if available else "Нет суммы документа или валюты"), 1):
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
        categories = {}
        hours = trips = ob_hours = Decimal(0)
        for entry in selected():
            if entry["member"]["kind"] == "shift":
                hours += Decimal(str(entry["row"]["hours"]))
                if entry["row"]["entry_type"] == "ob":
                    ob_hours += Decimal(str(entry["row"]["hours"]))
                trips += Decimal(str(entry["row"].get("transport") or 0))
                continue
            allocation = entry.get("display_allocation", entry["allocation"])
            net = parse_decimal(allocation.get("net_amount"))
            amount = net if net is not None else parse_decimal(allocation.get("gross_amount"))
            if amount is not None:
                key = (entry["category"], entry["currency"], "без НДС" if net is not None else "с НДС")
                categories[key] = categories.get(key, Decimal(0)) + amount
        lines = [f"Часы · {hours:g} ч · Из них смены OB · {ob_hours:g} ч · Поездки · {trips:g}",
                 "Стоимость работы, OB и поездок определяет бухгалтер."]
        lines += [f"{category} · {value:,.2f} {currency} {basis}"
                  for (category, currency, basis), value in sorted(categories.items())]
        total_label.setText("\n".join(lines))
        valid = bool(selected())
        save_button.setEnabled(supported and valid and reviewed.isChecked()
                               and bool(number.text().strip()) and not committed)
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
            write_accountant_report(Path(path), obj, selection, report, cutoff,
                                    font=desktop.DEFAULT_FONT, bold_font=desktop.DEFAULT_FONT_BOLD)
            QMessageBox.information(dialog, "Отчёт сохранён", path)
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
    refresh()
    dialog.exec()
