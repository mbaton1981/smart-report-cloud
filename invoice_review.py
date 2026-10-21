import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSplitter, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

from desktop_ui import ReportTable as QTableWidget
from document_preview import DocumentGalleryDialog
from expense_documents import (
    decimal_text, due_date_edit, due_date_issues, invoice_context,
    net_invoice_issues, non_invoice_document_type, parse_invoice_money,
)


class InvoiceReviewMixin:
    def _review_net_invoice(self, document, source_paths, parsed_fields, server_document=None, *,
                            trial_save=None, trial_objects=None, reviewed_save=None):
        fields = {**parsed_fields, **document}
        source_text = str(fields.get("raw_text") or fields.get("recognized_text") or "")
        non_invoice_type = fields.get("non_invoice_document_type") or non_invoice_document_type(source_text)
        order_confirmation_detected = non_invoice_type == "order_confirmation"
        context = invoice_context(source_text)
        context["prepaid"] = context["prepaid"] or fields.get("prepaid") is True
        context["reverse_charge"] = context["reverse_charge"] or fields.get("reverse_charge") is True
        server_document = server_document or document
        client_id = str(fields.get("client_id") or uuid.uuid4())
        preview_paths = list(source_paths or fields.get("local_attachment_paths") or [])
        if not preview_paths and fields.get("has_original"):
            preview_paths = self._expense_document_original_paths(server_document)
        dialog = QDialog(self)
        local_review = reviewed_save is not None
        dialog.setWindowTitle("ЛОКАЛЬНЫЙ ТЕСТ — проверка фактуры" if local_review else
                              "Тестовая почтовая очередь — распределение" if trial_save
                              else "Проверка фактуры и распределение расходов")
        dialog.resize(1040, 760)
        dialog.setMinimumSize(860, 650)
        layout = QVBoxLayout(dialog)
        if trial_save:
            from mail_import import mail_review_warnings
        if trial_save and not local_review:
            trial_note = QLabel("ТЕСТОВЫЙ КАТАЛОГ. Сохранение только в почтовой тестовой очереди. "
                                "Подтверждение, рабочий учёт и синхронизация отключены.")
            trial_note.setWordWrap(True)
            layout.addWidget(trial_note)
            trial_warnings = QLabel("\n".join(mail_review_warnings(fields)))
            trial_warnings.setObjectName("invoiceTrialDocumentWarnings")
            trial_warnings.setWordWrap(True)
            layout.addWidget(trial_warnings)
        elif local_review:
            local_note = QLabel(
                "ЛОКАЛЬНЫЙ ТЕСТ. До подтверждения — только черновик очереди. "
                "Для сохранения выберите объект, проверьте оригинал и подтвердите действие. "
                "Рабочий сервер не используется."
            )
            local_note.setWordWrap(True)
            layout.addWidget(local_note)
        elif non_invoice_type:
            classification_note = QLabel(
                "Это уведомление о заказе, а не подтверждённая фактура."
                if order_confirmation_detected
                else "Это решение о транспортном налоге, а не фактура."
            )
            classification_note.setWordWrap(True)
            layout.addWidget(classification_note)
        if non_invoice_type and not trial_save:
            review_label = (
                "Представьте саму фактуру или чек. Подтверждение заказа нельзя подтвердить как расход."
                if order_confirmation_detected
                else "Налоговое решение нельзя подтвердить как фактуру."
            )
            review_note = QLabel(review_label)
            review_note.setWordWrap(True)
            layout.addWidget(review_note)
        top = QSplitter(Qt.Orientation.Horizontal)
        top.setChildrenCollapsible(False)
        gallery = None
        if preview_paths:
            try:
                gallery = DocumentGalleryDialog(preview_paths, "Оригинал", dialog)
                gallery.setWindowFlags(Qt.WindowType.Widget)
                top.addWidget(gallery)
                dialog.finished.connect(lambda _result: gallery.close())
            except Exception as error:
                top.addWidget(QLabel(f"Оригинал недоступен: {error}"))
        else:
            top.addWidget(QLabel("Оригинал не прикреплён"))
        details = QWidget()
        form = QFormLayout(details)
        inputs = {}
        for key, label in (
            ("supplier_name", "Поставщик"), ("invoice_number", "Номер фактуры"),
            ("payment_reference", "OCR / reference"),
            ("document_date", "Дата (ГГГГ-ММ-ДД)"), ("due_date", "Срок оплаты (ГГГГ-ММ-ДД)"), ("currency_code", "Валюта"),
            ("net_amount", "Сумма без НДС"), ("vat_amount", "НДС фактуры"),
            ("rounding_amount", "Округление"), ("amount_due", "К оплате"),
        ):
            value = fields.get(key)
            if key == "currency_code" and value == "UNKNOWN":
                value = None
            widget = QLineEdit(str(value) if value is not None else "")
            if key == "rounding_amount":
                widget.setPlaceholderText("Не указано в документе")
            elif key == "currency_code":
                widget.setPlaceholderText("Не указана: выберите валюту")
            elif key == "due_date":
                widget.setPlaceholderText("Не указан или требует проверки")
            widget.setObjectName("invoice_" + key)
            inputs[key] = widget
            form.addRow(("Остаток к оплате" if key == "amount_due" and context["prepaid"] else label) + ":", widget)
        original_due_text = inputs["due_date"].text().strip()
        due_origin = fields.get("due_date_kind") or ("manual" if original_due_text else "absent")
        due_origin_note = QLabel()
        due_origin_note.setObjectName("invoiceDueDateOrigin")
        due_origin_note.setWordWrap(True)
        form.addRow("Источник срока:", due_origin_note)
        due_reviewed = QCheckBox("Срок оплаты и его источник сверены")
        due_reviewed.setObjectName("invoiceDueDateReviewed")
        due_reviewed.setChecked(fields.get("due_date_reviewed") is True)
        due_reviewed.setVisible(bool(fields.get("due_date_review_required") or due_origin in {"calculated", "ambiguous"}))
        form.addRow(due_reviewed)
        document_total = QLabel()
        document_total.setObjectName("invoiceDocumentTotal")
        if context["prepaid"]:
            form.addRow("Сумма документа с НДС:", document_total)
            prepaid_note = QLabel("Предоплаченный документ: общий итог не равен остатку к оплате. Пока доступен только черновик.")
            prepaid_note.setWordWrap(True)
            form.addRow(prepaid_note)
        tax_reviewed = QCheckBox("Обратное начисление проверено по оригиналу")
        tax_reviewed.setObjectName("invoiceTaxReviewed")
        tax_reviewed.setChecked(fields.get("tax_reviewed") is True)
        if context["reverse_charge"]:
            form.addRow(tax_reviewed)
        top.addWidget(details)
        top.setSizes([500, 500])
        layout.addWidget(top, 3)
        table = QTableWidget(0, 6)
        table.setObjectName("invoiceAllocations")
        table.setHorizontalHeaderLabels(["Заказ / пометка", "Без НДС", "Назначение", "Объект", "Категория", "Источник"])
        table.setMinimumHeight(155)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(table, 2)
        source_rows = []
        objects = list(trial_objects if trial_objects is not None else getattr(self, "_expense_objects", []))
        distribution = QLabel()
        distribution.setObjectName("invoiceDistribution")
        issues_label = QLabel()
        issues_label.setObjectName("invoiceIssues")
        issues_label.setWordWrap(True)
        row_actions = QHBoxLayout()
        add_row_button = QPushButton("Добавить строку")
        add_row_button.setObjectName("invoiceAddAllocation")
        remove_row_button = QPushButton("Удалить строку")
        remove_row_button.setObjectName("invoiceRemoveAllocation")
        row_actions.addWidget(add_row_button)
        row_actions.addWidget(remove_row_button)
        row_actions.addWidget(distribution, 1)
        layout.addLayout(row_actions)
        layout.addWidget(issues_label)
        ambiguity_reviewed = QCheckBox("Неоднозначные строки исправлены и сверены с оригиналом")
        ambiguity_reviewed.setObjectName("invoiceAmbiguityReviewed")
        ambiguity_reviewed.setVisible(any(item.get("ambiguous") for item in fields.get("order_proposals") or []))
        layout.addWidget(ambiguity_reviewed)
        original_reviewed = QCheckBox("Я проверил оригинал и выбранный объект")
        original_reviewed.setObjectName("invoiceOriginalReviewed")
        original_reviewed.setVisible(local_review)
        layout.addWidget(original_reviewed)
        extra_toggle = QPushButton("Дополнительно")
        extra_toggle.setCheckable(True)
        extra = QTextEdit()
        extra.setReadOnly(True)
        extra.setMaximumHeight(90)
        extra.setPlainText(str(fields.get("raw_text") or fields.get("recognized_text") or "")
                           + "\n\n" + "\n".join(fields.get("review_reasons") or []))
        extra.hide()
        extra_toggle.toggled.connect(extra.setVisible)
        layout.addWidget(extra_toggle)
        layout.addWidget(extra)
        actions = QHBoxLayout()
        draft = QPushButton("Сохранить черновик")
        if trial_save and not local_review:
            draft.setText("Сохранить только в тестовой очереди")
        draft.setObjectName("invoiceSaveDraft")
        confirm = QPushButton("Подтвердить распределение")
        confirm.setObjectName("invoiceConfirm")
        cancel = QPushButton("Отмена")
        actions.addWidget(draft)
        actions.addStretch()
        actions.addWidget(confirm)
        actions.addWidget(cancel)
        layout.addLayout(actions)

        def values():
            current = {key: widget.text().strip() for key, widget in inputs.items()}
            current.update({**context, "tax_reviewed": tax_reviewed.isChecked(), "recognized_text": source_text})
            current.update(due_date_edit(fields, current["due_date"], due_reviewed.isChecked()))
            if trial_save:
                current["document_type"] = fields.get("document_type") or "unclassified"
                current["mail_review_warnings"] = mail_review_warnings(current)
            rows = []
            for index, original in enumerate(source_rows):
                destination = table.cellWidget(index, 2).currentData()
                object_id = table.cellWidget(index, 3).currentData() if destination == "object" else None
                amount = table.item(index, 1).text().strip()
                unchanged = parse_invoice_money(amount) == parse_invoice_money(original.get("net_amount"))
                rows.append({
                    **original, "original_marker": table.item(index, 0).text().strip(),
                    "net_amount": amount or None, "destination": destination,
                    "object_id": object_id, "expense_category": table.cellWidget(index, 4).text().strip(),
                    "vat_amount": original.get("vat_amount") if unchanged else None,
                    "gross_amount": None,
                    "ambiguous": bool(original.get("ambiguous")) and not ambiguity_reviewed.isChecked(),
                })
                if trial_save and not unchanged:
                    rows[-1]["vat_items"] = []
            return current, rows

        def refresh(*_):
            current, rows = values()
            if trial_save and not local_review:
                trial_warnings.setText("\n".join(current["mail_review_warnings"]))
            origin_names = {"explicit": "Явно указан в документе", "calculated": "Рассчитан по условиям оплаты — требуется сверка",
                            "manual": "Изменён вручную", "ambiguous": "Неоднозначный срок — укажите проверенную дату",
                            "absent": "Срок в документе не найден"}
            due_origin_note.setText(origin_names.get(current["due_date_kind"], "Источник требует проверки"))
            due_reviewed.setVisible(current["due_date_review_required"])
            net_value = parse_invoice_money(current["net_amount"])
            vat_value = parse_invoice_money(current["vat_amount"])
            document_total.setText(decimal_text(net_value + vat_value) if net_value is not None and vat_value is not None else "Неизвестно")
            allocated = sum((parse_invoice_money(row["net_amount"]) or Decimal(0) for row in rows
                             if row.get("destination") == "company"
                             or row.get("destination") == "object" and row.get("object_id") is not None), Decimal(0))
            total = parse_invoice_money(current["net_amount"])
            remaining = decimal_text(total - allocated) if total is not None else "неизвестно"
            distribution.setText(f"Без НДС: распределено {decimal_text(allocated)} / осталось {remaining}")
            issues = net_invoice_issues(current, rows)
            if not current["supplier_name"]:
                issues.append("Укажите поставщика.")
            if not current["invoice_number"]:
                issues.append("Укажите номер фактуры.")
            try:
                datetime.strptime(current["document_date"], "%Y-%m-%d")
            except ValueError:
                issues.append("Укажите корректную дату фактуры.")
            if current["currency_code"].upper() not in {"SEK", "EUR", "NOK", "DKK", "USD", "GBP", "CHF", "PLN"}:
                issues.append("Выберите валюту.")
            if local_review:
                if fields.get("document_type") != "invoice":
                    issues.append("В этом тесте подтверждается только обычная фактура.")
                if not original_reviewed.isChecked():
                    issues.append("Сверьте оригинал и выбранный объект.")
                if not rows or any(row.get("destination") != "object"
                                   or row.get("object_id") is None for row in rows):
                    issues.append("Выберите объект для каждой строки.")
            issues_label.setText("Для подтверждения: " + " ".join(issues[:3]) if issues else "Суммы согласованы. Назначения выбраны.")
            confirm.setEnabled(
                not issues and (trial_save is None or local_review) and non_invoice_type is None
                and non_invoice_type != "tax_decision"
            )
            if trial_save and not local_review:
                confirm.setToolTip("Перенос в рабочий учёт отключён для тестовой очереди.")
            elif order_confirmation_detected:
                confirm.setToolTip("Для учёта загрузите саму фактуру или чек; заказ не подтверждается как расход.")
            elif non_invoice_type == "tax_decision":
                confirm.setToolTip("Решение о транспортном налоге нельзя подтвердить как фактуру.")

        def append_row(original=None):
            original = dict(original or {})
            row = table.rowCount()
            table.blockSignals(True)
            table.insertRow(row)
            marker = str(original.get("original_marker") or original.get("project_code") or "")
            table.setItem(row, 0, QTableWidgetItem(marker))
            table.item(row, 0).setToolTip("Заказ: " + str(original.get("order_id") or "не указан"))
            table.setItem(row, 1, QTableWidgetItem(str(original.get("net_amount") or "")))
            destination = QComboBox()
            for label, value in (("Выберите назначение", "unselected"), ("На объект", "object"), ("Общие расходы фирмы", "company")):
                destination.addItem(label, value)
            object_combo = QComboBox()
            object_combo.addItem("Выберите объект", None)
            for item in objects:
                object_combo.addItem(f"{item['name']} ({item.get('markning') or ''})", int(item["id"]))
            selected = original.get("object_id")
            if (selected is None and original.get("project_code")
                    and (trial_save is None or "destination" not in original)):
                matches = [int(item["id"]) for item in objects if str(item.get("markning") or "").casefold()
                           == str(original["project_code"]).casefold()]
                selected = matches[0] if len(matches) == 1 else None
            if selected is not None:
                object_combo.setCurrentIndex(max(0, object_combo.findData(selected)))
            selected_destination = original.get("destination") or ("object" if selected is not None else "unselected")
            destination.setCurrentIndex(max(0, destination.findData(selected_destination)))
            category = QLineEdit(str(original.get("expense_category") or ""))
            category.setPlaceholderText("Необязательно")
            table.setCellWidget(row, 2, destination)
            table.setCellWidget(row, 3, object_combo)
            table.setCellWidget(row, 4, category)
            sources = original.get("sources") or []
            source_button = QPushButton("Стр. " + str(sources[0]["page"]) if sources else "Нет страницы")
            source_button.setEnabled(bool(gallery and sources))
            if gallery and sources:
                source_button.clicked.connect(lambda _checked=False, source=sources[0]: gallery.show_source(
                    int(source.get("attachment_index") or 0), int(source["page"])))
            table.setCellWidget(row, 5, source_button)
            source_rows.append(original)
            table.blockSignals(False)

            def destination_changed(*_):
                object_combo.setEnabled(destination.currentData() == "object")
                category.setEnabled(destination.currentData() == "company")
                refresh()

            destination.currentIndexChanged.connect(destination_changed)
            object_combo.currentIndexChanged.connect(refresh)
            category.textChanged.connect(refresh)
            destination_changed()

        def remove_row():
            index = table.currentRow()
            if index >= 0:
                table.removeRow(index)
                source_rows.pop(index)
                refresh()

        initial_rows = fields.get("order_proposals") or fields.get("allocations") or [{"net_amount": fields.get("net_amount")}]
        for row in initial_rows:
            append_row(row)
        table.itemChanged.connect(refresh)
        for widget in inputs.values():
            widget.textChanged.connect(refresh)
        ambiguity_reviewed.toggled.connect(refresh)
        tax_reviewed.toggled.connect(refresh)
        due_reviewed.toggled.connect(refresh)
        original_reviewed.toggled.connect(refresh)
        add_row_button.clicked.connect(lambda: append_row())
        remove_row_button.clicked.connect(remove_row)

        def save(status):
            if trial_save is not None and not local_review and status != "draft":
                return
            if status == "confirmed" and non_invoice_type == "tax_decision":
                QMessageBox.warning(dialog, "Тип документа", "Решение о транспортном налоге нельзя подтвердить как фактуру.")
                return
            if status == "confirmed" and order_confirmation_detected:
                QMessageBox.warning(
                    dialog, "Тип документа",
                    "Подтверждение заказа нельзя подтвердить как расход. Загрузите саму фактуру или чек.",
                )
                return
            current, rows = values()
            if current["due_date"]:
                date_errors = due_date_issues({"due_date": current["due_date"], "due_date_kind": "manual"})
                if date_errors:
                    QMessageBox.warning(dialog, "Срок оплаты", " ".join(date_errors))
                    return
            for key in ("net_amount", "vat_amount", "rounding_amount", "amount_due"):
                if current[key] and parse_invoice_money(current[key]) is None:
                    QMessageBox.warning(dialog, "Формат суммы", "Исправьте формат денежной суммы перед сохранением.")
                    return
            if any(row.get("net_amount") and parse_invoice_money(row["net_amount"]) is None for row in rows):
                QMessageBox.warning(dialog, "Распределение", "Исправьте формат суммы без НДС в строках.")
                return
            if status == "confirmed" and (net_invoice_issues(current, rows) or not confirm.isEnabled()):
                return
            if status == "confirmed" and local_review:
                answer = QMessageBox.question(
                    dialog, "Подтвердить тестовый расход?",
                    "Сохранить эту фактуру на выбранные объекты только в локальном тестовом реестре? "
                    "После подтверждения возможна повторная отправка этого же документа при сбое.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            net = parse_invoice_money(current["net_amount"])
            vat = parse_invoice_money(current["vat_amount"])
            payload = {**current, "client_id": client_id, "allocation_basis": "net",
                       "currency_code": current["currency_code"].upper() or "UNKNOWN",
                       "gross_amount": decimal_text(net + vat) if net is not None and vat is not None else None,
                       "verification_status": status, "allocations": rows, "order_proposals": rows,
                       "recognized_text": fields.get("raw_text") or fields.get("recognized_text") or "",
                       "document_comment": fields.get("document_comment") or "",
                       "expense_category": fields.get("expense_category") or "Прочие расходы"}
            known_hashes = [item["sha256"] for item in fields.get("attachments") or [] if item.get("sha256")]
            if known_hashes:
                payload["required_attachment_sha256"] = known_hashes
            if trial_save is not None:
                try:
                    if status == "confirmed" and local_review:
                        reviewed_save(payload)
                    else:
                        trial_save(payload)
                except Exception as error:
                    QMessageBox.warning(dialog, "Не сохранено в тестовой очереди", str(error))
                    return
                dialog.accept()
                return
            staged = []
            try:
                if source_paths:
                    staged = self._store_expense_originals(source_paths, client_id)
                server_id = server_document.get("server_document_id")
                if server_id is None and isinstance(server_document.get("id"), int):
                    server_id = server_document["id"]
                self.queue_expense_document_operation(
                    client_id, "upsert", payload, server_document_id=server_id,
                    server_version=server_document.get("server_version") or server_document.get("sync_version"),
                    attachment_paths=staged if source_paths else fields.get("local_attachment_paths"),
                )
            except Exception as error:
                for path in staged:
                    Path(path).unlink(missing_ok=True)
                QMessageBox.warning(dialog, "Не сохранено", str(error))
                return
            dialog.accept()
            self.load_expense_documents()
            self.background_sync_with_cloud()

        draft.clicked.connect(lambda: save("draft"))
        confirm.clicked.connect(lambda: save("confirmed"))
        cancel.clicked.connect(dialog.reject)
        refresh()
        try:
            return dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            if gallery is not None:
                gallery.close()
            dialog.deleteLater()
