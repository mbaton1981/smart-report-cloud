"""Desktop editors for factual time and customer contract versions."""
import json
from datetime import date

from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QHeaderView,
)

from customer_intervals import DAYS, MODES, terms, time_facts

DAY_NAMES = {"weekday": "Будни", "saturday": "Суббота", "sunday": "Воскресенье", "holiday": "Праздник"}
MODE_NAMES = {"normal": "Обычная", "overtime": "Сверхурочная"}


class TimeFactsEditor(QWidget):
    def __init__(self, value=None, parent=None):
        super().__init__(parent)
        value = json.loads(value) if isinstance(value, str) and value else value or {}
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        self.start = QLineEdit(value.get("start", ""))
        self.end = QLineEdit(value.get("end", ""))
        self.start.setPlaceholderText("16:00")
        self.end.setPlaceholderText("20:00")
        self.next_day = QCheckBox("Окончание на следующий день")
        self.next_day.setChecked(value.get("next_day", False))
        form.addRow("Начало, HH:MM:", self.start)
        form.addRow("Конец, HH:MM:", self.end)
        form.addRow(self.next_day)
        note = QLabel("Обычная работа — по умолчанию, в том числе вечером. "
                      "Укажите только сверхурочные части. Перерывы не оплачиваются. "
                      "Для следующего дня укажите фактические часы; положение внутри смены учитывается. "
                      "Клиентский режим не меняет зарплатные правила OB.")
        note.setWordWrap(True)
        form.addRow(note)
        self.tables = {}
        for key, label in (("breaks", "Неоплачиваемые перерывы"), ("overtime", "Сверхурочные части")):
            table = QTableWidget(0, 2)
            table.setHorizontalHeaderLabels(["Начало HH:MM", "Конец HH:MM"])
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.setMaximumHeight(115)
            for entry in value.get(key, []):
                row = table.rowCount()
                table.insertRow(row)
                for column, field in enumerate(("start", "end")):
                    table.setItem(row, column, QTableWidgetItem(entry[field]))
            self.tables[key] = table
            form.addRow(label, table)
            buttons = QHBoxLayout()
            add = QPushButton("Добавить интервал")
            remove = QPushButton("Убрать выбранный")
            add.clicked.connect(lambda _=False, t=table: t.insertRow(t.rowCount()))
            remove.clicked.connect(lambda _=False, t=table: t.removeRow(t.currentRow()))
            buttons.addWidget(add)
            buttons.addWidget(remove)
            form.addRow(buttons)

    def value(self, work_date, expected_hours=None):
        value = {"version": 1, "start": self.start.text().strip(), "end": self.end.text().strip(),
                 "next_day": self.next_day.isChecked()}
        for key, table in self.tables.items():
            value[key] = [{
                name: table.item(row, column).text().strip() if table.item(row, column) else ""
                for column, name in enumerate(("start", "end"))
            } for row in range(table.rowCount())]
        return time_facts(value, work_date, expected_hours)


class CustomerTermsDialog(QDialog):
    def __init__(self, obj, versions, objects, load_versions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Клиентские доплаты — " + obj["name"])
        self.resize(1000, 740)
        layout = QVBoxLayout(self)
        note = QLabel(
            f"{obj['name']} ({obj.get('markning') or 'без маркировки'})\n"
            f"База объекта: {obj['rate']} SEK/ч без НДС — из существующей карточки цен. "
            "Чтобы изменить базу, измените «Ставка, SEK/ч» в карточке объекта.\n"
            "Одна версия условий применяется с указанной даты. Новая версия не меняет старые записи. "
            "Пустой % или отсутствующий интервал = условия не заданы. 0% = подтверждено без доплаты.\n"
            "Для каждого участка применяется одно правило: праздник → суббота/воскресенье → будни, "
            "внутри дня — явно указанный режим. Доплаты не складываются. Порог 40 ч не используется."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.effective = QLineEdit(date.today().isoformat())
        form.addRow("Действует с (YYYY-MM-DD):", self.effective)
        self.holidays = QLineEdit()
        self.holidays.setPlaceholderText("Явные даты YYYY-MM-DD через запятую; правила праздника вводятся отдельно")
        form.addRow("Праздники:", self.holidays)
        copy_row = QHBoxLayout()
        self.source = QComboBox()
        for item in objects:
            self.source.addItem(f"{item['name']} ({item.get('markning') or 'без кода'}) · {item['company']}", item)
        copy_button = QPushButton("Скопировать условия объекта")
        def copy_terms():
            try:
                available = load_versions(self.source.currentData())
                if not available:
                    raise ValueError("У выбранного объекта клиентские условия не заданы.")
                self.populate(json.loads(available[-1]["conditions_json"]), keep_date=True)
            except (ValueError, RuntimeError) as error:
                QMessageBox.warning(self, "Копирование", str(error))
        copy_button.clicked.connect(copy_terms)
        copy_row.addWidget(self.source, 1)
        copy_row.addWidget(copy_button)
        form.addRow(copy_row)
        layout.addLayout(form)
        self.table = QTableWidget(0, 5)
        self.table.setObjectName("customerTermsRules")
        self.table.setHorizontalHeaderLabels(["День", "Режим", "Начало HH:MM", "Конец HH:MM", "Доплата %"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        add, remove = QPushButton("Добавить правило"), QPushButton("Убрать выбранное")
        add.clicked.connect(lambda: self.add_rule())
        remove.clicked.connect(lambda: self.table.removeRow(self.table.currentRow()))
        row.addWidget(add)
        row.addWidget(remove)
        layout.addLayout(row)
        self.example = QLabel()
        self.example.setObjectName("customerTermsExample")
        self.example.setWordWrap(True)
        self.table.itemChanged.connect(lambda: self.show_example(obj["rate"]))
        self.table.itemSelectionChanged.connect(lambda: self.show_example(obj["rate"]))
        layout.addWidget(self.example)
        history = QLabel("История: " + ("; ".join(
            f"v{v['revision']} с {json.loads(v['conditions_json'])['effective_from']}" for v in versions
        ) or "условия не заданы"))
        history.setWordWrap(True)
        layout.addWidget(history)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить новую версию")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if versions:
            self.populate(json.loads(versions[-1]["conditions_json"]))
        self.show_example(obj["rate"])

    def add_rule(self, rule=None):
        rule = rule or {"day": "weekday", "mode": "normal", "start": "", "end": "", "percent": None}
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, (key, values, names) in enumerate((
            ("day", DAYS, DAY_NAMES), ("mode", MODES, MODE_NAMES),
        )):
            combo = QComboBox()
            for value in values:
                combo.addItem(names[value], value)
            combo.setCurrentIndex(combo.findData(rule[key]))
            self.table.setCellWidget(row, column, combo)
        for column, key in enumerate(("start", "end", "percent"), 2):
            self.table.setItem(row, column, QTableWidgetItem(str(rule[key]) if rule[key] is not None else ""))

    def populate(self, value, keep_date=False):
        value = terms(value)
        if not keep_date:
            self.effective.setText(value["effective_from"])
        self.holidays.setText(", ".join(value["holidays"]))
        self.table.setRowCount(0)
        for rule in value["rules"]:
            self.add_rule(rule)

    def value(self):
        rules = []
        for row in range(self.table.rowCount()):
            rule = {key: self.table.cellWidget(row, column).currentData()
                    for column, key in enumerate(("day", "mode"))}
            rule.update({key: self.table.item(row, column).text().strip() if self.table.item(row, column) else ""
                         for column, key in enumerate(("start", "end", "percent"), 2)})
            rules.append(rule)
        return terms({"version": 1, "effective_from": self.effective.text().strip(),
                      "holidays": [d.strip() for d in self.holidays.text().split(",") if d.strip()], "rules": rules})

    def show_example(self, base):
        try:
            value = self.value()
            known = [r for r in value["rules"] if r["percent"] is not None]
            row = self.table.currentRow()
            rule = value["rules"][row] if row >= 0 else next(
                (r for r in known if float(r["percent"]) > 0), known[0] if known else None)
            self.example.setText(
                f"{base:g} + {float(rule['percent']):g}% = {float(base) * (1 + float(rule['percent']) / 100):g} SEK/ч без НДС"
                if rule and rule["percent"] is not None else "Доплата выбранного участка не подтверждена.")
        except ValueError as error:
            self.example.setText(str(error))

    def validate_and_accept(self):
        try:
            self.value()
        except ValueError as error:
            QMessageBox.warning(self, "Условия не сохранены", str(error))
            return
        self.accept()
