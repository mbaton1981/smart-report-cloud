"""Existing-employee cards and an explicitly authorized accountant PDF."""
from datetime import date
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from PyQt6.QtCore import Qt, QSaveFile, QIODevice
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QInputDialog,
    QTabWidget, QVBoxLayout, QWidget,
)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether

from employee_cards import BANK_FIELDS, CardError, FIELDS, missing_fields, validate_card, warnings
from employee_cards_client import CardClient
from payroll_preview import PayrollPdfPreviewDialog


class EmployeeCardDialog(QDialog):
    def __init__(self, client, record, local_id=None, parent=None):
        super().__init__(parent)
        self.client, self.record, self.local_id = client, record, local_id
        self.setWindowTitle("Карточка сотрудника")
        self.setObjectName("employee_card_dialog")
        self.resize(760, 720)
        layout = QVBoxLayout(self)
        identity_label = QLabel(f"ID: {record['id']} · {record['name']}")
        identity_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(identity_label)
        layout.addWidget(QLabel("ФИО и ставка — из существующего справочника. Переименование здесь отключено,\n"
                               "чтобы сохранить старые связи. Реквизиты не влияют на расчёты и выплаты."))
        tabs = QTabWidget()
        layout.addWidget(tabs)
        self.fields = {}
        sections = {
            "Основные сведения": ["start_date", "end_date"],
            "Персональные данные": ["identifier_type", "identifier", "address", "postal_code", "city", "country"],
            "Контакты": ["phone", "email"],
            "Банк": ["account_type", "bank_name", "account_holder", "clearing", "account_number", "iban", "bic", "verified_date"],
        }
        self.active = QComboBox()
        self.active.addItem("Работает", True)
        self.active.addItem("Уволен", False)
        self.active.setCurrentIndex(0 if record["is_active"] else 1)
        for section, keys in sections.items():
            page = QWidget()
            form = QFormLayout(page)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            if section == "Основные сведения":
                name = QLineEdit(record["name"])
                name.setReadOnly(True)
                form.addRow("ФИО", name)
                salary = QLineEdit("Не задана" if record["salary_rate"] is None else f"{record['salary_rate']:.2f} SEK/ч")
                salary.setReadOnly(True)
                form.addRow("Текущая зарплатная ставка", salary)
                form.addRow("Статус", self.active)
            for key in keys:
                if key in {"account_type", "identifier_type"}:
                    widget = QComboBox()
                    choices = ([("Не указан", ""), ("Personnummer", "personnummer"), ("Samordningsnummer", "samordningsnummer")]
                               if key == "identifier_type" else [("Шведский счёт", "swedish"), ("Иностранный счёт (IBAN/BIC)", "foreign")])
                    for text, value in choices:
                        widget.addItem(text, value)
                    widget.setCurrentIndex(widget.findData(record["card"][key]))
                else:
                    widget = QLineEdit(record["card"][key])
                    widget.setMaxLength(500 if key == "address" else 200)
                    if key.endswith("_date"):
                        widget.setPlaceholderText("ГГГГ-ММ-ДД или пусто")
                widget.setObjectName("employee_card_" + key)
                self.fields[key] = widget
                form.addRow(FIELDS[key], widget)
            if section == "Банк":
                self.confirm_button = QPushButton("Подтвердить актуальность сегодня")
                self.confirm_button.setToolTip("Только после проверки реквизитов у сотрудника. Изменённый счёт сначала сохраните.")
                self.confirm_button.clicked.connect(self.confirm_today)
                form.addRow(self.confirm_button)
                self.bank_note = QLabel()
                self.bank_note.setWordWrap(True)
                form.addRow(self.bank_note)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            tabs.addTab(scroll, section)
        self.state_note = QLabel()
        self.state_note.setWordWrap(True)
        layout.addWidget(self.state_note)
        audit_button = QPushButton("История изменений реквизитов")
        audit_button.clicked.connect(self.show_audit)
        layout.addWidget(audit_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for widget in self.fields.values():
            if isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self.update_notes)
            else:
                widget.textChanged.connect(self.update_notes)
        self.active.currentIndexChanged.connect(self.update_notes)
        self.update_notes()

    def values(self):
        return {key: widget.currentData() if isinstance(widget, QComboBox) else widget.text().strip()
                for key, widget in self.fields.items()}

    def dirty(self):
        return self.values() != self.record["card"] or self.active.currentData() != self.record["is_active"]

    def update_notes(self):
        card = self.values()
        bank_changed = any(card[key] != self.record["card"][key] for key in BANK_FIELDS)
        self.confirm_button.setEnabled(not bank_changed)
        self.fields["verified_date"].setEnabled(not bank_changed)
        self.bank_note.setText(
            "Счёт изменён: подтверждение будет снято. Сохраните, затем заново подтвердите у сотрудника."
            if bank_changed else "Подтверждение — только актуальность реквизитов, не выплата."
        )
        self.state_note.setText("Не хватает данных: " + ", ".join(missing_fields(card))
                               if missing_fields(card) else "Данные заполнены")
        foreign = card["account_type"] == "foreign"
        for key in ("clearing", "account_number"):
            self.fields[key].setEnabled(not foreign)
        for key in ("iban", "bic"):
            self.fields[key].setEnabled(foreign)

    def confirm_today(self):
        if QMessageBox.question(self, "Подтверждение реквизитов",
                                "Вы проверили актуальность реквизитов непосредственно у сотрудника?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.fields["verified_date"].setText(date.today().isoformat())

    def save(self):
        try:
            card = validate_card(self.values())
            issues = warnings(card)
            if issues and QMessageBox.question(
                self, "Проверьте формат", "\n".join(issues) + "\nСохранить частично заполненную карточку?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return
            result = self.client.save(self.record, card, self.active.currentData(), self.local_id)
            self.record = result
            for key, widget in self.fields.items():
                if isinstance(widget, QComboBox):
                    widget.setCurrentIndex(widget.findData(result["card"][key]))
                else:
                    widget.setText(result["card"][key])
            self.active.setCurrentIndex(0 if result["is_active"] else 1)
            self.update_notes()
            message = "Карточка сохранена. Зарплата и «Баланс» не изменены."
            if result.get("bank_confirmation_reset"):
                message += "\nСчёт изменён: подтверждение снято. Проверьте реквизиты у сотрудника и подтвердите повторно."
            QMessageBox.information(self, "Карточка сохранена", message)
        except CardError as error:
            QMessageBox.warning(self, "Карточка не сохранена", str(error))

    def show_audit(self):
        try:
            changes = self.client.audit(self.record["id"])
            dialog = QDialog(self)
            dialog.setWindowTitle("История изменений — без значений реквизитов")
            dialog.resize(850, 450)
            layout = QVBoxLayout(dialog)
            table = QTableWidget(len(changes), 4)
            table.setHorizontalHeaderLabels(["Когда", "Кто", "Версия", "Какие поля"])
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            for index, change in enumerate(changes):
                for column, text in enumerate((change["date"], change["actor"], str(change["revision"]), ", ".join(change["fields"]))):
                    table.setItem(index, column, QTableWidgetItem(text))
            table.resizeColumnsToContents()
            table.horizontalHeader().setStretchLastSection(True)
            layout.addWidget(table)
            dialog.exec()
        except CardError as error:
            QMessageBox.warning(self, "История недоступна", str(error))

    def can_close(self):
        return not self.dirty() or QMessageBox.question(
            self, "Несохранённые изменения", "Закрыть карточку без сохранения изменений?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes

    def reject(self):
        if self.can_close():
            super().reject()

    def closeEvent(self, event):
        if self.can_close():
            event.accept()
        else:
            event.ignore()


def selected_local_ids(window):
    table = window.table_employees
    return [table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            for row in sorted({index.row() for index in table.selectionModel().selectedRows()})]


def open_employee_card(window):
    try:
        selected = selected_local_ids(window)
        if len(selected) != 1:
            raise CardError("Выберите одного сотрудника в справочнике.")
        import app
        client = CardClient(app.get_db_connection)
        record = client.resolve(selected[0])
        EmployeeCardDialog(client, record, selected[0], window).exec()
        window.load_employees_table()
    except CardError as error:
        QMessageBox.warning(window, "Карточка недоступна", str(error))


def accountant_pdf(records, font, bold):
    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=landscape(A4), leftMargin=24, rightMargin=24,
                            topMargin=24, bottomMargin=24, title="Employee details", author="SmartReport")
    style = ParagraphStyle("EmployeeDetails", fontName=font, fontSize=9, leading=12, splitLongWords=False)
    heading = ParagraphStyle("EmployeeHeading", parent=style, fontName=bold, fontSize=12, leading=15)
    story = [Paragraph("Данные сотрудников для бухгалтера — конфиденциально", heading), Spacer(1, 8)]
    for record in records:
        card = record["card"]
        def text(key):
            return escape(card[key]) if card[key] else "— не указано"
        bank_keys = ["bank_name", "account_holder"] + (["iban", "bic"] if card["account_type"] == "foreign" else ["clearing", "account_number"])
        bank_keys += ["verified_date"]
        rows = [
            [Paragraph(escape(record["name"]), heading),
             Paragraph("Работает" if record["is_active"] else "Уволен", style)],
            [Paragraph("Персональный идентификатор", style),
             Paragraph(escape(card["identifier_type"] or "Тип не указан") + ": " + text("identifier"), style)],
            [Paragraph("Адрес", style), Paragraph(" · ".join(text(key) for key in ("address", "postal_code", "city", "country")), style)],
            [Paragraph("Контакты", style), Paragraph(text("phone") + " · " + text("email"), style)],
        ] + [[Paragraph(FIELDS[key], style), Paragraph(text(key), style)] for key in bank_keys]
        issues = missing_fields(card) + warnings(card)
        if issues:
            rows.append([Paragraph("Требует уточнения", style), Paragraph(escape("; ".join(issues)), style)])
        table = Table(rows, colWidths=[180, landscape(A4)[0] - 228], hAlign="LEFT")
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (0, 0), (-1, -1), .4, colors.grey),
            ("LINEBELOW", (0, 0), (-1, 0), .4, colors.grey),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.extend([KeepTogether([table]), Spacer(1, 10)])
    doc.build(story)
    return output.getvalue()


class ConfidentialPreview(PayrollPdfPreviewDialog):
    def __init__(self, pdf, client, parent):
        self.client = client
        self.client.authorize()
        super().__init__(pdf, Path.home() / "employee-data.pdf", parent)
        self.setWindowTitle("Данные для бухгалтера — конфиденциальный PDF")

    def save_as(self):
        try:
            self.client.authorize()
            path, _ = QFileDialog.getSaveFileName(self, "Сохранить конфиденциальный PDF", str(self.suggested_path), "PDF (*.pdf)")
            if not path:
                return
            self.client.authorize()
            if QMessageBox.question(
                self, "Защита передаваемых данных",
                "PDF содержит персональные и банковские данные и будет сохранён без пароля.\n"
                "Сохранить в выбранное защищённое место?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No
            ) != QMessageBox.StandardButton.Yes:
                return
            target = Path(path)
            if not target.suffix:
                target = target.with_suffix(".pdf")
            file = QSaveFile(str(target))
            if not file.open(QIODevice.OpenModeFlag.WriteOnly):
                raise CardError("Не удалось открыть выбранное место для сохранения.")
            if file.write(self._source_bytes) != len(self._source_bytes):
                file.cancelWriting()
                raise CardError("Не удалось записать PDF.")
            if not file.commit():
                raise CardError("Не удалось завершить сохранение PDF.")
            QMessageBox.information(self, "PDF сохранён", "Конфиденциальный PDF сохранён в выбранном месте.")
        except CardError as error:
            QMessageBox.warning(self, "PDF не сохранён", str(error))

    def print_pdf(self):
        try:
            self.client.authorize()
            super().print_pdf()
        except CardError as error:
            QMessageBox.warning(self, "Печать запрещена", str(error))

    def print_document(self, printer):
        self.client.authorize()
        super().print_document(printer)


def export_records(client, ids, parent):
    import app
    records = [client.get(identifier) for identifier in dict.fromkeys(ids)]
    if not records:
        raise CardError("Нет сотрудников для передачи бухгалтеру.")
    issues = [(row["name"], row["missing"] + row["warnings"]) for row in records if row["missing"] or row["warnings"]]
    if issues and QMessageBox.question(
        parent, "Не хватает данных",
        "\n".join(name + ": " + ", ".join(missing) for name, missing in issues)
        + "\nОткрыть PDF с явными пропусками?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No
    ) != QMessageBox.StandardButton.Yes:
        return
    pdf = accountant_pdf(records, app.DEFAULT_FONT, app.DEFAULT_FONT_BOLD)
    try:
        preview = ConfidentialPreview(pdf, client, parent)
    except (ImportError, OSError, ValueError, RuntimeError):
        raise CardError("Защищённый встроенный PDF-просмотр недоступен. "
                        "Временный файл с реквизитами не создавался. Проверьте установку QtPdf.") from None
    try:
        preview.exec()
    finally:
        preview.release_resources()


def accountant_export(window):
    try:
        import app
        client = CardClient(app.get_db_connection)
        mode, ok = QInputDialog.getItem(
            window, "Данные для бухгалтера", "Состав отчёта:",
            ["Конкретный сотрудник", "Выбранные сотрудники", "Все действующие сотрудники"], 0, False)
        if not ok:
            return
        if mode == "Все действующие сотрудники":
            ids = [row["id"] for row in client.employees() if row["is_active"]]
        elif mode == "Выбранные сотрудники":
            ids = [client.resolve(local_id)["id"] for local_id in selected_local_ids(window)]
        else:
            employees = client.employees()
            names = [f"{row['name']} · ID {row['id']}" for row in employees]
            selected, ok = QInputDialog.getItem(
                window, "Сотрудник", "Выберите сотрудника:", names, 0, False)
            if not ok:
                return
            ids = [employees[names.index(selected)]["id"]]
        export_records(client, ids, window)
    except CardError as error:
        QMessageBox.warning(window, "Отчёт недоступен", str(error))


def accountant_access(window):
    try:
        import app
        client = CardClient(app.get_db_connection)
        accounts = client.request("GET", "/accountants")["accountants"]
        action, ok = QInputDialog.getItem(window, "Доступ бухгалтера", "Действие:",
                                         ["Создать отдельную учётную запись", "Отозвать доступ"], 0, False)
        if not ok:
            return
        if action == "Отозвать доступ":
            active = [account for account in accounts if account["is_active"]]
            if not active:
                raise CardError("Нет активных учётных записей бухгалтера.")
            names = [account["username"] for account in active]
            name, ok = QInputDialog.getItem(window, "Отзыв доступа", "Учётная запись:", names, 0, False)
            if ok and QMessageBox.question(window, "Отзыв доступа", "Отозвать доступ и все токены выбранного бухгалтера?",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                client.request("DELETE", f"/accountants/{active[names.index(name)]['id']}")
                QMessageBox.information(window, "Доступ отозван", "Вход бухгалтера и его токены отключены.")
        else:
            name, ok = QInputDialog.getText(window, "Логин бухгалтера", "Новый отдельный логин:")
            if not ok:
                return
            password, ok = QInputDialog.getText(window, "Пароль бухгалтера", "Не менее 12 символов:",
                                              QLineEdit.EchoMode.Password)
            if not ok:
                return
            if not name.strip() or not 12 <= len(password) <= 200:
                raise CardError("Укажите непустой отдельный логин и пароль от 12 до 200 символов.")
            client.request("POST", "/accountants", {"username": name, "password": password})
            QMessageBox.information(window, "Доступ создан",
                                    "Создана отдельная ограниченная учётная запись. Передайте пароль защищённым способом.")
    except CardError as error:
        QMessageBox.warning(window, "Доступ не изменён", str(error))


class AccountantDirectory(QDialog):
    """Accountant login never constructs the unrestricted financial desktop window."""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SmartReport — сотрудники для бухгалтера")
        self.resize(950, 600)
        self.client = CardClient()
        self.records = []
        layout = QVBoxLayout(self)
        from desktop_theme import header
        layout.addWidget(header(self))
        layout.addWidget(QLabel("Доступ только к карточкам сотрудников. Смены, зарплатные расчёты и «Баланс» недоступны."))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["ФИО", "Статус", "Данные"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)
        for label, callback in (("Обновить", self.refresh), ("Открыть карточку", self.open_card),
                                ("Выбранные — PDF", self.export_selected), ("Все действующие — PDF", self.export_all)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            layout.addWidget(button)
        self.refresh()

    def refresh(self):
        try:
            self.records = self.client.employees()
            self.table.setRowCount(len(self.records))
            for index, row in enumerate(self.records):
                issues = row["missing"] + row["warnings"]
                for column, text in enumerate((row["name"], "Работает" if row["is_active"] else "Уволен",
                                               "Не хватает данных" if issues else "Данные заполнены")):
                    item = QTableWidgetItem(text)
                    if column == 2:
                        item.setToolTip("\n".join(issues))
                    self.table.setItem(index, column, item)
            self.table.resizeColumnsToContents()
        except CardError as error:
            self.table.setRowCount(0)
            QMessageBox.warning(self, "Карточки недоступны", str(error))

    def chosen(self):
        return [self.records[index.row()]["id"] for index in self.table.selectionModel().selectedRows()]

    def open_card(self):
        try:
            ids = self.chosen()
            if len(ids) != 1:
                raise CardError("Выберите одного сотрудника.")
            EmployeeCardDialog(self.client, self.client.get(ids[0]), parent=self).exec()
            self.refresh()
        except CardError as error:
            QMessageBox.warning(self, "Карточка недоступна", str(error))

    def export_selected(self):
        self.export(self.chosen())

    def export_all(self):
        try:
            self.export([row["id"] for row in self.client.employees() if row["is_active"]])
        except CardError as error:
            QMessageBox.warning(self, "Отчёт недоступен", str(error))

    def export(self, ids):
        try:
            export_records(self.client, ids, self)
        except CardError as error:
            QMessageBox.warning(self, "Отчёт недоступен", str(error))
