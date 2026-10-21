"""Offline-only test desktop; never imports app.py or connects to mail/API."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QScrollArea, QSplitter, QTextEdit,
    QVBoxLayout, QWidget,
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
TYPES = {"invoice": "Фактура", "receipt": "Чек", "unclassified": "Тип не установлен"}


class EmbeddedOriginal(DocumentGalleryDialog):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "preview"):
            QTimer.singleShot(0, self.preview.fit_width)


class OfflineReviewWindow(QDialog):
    def __init__(self, store: OfflineReviewStore, *, integrated=False):
        super().__init__()
        self.store = store
        self.integrated = integrated
        self.gallery = None
        self.inputs, self.origins = {}, {}
        self.setWindowTitle("SmartReport — ЛОКАЛЬНЫЙ ТЕСТ: входящие PDF")
        self.resize(1450, 940)
        layout = QVBoxLayout(self)
        banner = QLabel(
            ("PDF распознаётся автоматически. Проверьте поля, выберите объект и добавьте расход.\n"
             "Получение почты — только по кнопке, только INBOX.SmartReport-Test. Расписания нет.\n"
             if integrated else
             "ОТДЕЛЬНЫЙ ТЕСТОВЫЙ ПРОФИЛЬ. Почта, рабочая база и синхронизация отключены.\n")
            + f"Профиль: {store.root}")
        banner.setWordWrap(True)
        layout.addWidget(banner)
        self.selector = QComboBox()
        for doc in store.documents():
            source = json.loads(store.conn.execute(
                "SELECT source_json FROM documents WHERE id=?", (doc["id"],)).fetchone()[0])
            filename = next((item["filename"] for item in source.get("sources", [])
                             if item.get("filename")), source["original"])
            fields = source["result"]["fields"]
            self.selector.addItem(
                f"№{doc['id']} — {fields.get('supplier_name') or 'Документ для сверки'} — "
                f"{fields.get('invoice_number') or filename[:50]}", doc["id"])
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
        form.addRow("Подтверждённый тип:", self.kind)
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
                label = "Сумма расхода с НДС, включая округление"
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
        self.object = QComboBox()
        self.object.setObjectName("review_object")
        self.object.addItem("Обязательно выберите объект", None)
        for obj in store.objects():
            self.object.addItem(obj["name"], obj["id"])
        form.addRow("Объект:", self.object)
        self.checked = QCheckBox("Сверил оригинал, реквизиты и отсутствие повтора покупки")
        form.addRow(self.checked)
        self.history = QTextEdit()
        self.history.setReadOnly(True)
        self.history.setMaximumHeight(120)
        form.addRow("История правок:", self.history)
        scroll.setWidget(details)
        self.splitter.addWidget(scroll)
        self.splitter.setSizes([800, 600])
        buttons = QHBoxLayout()
        self.save_button = QPushButton("Сохранить исправления — без расхода")
        self.transfer_button = QPushButton("Подтвердить и перенести")
        self.registry_button = QPushButton("Тестовый реестр")
        if integrated:
            self.registry_button.setText("Журнал подтверждённых передач")
            self.transfer_button.setText("Добавить к расходам объекта")
            self.reassign_button = QPushButton("Сменить объект — без нового расхода")
            self.reassign_button.clicked.connect(self.reassign_object)
            buttons.addWidget(self.reassign_button)
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
        return {**{key: widget.text().strip() or None for key, widget in self.inputs.items()},
                "confirmed_type": self.kind.currentData(), "object_id": self.object.currentData(),
                "original_checked": self.checked.isChecked(),
                "settlement_status": None if self.integrated else
                    "prepaid" if self.receipt_paid.isChecked() else None}

    def reload_documents(self):
        self.selector.blockSignals(True)
        selected = self.selector.currentData()
        self.selector.clear()
        for doc in self.store.documents():
            base = self.store.base(doc["id"])
            self.selector.addItem(
                f"№{doc['id']} — {base.get('supplier_name') or 'Для ручного заполнения'} — "
                f"{base.get('invoice_number') or 'PDF'}", doc["id"])
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
            return
        self.revision, payload = self.store.latest(self.document_id)
        base = self.store.base(self.document_id)
        for key, field in self.inputs.items():
            field.blockSignals(True)
            field.setText(str(payload[key]) if payload.get(key) not in (None, "UNKNOWN") else "")
            field.blockSignals(False)
        self.kind.setCurrentIndex(max(0, self.kind.findData(payload.get("confirmed_type"))))
        self.object.setCurrentIndex(max(0, self.object.findData(payload.get("object_id"))))
        self.checked.setChecked(payload.get("original_checked") is True)
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
        if self.gallery:
            self.gallery.hide()
            self.preview_layout.removeWidget(self.gallery)
            self.gallery.close()
            self.gallery.deleteLater()
            self.gallery = None
        self.original_available = False
        try:
            path = self.store.original(self.document_id)
            self.gallery = EmbeddedOriginal([path], "Исходный PDF", self)
            self.gallery.setWindowFlags(Qt.WindowType.Widget)
            self.preview_layout.addWidget(self.gallery)
            self.original_available = True
        except (ReviewError, OSError, ValueError) as error:
            self.status.setText(f"Оригинал недоступен; перенос запрещён: {error}")
        card = self.store.card(self.document_id)
        for field in self.inputs.values():
            field.setReadOnly(card is not None)
        self.object.setEnabled(card is None)
        self.checked.setEnabled(card is None)
        self.receipt_paid.setEnabled(card is None)
        self.transfer_button.setEnabled(
            (kind in ("invoice", "receipt") or self.integrated and kind == "unclassified"
             and base.get("suggested_document_type") != "payment_reminder")
            and self.original_available and card is None)
        if self.integrated:
            self.reassign_button.setEnabled(card is not None)
        self.save_button.setEnabled(card is None)
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

    def keep_unsaved(self):
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
        self.status.setText(f"Версия {self.revision} сохранена отдельно. Расход не создан.")
        return True

    def confirm_transfer(self):
        problems = self.store.issues(self.document_id, self.payload())
        if problems:
            QMessageBox.warning(self, "Перенос запрещён", "\n".join(problems))
            return
        answer = QMessageBox.question(
            self, "Подтверждение передачи" if self.integrated else "Только тестовый реестр",
            ("Добавить один расход с исходным PDF к выбранному объекту?\n"
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
                         f"{payload['confirmed_type']} · объект {payload['object_id']}\n"
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
