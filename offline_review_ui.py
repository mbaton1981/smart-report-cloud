"""Offline-only test desktop; never imports app.py or connects to mail/API."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QScrollArea, QSplitter, QTextEdit,
    QVBoxLayout, QWidget, QTableWidget, QTableWidgetItem, QHeaderView,
)

from document_preview import DocumentGalleryDialog
from offline_review import EDITABLE, OfflineReviewStore, ReviewError


LABELS = {
    "supplier_name": "Поставщик / продавец", "invoice_number": "Номер фактуры / чека",
    "payment_reference": "OCR / reference", "document_date": "Дата документа",
    "due_date": "Срок оплаты", "currency_code": "Валюта",
    "net_amount": "Нетто", "vat_amount": "НДС", "gross_amount": "Итог до округления / итог чека",
    "rounding_amount": "Указанное округление", "amount_due": "Долг / к оплате (не для чека)",
}
TYPES = {"invoice": "Фактура", "receipt": "Чек", "unclassified": "Тип не установлен",
         "tax_decision": "Решение о транспортном налоге", "order_confirmation": "Подтверждение заказа",
         "credit_note": "Кредитная фактура", "proforma": "Проформа"}


class OfflineReviewWindow(QDialog):
    def __init__(self, store: OfflineReviewStore, *, integrated=False):
        super().__init__()
        self.store = store
        self.integrated = integrated
        self.gallery = None
        self.preview_document_id = None
        self.inputs, self.origins = {}, {}
        self.setWindowTitle("SmartReport — Входящие документы" if integrated
                            else "SmartReport — ЛОКАЛЬНЫЙ ТЕСТ: входящие PDF")
        self.resize(1450, 940)
        layout = QVBoxLayout(self)
        banner = QLabel(
            ("PDF распознаётся автоматически. Проверьте поля и распределите общую сумму по объектам.\n"
             "Получение почты — только по кнопке из основной папки INBOX. Расписания нет.\n"
             if integrated else
             "ОТДЕЛЬНЫЙ ТЕСТОВЫЙ ПРОФИЛЬ. Почта, рабочая база и синхронизация отключены.\n")
            + f"Профиль: {store.root}")
        banner.setWordWrap(True)
        layout.addWidget(banner)
        self.selector = QComboBox()
        for doc in store.documents():
            self.selector.addItem(self.document_label(doc["id"]), doc["id"])
        layout.addWidget(self.selector)
        self.classification = QLabel()
        self.classification.setWordWrap(True)
        layout.addWidget(self.classification)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(self.splitter, 1)
        self.preview_host = QWidget()
        self.preview_layout = QVBoxLayout(self.preview_host)
        self.splitter.addWidget(self.preview_host)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        details = QWidget()
        form = QFormLayout(details)
        self.kind = QComboBox()
        self.kind.addItem("Не подтверждён — сверьте оригинал", None)
        self.kind.addItem("Подтверждаю: фактура", "invoice")
        self.kind.addItem("Подтверждаю: чек", "receipt")
        if not integrated:
            form.addRow("Подтверждённый тип:", self.kind)
        else:
            self.kind.hide()
        self.receipt_paid = QCheckBox("Сверил: чек полностью оплачен; долг поставщику не создаётся.\n"
                                     "Возмещение сотруднику этим действием не оформляется.")
        if not integrated:
            form.addRow(self.receipt_paid)
        else:
            self.receipt_paid.hide()
        for key, label in LABELS.items():
            if integrated and key in ("due_date", "amount_due", "rounding_amount"):
                continue
            if integrated and key == "gross_amount":
                label = "Сумма расхода по документу"
            if integrated and key == "supplier_name":
                label = "Поставщик / организация / получатель сбора"
            if integrated and key in ("invoice_number", "vat_amount", "net_amount"):
                label += " (если указан)"
            row = QWidget()
            box = QVBoxLayout(row)
            box.setContentsMargins(0, 0, 0, 0)
            field = QLineEdit()
            field.setObjectName("review_" + key)
            field.setPlaceholderText("Не указано / не распознано")
            origin = QLabel()
            origin.setWordWrap(True)
            box.addWidget(field)
            box.addWidget(origin)
            form.addRow(label + ":", row)
            self.inputs[key], self.origins[key] = field, origin
            field.textChanged.connect(self.update_origins)
        self.amount_choices = QComboBox()
        if integrated:
            self.amount_label = QLabel("Найденные суммы — выберите по оригиналу:")
            form.addRow(self.amount_label, self.amount_choices)
        else:
            self.amount_choices.hide()
        self.amount_choices.currentIndexChanged.connect(self.choose_amount)
        self.object = QComboBox()
        self.object.setObjectName("review_object")
        self.object.addItem("Обязательно выберите объект", None)
        for obj in store.objects():
            self.object.addItem(obj["name"], obj["id"])
        if integrated:
            self.object.hide()
            self.allocations = QTableWidget(0, 3)
            self.allocations.setObjectName("review_allocations")
            self.allocations.setHorizontalHeaderLabels(["Объект", "Сумма", ""])
            header = self.allocations.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            self.allocations.setMinimumHeight(160)
            form.addRow("Распределение по объектам:", self.allocations)
            self.add_allocation_button = QPushButton("Добавить объект — сумму")
            self.add_allocation_button.clicked.connect(lambda: self.add_allocation())
            form.addRow(self.add_allocation_button)
            self.allocation_total = QLabel()
            form.addRow(self.allocation_total)
            self.allocations.itemChanged.connect(self.update_allocation_total)
            self.inputs["gross_amount"].textChanged.connect(self.update_allocation_total)
        else:
            form.addRow("Объект:", self.object)
        self.checked = QCheckBox("Сверил оригинал, реквизиты и отсутствие повтора покупки")
        form.addRow(self.checked)
        self.history = QTextEdit()
        self.history.setReadOnly(True)
        self.history.setMaximumHeight(120)
        form.addRow("История правок:", self.history)
        scroll.setWidget(details)
        self.financial_panel = scroll
        self.splitter.addWidget(scroll)
        self.splitter.setSizes([800, 600])
        buttons = QHBoxLayout()
        self.save_button = QPushButton("Сохранить исправления — без расхода")
        self.transfer_button = QPushButton("Подтвердить и перенести")
        self.registry_button = QPushButton("Тестовый реестр")
        if integrated:
            self.registry_button.setText("Журнал подтверждённых передач")
            self.transfer_button.setText("Добавить к расходам объекта")
            self.reassign_button = QPushButton("Открыть актуальное распределение")
            self.reassign_button.clicked.connect(self.refresh_distribution)
            buttons.addWidget(self.reassign_button)
            actions = QHBoxLayout()
            self.ignore_button = QPushButton("Не учитывать")
            self.later_button = QPushButton("Разобрать позже")
            self.expense_button = QPushButton("Учесть как расход")
            self.ignore_button.clicked.connect(lambda: self.set_action("ignore"))
            self.later_button.clicked.connect(lambda: self.set_action("later"))
            self.expense_button.clicked.connect(lambda: self.set_action("expense"))
            for button in (self.ignore_button, self.later_button, self.expense_button):
                actions.addWidget(button)
            layout.addLayout(actions)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.transfer_button)
        buttons.addWidget(self.registry_button)
        layout.addLayout(buttons)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.selector.currentIndexChanged.connect(self.change_document)
        self.save_button.clicked.connect(self.save_edits)
        self.transfer_button.clicked.connect(self.confirm_transfer)
        self.registry_button.clicked.connect(self.show_registry)
        self.select_document()

    def payload(self):
        result = {**{key: widget.text().strip() or None for key, widget in self.inputs.items()},
                "confirmed_type": None if self.integrated else self.kind.currentData(),
                "object_id": self.object.currentData(),
                "original_checked": self.checked.isChecked(),
                "settlement_status": None if self.integrated else
                    "prepaid" if self.receipt_paid.isChecked() else None}
        if self.integrated:
            parts = [{"object_id": self.allocations.cellWidget(row, 0).currentData(),
                      "gross_amount": self.allocations.item(row, 1).text().strip() or None}
                     for row in range(self.allocations.rowCount())]
            if parts:
                result["allocations"] = parts
            elif self.object.currentData() is None:
                result["allocations"] = []
            if getattr(self, "server_version", None) is not None:
                result["server_version"] = self.server_version
        return result

    def add_allocation(self, part=None):
        part = part or {}
        row = self.allocations.rowCount()
        self.allocations.insertRow(row)
        combo = QComboBox()
        combo.addItem("Выберите объект", None)
        for obj in self.store.objects():
            combo.addItem(obj["name"], obj["id"])
        selected = part.get("object_id")
        if selected is not None and combo.findData(selected) < 0:
            combo.addItem(f"Объект #{selected} — недоступен", selected)
        combo.setCurrentIndex(max(0, combo.findData(selected)))
        combo.setToolTip(combo.currentText())
        combo.currentTextChanged.connect(combo.setToolTip)
        self.allocations.setCellWidget(row, 0, combo)
        self.allocations.setItem(row, 1, QTableWidgetItem(str(part.get("gross_amount") or "")))
        remove = QPushButton("Убрать")
        def remove_row():
            for index in range(self.allocations.rowCount()):
                if self.allocations.cellWidget(index, 2) is remove:
                    self.allocations.removeRow(index)
                    break
            self.update_allocation_total()
        remove.clicked.connect(remove_row)
        self.allocations.setCellWidget(row, 2, remove)
        self.update_allocation_total()

    def update_allocation_total(self, *_):
        if not self.integrated or not hasattr(self, "allocation_total"):
            return
        from decimal import Decimal
        from expense_documents import parse_invoice_money, decimal_text
        amounts = [parse_invoice_money(self.allocations.item(row, 1).text())
                   for row in range(self.allocations.rowCount()) if self.allocations.item(row, 1)]
        total = parse_invoice_money(self.inputs["gross_amount"].text())
        if any(value is None or not value.is_finite() for value in amounts):
            self.allocation_total.setText("Проверьте суммы частей. Черновик можно сохранить.")
            return
        allocated = sum(amounts, Decimal(0))
        self.allocation_total.setText(
            f"Распределено: {decimal_text(allocated)}; общая сумма: {decimal_text(total) or 'не указана'}. "
            + ("Суммы совпадают." if total == allocated and amounts else
               "Для учёта распределите всю сумму. Можно сохранить черновик."))

    def refresh_distribution(self):
        try:
            if not self.keep_unsaved():
                return
            refresh = getattr(self.store.host, "refresh_incoming_register", None)
            if refresh is None:
                raise ReviewError("Обновите обычный реестр и откройте карточку заново.")
            refresh()
            self.select_document()
        except (ReviewError, OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            QMessageBox.warning(self, "Распределение не обновлено", str(error))

    def choose_amount(self):
        value = self.amount_choices.currentData()
        if value is not None:
            self.inputs["gross_amount"].setText(value)

    def set_action(self, action):
        try:
            self.action_revision = self.store.set_action(self.document_id, action, self.action_revision)
        except (ReviewError, OSError, sqlite3.Error) as error:
            QMessageBox.warning(self, "Статус не изменён", str(error))
            return
        # Status changes do not parse, save, or discard visible financial edits.
        self.update_action_controls()
        self.refresh_document_labels()
        labels = {"ignore": "Не учитывать — PDF сохранён, расход не создан",
                  "later": "Разобрать позже — PDF сохранён, расход не создан",
                  "expense": "Проверьте данные и распределите сумму по объектам. Номер и НДС необязательны."}
        self.status.setText(labels[action])
        if action in ("ignore", "later") and self.payload() != self.loaded_payload:
            self.status.setText(labels[action] + ". Несохранённые правки полей не применены.")
        self.status.setStyleSheet("")

    def update_action_controls(self):
        if not self.integrated or self.document_id is None:
            return
        self.action_revision, action = self.store.action(self.document_id)
        card = self.store.card(self.document_id)
        editing = action == "expense" or card is not None
        self.financial_panel.setVisible(editing)
        self.save_button.setVisible(editing)
        self.transfer_button.setVisible(editing)
        self.reassign_button.setVisible(card is not None)
        for button in (self.ignore_button, self.later_button, self.expense_button):
            button.setEnabled(card is None)
        self.transfer_button.setEnabled(
            card is None and self.original_available and action == "expense"
            and self.store.base(self.document_id).get("suggested_document_type") != "payment_reminder")

    def document_label(self, document_id):
        _, fields = (self.store.current_review(document_id) if self.integrated
                     else self.store.latest(document_id))
        supplier = fields.get("supplier_name") or "Поставщик не распознан"
        date = fields.get("document_date") or "Дата не распознана"
        amount = fields.get("gross_amount")
        total = (f"{amount} {fields.get('currency_code') or ''}".strip()
                 if amount not in (None, "") else "Сумма не распознана")
        status = (self.store.accounting_status(document_id)[0] if self.integrated
                  else "Требует проверки")
        return f"№{document_id} — {supplier} — {date} — {total} — {status}"

    def refresh_document_labels(self):
        self.selector.blockSignals(True)
        try:
            for index in range(self.selector.count()):
                document_id = self.selector.itemData(index)
                label = self.document_label(document_id)
                if self.selector.itemText(index) != label:
                    self.selector.setItemText(index, label)
                if self.integrated:
                    _, green = self.store.accounting_status(document_id)
                    self.selector.setItemData(index, QColor("#b9efc6") if green else None,
                                              Qt.ItemDataRole.BackgroundRole)
        finally:
            self.selector.blockSignals(False)

    def reload_documents(self):
        self.selector.blockSignals(True)
        selected = self.selector.currentData()
        self.selector.clear()
        for doc in self.store.documents():
            self.selector.addItem(self.document_label(doc["id"]), doc["id"])
            if self.integrated:
                _, green = self.store.accounting_status(doc["id"])
                if green:
                    self.selector.setItemData(self.selector.count() - 1, QColor("#b9efc6"),
                                              Qt.ItemDataRole.BackgroundRole)
        self.selector.setCurrentIndex(max(0, self.selector.findData(selected)))
        self.selector.blockSignals(False)
        self.select_document()

    def refresh_objects(self):
        selected = self.object.currentData()
        self.object.blockSignals(True)
        self.object.clear()
        self.object.addItem("Обязательно выберите объект", None)
        for obj in self.store.objects():
            self.object.addItem(obj["name"], obj["id"])
        self.object.setCurrentIndex(max(0, self.object.findData(selected)))
        self.object.blockSignals(False)

    def reassign_object(self):
        from PyQt6.QtWidgets import QInputDialog
        objects = self.store.objects()
        names = [f"{row['name']} (#{row['id']})" for row in objects]
        name, accepted = QInputDialog.getItem(self, "Изменение привязки", "Новый объект:", names, 0, False)
        if not accepted:
            return
        try:
            self.store.reassign(self.document_id, objects[names.index(name)]["id"], self.revision)
        except (ReviewError, OSError, sqlite3.Error, ValueError) as error:
            QMessageBox.warning(self, "Объект не изменён", str(error))
            return
        self.select_document()
        self.status.setText("Изменение объекта сохранено в очереди. Обновляется та же карточка, не новый расход.")

    def update_origins(self):
        if not hasattr(self, "document_id"):
            return
        provenance = self.store.provenance(self.document_id, self.payload())
        for key, label in self.origins.items():
            label.setText(provenance[key])

    def select_document(self):
        self.document_id = self.selector.currentData()
        if self.document_id is None:
            self.save_button.setEnabled(False)
            self.transfer_button.setEnabled(False)
            if self.integrated:
                self.financial_panel.hide()
                for button in (self.ignore_button, self.later_button, self.expense_button):
                    button.setEnabled(False)
            return
        self.revision, payload = (self.store.current_review(self.document_id) if self.integrated
                                  else self.store.latest(self.document_id))
        self.server_version = payload.get("server_version")
        base = self.store.base(self.document_id)
        for key, field in self.inputs.items():
            field.blockSignals(True)
            field.setText(str(payload[key]) if payload.get(key) not in (None, "UNKNOWN") else "")
            field.blockSignals(False)
        self.kind.setCurrentIndex(max(0, self.kind.findData(payload.get("confirmed_type"))))
        self.object.setCurrentIndex(max(0, self.object.findData(payload.get("object_id"))))
        if self.integrated:
            self.allocations.setRowCount(0)
            for part in self.store.allocations(payload):
                self.add_allocation(part)
            self.update_allocation_total()
        self.checked.setChecked(payload.get("original_checked") is True)
        self.amount_choices.blockSignals(True)
        self.amount_choices.clear()
        if self.integrated:
            candidates = self.store.cost_fields(self.document_id).get("amount_candidates") or []
            self.amount_label.setVisible(bool(candidates))
            self.amount_choices.setVisible(bool(candidates))
            self.amount_choices.addItem("Не выбрано — сверьте суммы в оригинале", None)
            for candidate in candidates:
                context = "; ".join(f"стр. {item['page']}: {item['text']}"
                                    for item in candidate["evidence"])
                self.amount_choices.addItem(f"{candidate['amount']} — {context}", candidate["amount"])
            self.amount_choices.setCurrentIndex(max(0, self.amount_choices.findData(payload.get("gross_amount"))))
            self.amount_choices.setEnabled(self.store.card(self.document_id) is None)
        self.amount_choices.blockSignals(False)
        self.receipt_paid.setChecked(payload.get("settlement_status") == "prepaid")
        kind = base.get("document_type")
        self.receipt_paid.setVisible(kind == "receipt" and not self.integrated)
        self.classification.setText(
            f"Предполагаемый тип: {TYPES.get(kind, kind)}. "
            "Он не считается подтверждённым до вашего выбора.\n" +
            ("НАПОМИНАНИЕ: это не новая покупка. Нужна исходная фактура и сверка уже учтённого расхода."
             if base.get("suggested_document_type") == "payment_reminder"
             else "Чек: номер фактуры не обязателен; оплата не влияет на учёт расхода."
             if kind == "receipt" else
             "Объект всегда выбирается вручную; коды проектов в PDF не означают выбор объекта."))
        if self.integrated:
            self.classification.setText(
                f"Предполагаемый тип: {TYPES.get(kind, kind)}. Это подсказка распознавания, не подтверждение типа.\n"
                + ("Напоминание не является новым расходом — выберите статус без финансовых полей."
                   if base.get("suggested_document_type") == "payment_reminder"
                   else "Выберите действие. Номер фактуры и НДС не требуются, если их нет в документе."))
            if len(candidates) > 1 and payload.get("gross_amount") is None:
                self.classification.setText(self.classification.text()
                                            + "\nНайдено несколько сумм: выберите подходящую по подписи и странице оригинала.")
        if self.gallery and self.preview_document_id != self.document_id:
            self.gallery.hide()
            self.preview_layout.removeWidget(self.gallery)
            self.gallery.close()
            self.gallery.deleteLater()
            self.gallery = None
        self.original_available = self.gallery is not None
        if self.gallery is None:
            try:
                path = self.store.original(self.document_id)
                self.gallery = DocumentGalleryDialog([path], "Исходный PDF", self)
                self.gallery.setWindowFlags(Qt.WindowType.Widget)
                self.preview_layout.addWidget(self.gallery)
                self.preview_document_id = self.document_id
                self.gallery.show()
                self.original_available = True
            except (ReviewError, OSError, ValueError) as error:
                self.status.setText(f"Оригинал недоступен; перенос запрещён: {error}")
        card = self.store.card(self.document_id)
        editable = card is None or self.integrated and self.server_version is not None
        for field in self.inputs.values():
            field.setReadOnly(not editable)
        if self.integrated:
            self.allocations.setEnabled(editable)
            self.add_allocation_button.setEnabled(editable)
        self.object.setEnabled(card is None)
        self.checked.setEnabled(editable)
        self.receipt_paid.setEnabled(card is None)
        self.transfer_button.setEnabled(
            (kind in ("invoice", "receipt") or self.integrated and kind == "unclassified"
             and base.get("suggested_document_type") != "payment_reminder")
            and self.original_available and card is None)
        if self.integrated:
            self.reassign_button.setEnabled(card is not None)
        self.save_button.setEnabled(editable)
        self.save_button.setText("Сохранить изменения существующего расхода" if card and self.integrated
                                 else "Сохранить исправления — без расхода")
        self.kind.setEnabled(card is None and (kind in ("invoice", "receipt") or self.integrated))
        history = self.store.history(self.document_id)
        self.history.setPlainText("\n".join(
            f"Версия {row['revision']} · {row['created_at']}\n{row['changes_json']}" for row in history))
        if self.original_available:
            self.status.setText(f"Версия {self.revision}. " +
                                ((f"Подтверждение №{card['id']} сохранено. Доставка — отдельная очередь; доступен повтор передачи."
                                  if self.integrated else f"Уже перенесено: карточка №{card['id']}. Повтор не создаётся.")
                                                 if card else "Исправления сохраняются отдельно. Расход пока не создан."))
        self.update_origins()
        self.loaded_payload = self.payload()
        self.loaded_index = self.selector.currentIndex()
        if self.integrated:
            text, green = self.store.accounting_status(self.document_id)
            self.status.setText(text)
            self.status.setStyleSheet("background:#b9efc6;color:#14552b;padding:6px" if green else "")
            self.update_action_controls()
        self.refresh_document_labels()

    def keep_unsaved(self):
        if self.integrated and getattr(self, "document_id", None) is not None and self.store.action(self.document_id)[1] in ("ignore", "later"):
            return True
        if not hasattr(self, "loaded_payload") or self.payload() == self.loaded_payload:
            return True
        answer = QMessageBox.question(
            self, "Несохранённые исправления", "Сохранить изменения черновика перед выходом из него?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
            QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Save)
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            try:
                self.revision = self.store.save(self.document_id, self.payload(), self.revision)
                self.loaded_payload = self.payload()
            except (ReviewError, OSError, sqlite3.Error) as error:
                QMessageBox.warning(self, "Исправления не сохранены", str(error))
                return False
        return True

    def change_document(self):
        if not self.keep_unsaved():
            self.selector.blockSignals(True)
            self.selector.setCurrentIndex(self.loaded_index)
            self.selector.blockSignals(False)
            return
        self.select_document()

    def save_edits(self):
        try:
            self.revision = self.store.save(self.document_id, self.payload(), self.revision)
        except (ReviewError, OSError, sqlite3.Error) as error:
            QMessageBox.warning(self, "Исправления не сохранены", str(error))
            return False
        self.select_document()
        self.status.setText("Изменения той же карточки поставлены в очередь; при конфликте требуется сверка."
                            if self.integrated and self.store.card(self.document_id)
                            else f"Версия {self.revision} сохранена отдельно. Расход не создан.")
        return True

    def confirm_transfer(self):
        problems = self.store.issues(self.document_id, self.payload())
        if problems:
            QMessageBox.warning(self, "Перенос запрещён", "\n".join(problems))
            return
        answer = QMessageBox.question(
            self, "Подтверждение передачи" if self.integrated else "Только тестовый реестр",
            ("Добавить один расход с исходным PDF и указанным распределением по объектам?\n"
             if self.integrated else "Создать одну тестовую карточку с исходным PDF?\n") +
            "Это отдельное подтверждение переноса, не просто сохранение исправлений.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.transfer_button.setEnabled(False)
        try:
            if self.store.card(self.document_id) is None:
                self.revision = self.store.save(self.document_id, self.payload(), self.revision)
            card = self.store.transfer(self.document_id, self.revision)
        except (ReviewError, OSError, sqlite3.Error) as error:
            QMessageBox.warning(self, "Перенос не завершён", str(error))
            self.transfer_button.setEnabled(self.original_available)
            self.status.setText("Перенос не завершён. Введённые поля оставлены в форме; "
                                "проверьте сообщение об ошибке перед повторной попыткой.")
            return
        self.select_document()
        if not self.integrated:
            self.status.setText(f"Тестовая карточка №{card['id']} сохранена с исходным PDF. Повтор безопасен.")

    def show_registry(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Журнал передач — статус доставки смотрите в обычном реестре"
                              if self.integrated else "ТЕСТОВЫЙ реестр — не рабочие расходы")
        dialog.resize(850, 400)
        layout = QVBoxLayout(dialog)
        viewer = QTextEdit()
        viewer.setReadOnly(True)
        lines = []
        selection = QComboBox()
        for row in self.store.conn.execute("SELECT * FROM register ORDER BY id"):
            payload = json.loads(row["payload_json"])
            selection.addItem(f"Карточка №{row['id']} — {payload['supplier_name']}", row["document_id"])
            lines.append(f"Карточка №{row['id']} · {payload['supplier_name']} · "
                         f"{'Расход по документу' if self.integrated else payload['confirmed_type']} · объект {payload['object_id']}\n"
                         + (f"Сумма расхода: {payload['gross_amount']} · "
                            if self.integrated else f"Долг: {row['debt_amount']} · ")
                         + f"PDF SHA-256: {row['sha256']}\n")
        viewer.setPlainText("\n".join(lines) or "Карточек нет. Черновики не являются расходами.")
        layout.addWidget(viewer)
        layout.addWidget(selection)
        button = QPushButton("Открыть PDF выбранной карточки")
        button.setEnabled(selection.count() > 0)
        button.clicked.connect(lambda: self.open_registered_original(dialog, selection.currentData()))
        layout.addWidget(button)
        dialog.exec()

    def open_registered_original(self, parent, document_id):
        try:
            preview = DocumentGalleryDialog([self.store.original(document_id)], "PDF карточки", parent)
        except (ReviewError, OSError, ValueError) as error:
            QMessageBox.warning(parent, "PDF недоступен", str(error))
            return
        preview.exec()
        preview.close()

    def closeEvent(self, event):
        if not self.keep_unsaved():
            event.ignore()
            return
        if self.gallery:
            self.gallery.close()
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-profile", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    app = QApplication([sys.argv[0]])
    store = OfflineReviewStore(args.test_profile)
    try:
        if args.snapshot:
            store.import_snapshot(args.snapshot)
        window = OfflineReviewWindow(store)
        window.show()
        return app.exec()
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
