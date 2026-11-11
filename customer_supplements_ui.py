"""Compact administrator workflow for allocating recorded hours, not editing time."""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import sqlite3
import sys

from PyQt6.QtCore import Qt, QSignalBlocker
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)
from requests import RequestException

import customer_supplements as supplements
from customer_pricing import row_prices, store_pricing


class SupplementDialog(QDialog):
    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Рассчитать доплаты — сохранённые часы")
        self.setObjectName("customerSupplementDialog")
        self.rows = [row for row in report["journal_rows"] if row["entry_type"] in ("work", "ob")]
        self.drafts = {}
        self.loading = False
        self.editor_dirty = False
        layout = QVBoxLayout(self)
        title = QLabel(f"{report['company']} · {report['object']} · "
                       f"{report['date_from']} — {report['date_to']}")
        title.setWordWrap(True)
        layout.addWidget(title)
        note = QLabel("1. Отметьте записи. 2. Выберите схему и распределите часы. "
                      "3. Проверьте каждую строку. 4. Подтвердите расчёт, затем закройте период штатно.\n"
                      "OB уже входит в часы. Проценты клиентские, без НДС; зарплата и время не меняются.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(len(self.rows), 8)
        self.table.setObjectName("customerSupplementRows")
        self.table.setMinimumHeight(185)
        self.table.setHorizontalHeaderLabels(["Выбор", "Дата / №", "Сотрудник", "Часы",
                                              "База/ч", "База SEK", "Доплата SEK", "Итог / статус"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        for index, row in enumerate(self.rows):
            quote = row_prices(row)
            enabled = not row["invoiced"] and not quote.get("fixed")
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                           if enabled else Qt.ItemFlag.NoItemFlags)
            check.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(index, 0, check)
            for col, value in enumerate((f"{row['date']} · №{row['id']}", row["employee"], f"{row['hours']:g}",
                                          quote["base"] if quote["base"] is not None else "Не задана"), 1):
                self.table.setItem(index, col, QTableWidgetItem(str(value)))
            self.show_amounts(index, quote.get("supplement_allocation"),
                              "Закрыта — защищена" if row["invoiced"] else "Fast pris — защищён"
                              if quote.get("fixed") else "Не распределено")
        layout.addWidget(self.table, 1)
        selection = QHBoxLayout()
        for label, state in (("Выбрать открытые", Qt.CheckState.Checked), ("Снять выбор", Qt.CheckState.Unchecked)):
            button = QPushButton(label)
            button.clicked.connect(lambda _, state=state: self.select_open(state))
            selection.addWidget(button)
        self.selected_note = QLabel("Одно распределение применяется к КАЖДОЙ отмеченной записи.")
        selection.addWidget(self.selected_note, 1)
        layout.addLayout(selection)
        options = QHBoxLayout()
        self.scheme = QComboBox()
        self.scheme.setObjectName("customerSupplementScheme")
        self.scheme.addItem("Выберите схему…", None)
        for key, name in supplements.SCHEMES.items():
            self.scheme.addItem(name, key)
        self.scheme.wheelEvent = lambda event: event.ignore()
        options.addWidget(self.scheme, 1)
        self.weekend = QCheckBox("Выходной +100% — явно согласовано")
        self.weekend.setObjectName("customerSupplementWeekend")
        options.addWidget(self.weekend)
        layout.addLayout(options)
        warning = QLabel("Выходной +100% — отдельное условие заказчика (двойная база), не правило зарплаты. "
                         "В письме +100% связано с >40 ч: автоматического недельного порога здесь нет. "
                         "Неописанные часы не получают доплату; обычный остаток подтвердите явно.")
        warning.setWordWrap(True)
        layout.addWidget(warning)
        grid = QGridLayout()
        grid.setVerticalSpacing(3)
        for col, text in enumerate(("Ценовая строка (не фактическое время)", "OB", "Övertid", "Часов на каждую запись")):
            grid.addWidget(QLabel(text), 0, col)
        self.fields = {}
        for index, bucket in enumerate(supplements.BUCKETS, 1):
            grid.addWidget(QLabel(supplements.LABELS[bucket]), index, 0)
            for col, scheme in enumerate(supplements.SCHEMES, 1):
                percent = supplements.PERCENTAGES[scheme][index - 1]
                grid.addWidget(QLabel("Не определено" if percent is None else f"+{percent}%"), index, col)
            edit = QLineEdit("0")
            edit.setObjectName("customerSupplementHours_" + bucket)
            edit.setMaximumWidth(180)
            edit.setStyleSheet("QLineEdit { min-height: 18px; padding: 3px; }")
            edit.setMaximumHeight(28)
            edit.textChanged.connect(self.field_edited)
            self.fields[bucket] = edit
            grid.addWidget(edit, index, 3)
        layout.addLayout(grid)
        self.balance = QLabel()
        self.balance.setWordWrap(True)
        layout.addWidget(self.balance)
        actions = QHBoxLayout()
        exact = QPushButton("Предложить по точным интервалам")
        exact.clicked.connect(self.suggest)
        actions.addWidget(exact)
        ordinary = QPushButton("Все часы текущей записи — обычные")
        ordinary.clicked.connect(self.all_ordinary)
        actions.addWidget(ordinary)
        stage = QPushButton("Предпросмотр для выбранных")
        stage.setObjectName("previewCustomerSupplements")
        stage.clicked.connect(self.preview)
        actions.addWidget(stage)
        layout.addLayout(actions)
        self.status = QLabel("Ничего не выбрано и не подтверждено.")
        self.status.setWordWrap(True)
        self.status.setObjectName("customerSupplementStatus")
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Подтвердить расчёт")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.table.currentCellChanged.connect(self.load_row)
        self.scheme.currentIndexChanged.connect(self.options_changed)
        self.weekend.toggled.connect(self.options_changed)
        self.resize(1060, 740)
        if self.rows:
            self.table.setCurrentCell(0, 1)

    def select_open(self, state):
        for i in range(len(self.rows)):
            if self.table.item(i, 0).flags() & Qt.ItemFlag.ItemIsEnabled:
                self.table.item(i, 0).setCheckState(state)

    def show_amounts(self, index, allocation, fallback=""):
        values = ("—", "—", fallback)
        if allocation:
            parts = allocation["parts"]
            values = tuple(f"{sum(Decimal(p[key]) for p in parts):.2f}"
                           for key in ("base_amount", "premium_amount", "total"))
            values = (*values[:2], values[2] + (" · предпросмотр" if index in self.drafts else " · сохранено"))
        for col, value in enumerate(values, 5):
            self.table.setItem(index, col, QTableWidgetItem(value))

    def current_row(self):
        index = self.table.currentRow()
        if index < 0:
            raise ValueError("Выберите строку записи.")
        return index, self.rows[index]

    def quantities(self):
        return {key: field.text().strip().replace(",", ".") or "0" for key, field in self.fields.items()}

    def update_balance(self):
        if self.loading:
            return
        try:
            _, row = self.current_row()
            total = sum(supplements.number(value) for value in self.quantities().values())
            actual = supplements.number(row["hours"])
            self.balance.setText(f"Текущая запись: {actual:g} ч. Распределено: {total:g} ч. "
                                 f"Обычный нераспределённый остаток: {actual-total:g} ч (введите явно).")
        except ValueError as error:
            self.balance.setText(str(error))

    def field_edited(self):
        if not self.loading:
            self.editor_dirty = True
        self.update_balance()

    def load_row(self, *_):
        index, row = self.current_row()
        value = self.drafts.get(index, row_prices(row)).get("supplement_allocation")
        self.loading = True
        with QSignalBlocker(self.scheme), QSignalBlocker(self.weekend):
            self.scheme.setCurrentIndex(self.scheme.findData(value["scheme"]) if value else 0)
            self.weekend.setChecked(value["weekend"] if value else False)
        quantities = {p["bucket"]: p["hours"] for p in value["parts"]} if value else {}
        for key, field in self.fields.items():
            field.setText(quantities.get(key, "0"))
        self.loading = False
        self.editor_dirty = False
        self.update_balance()

    def options_changed(self, *_):
        if self.loading:
            return
        for field in self.fields.values():
            field.setText("0")
        self.status.setText("Схема выбрана только для нового предпросмотра, не для всего периода.")

    def suggest(self):
        try:
            _, row = self.current_row()
            if self.weekend.isChecked():
                raise ValueError("Выходные часы распределяются только по явно согласованному условию +100%.")
            quantities = supplements.proposed_hours(row, self.scheme.currentData())
            for key, field in self.fields.items():
                field.setText(quantities.get(key, "0"))
            self.status.setText("Предложено по сохранённому точному времени. Проверьте и отметьте нужные записи.")
        except ValueError as error:
            self.status.setText(str(error))

    def all_ordinary(self):
        try:
            _, row = self.current_row()
            if self.scheme.currentData() is None:
                self.scheme.setCurrentIndex(1)
            self.weekend.setChecked(False)
            for key, field in self.fields.items():
                field.setText(str(row["hours"]) if key == "ordinary" else "0")
            self.status.setText("Все сохранённые часы явно указаны обычными без доплаты. Проверьте выбор записей.")
        except ValueError as error:
            self.status.setText(str(error))

    def preview(self):
        try:
            indices = [i for i in range(len(self.rows))
                       if self.table.item(i, 0).checkState() == Qt.CheckState.Checked]
            if not indices:
                raise ValueError("Отметьте записи для применения. Сам выбор схемы ничего не меняет.")
            stamp = datetime.now(timezone.utc).isoformat()
            proposed = {}
            for i in indices:
                row = self.rows[i]
                try:
                    proposed[i] = supplements.confirmed_quote(
                        row, row_prices(row), self.scheme.currentData(), self.quantities(),
                        weekend=self.weekend.isChecked(), confirmed_at=stamp)
                except ValueError as error:
                    raise ValueError(f"№{row['id']} · {row['employee']}: {error} Группа не применена.") from None
            self.drafts.update(proposed)
            self.editor_dirty = False
            for i, quote in proposed.items():
                self.show_amounts(i, quote["supplement_allocation"])
                self.table.item(i, 0).setCheckState(Qt.CheckState.Unchecked)
            self.status.setText(f"Предпросмотр: {len(self.drafts)} записей. Суммы в таблице относятся К КАЖДОЙ записи. "
                                "Транспорт оплачивается отдельно по сохранённой цене. "
                                "Сохранится только этот предпросмотр; для новых правок нажмите предпросмотр ещё раз.")
        except ValueError as error:
            self.status.setText(str(error))

    def confirm(self):
        if self.editor_dirty or any(self.table.item(i, 0).checkState() == Qt.CheckState.Checked
                                    for i in range(len(self.rows))):
            self.status.setText("Есть изменения без предпросмотра. Сначала нажмите «Предпросмотр для выбранных».")
            return
        if not self.drafts:
            self.status.setText("Сначала отметьте записи и подготовьте предпросмотр.")
            return
        self.accept()


def calculate_supplements(window):
    app = sys.modules[window.__class__.__module__]
    try:
        report = window.collect_invoice_report()
        dialog = SupplementDialog(report, window)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        capabilities = app.request_expense_api("GET", "/api/customer-terms")
        if capabilities.get("customer_supplements") != 1:
            raise ValueError("Обновите сервер: сохранение точного распределения ещё не поддерживается.")
        with closing(app.get_db_connection()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for index, quote in dialog.drafts.items():
                old = dialog.rows[index]
                actual = db.execute("SELECT * FROM shifts WHERE id=?", (old["id"],)).fetchone()
                if actual is None or dict(actual) != old:
                    raise ValueError(f"№{old['id']}: смена изменилась во время расчёта. Откройте расчёт заново.")
                if db.execute("SELECT 1 FROM shift_sync_outbox WHERE sync_id=? AND operation='delete' "
                              "AND state IN ('pending','blocked')", (old["sync_id"],)).fetchone():
                    raise ValueError("Сначала дождитесь ответа на запрос удаления.")
                store_pricing(db.cursor(), "shifts", old["sync_id"], quote)
                app.enqueue_matching_shifts(db.cursor(), "id=?", (old["id"],))
        window.generate_invoice()
        window.background_sync_with_cloud()
    except (ValueError, RuntimeError, sqlite3.Error, RequestException) as error:
        QMessageBox.warning(window, "Расчёт не сохранён", str(error))
