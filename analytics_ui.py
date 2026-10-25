"""Director-only, asynchronous presentation of the shared analytics projection."""
import json
import logging

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, QRectF, QDate, pyqtSignal
from PyQt6.QtGui import QPainter, QPen, QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QComboBox,
    QDateEdit, QFrame, QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
)
from analytics import period, HOW

logger = logging.getLogger(__name__)


def number(value):
    return f"{value:,.2f}".replace(",", " ")


class ReadSignals(QObject):
    ready = pyqtSignal(int, object, str)


class ReadTask(QRunnable):
    def __init__(self, serial, fetch, parameters):
        super().__init__()
        self.serial, self.fetch, self.parameters = serial, fetch, parameters
        self.signals = ReadSignals()

    def run(self):
        try:
            result = self.fetch(self.parameters)
            if not isinstance(result, dict) or not result.get("ok") or not isinstance(result.get("analytics"), dict):
                raise ValueError("Сервер не вернул ожидаемый результат аналитики.")
        except Exception as error:
            logger.exception("Analytics read failed")
            self.signals.ready.emit(self.serial, None, str(error))
        else:
            self.signals.ready.emit(self.serial, result["analytics"], "")


class NumericItem(QTableWidgetItem):
    def __init__(self, value, partial=False):
        super().__init__(number(value))
        self.setToolTip(("Частичные данные · известная часть: " if partial else "") + number(value))
        self.setData(Qt.ItemDataRole.UserRole, value)
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other):
        return self.data(Qt.ItemDataRole.UserRole) < other.data(Qt.ItemDataRole.UserRole)


class Chart(QWidget):
    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind, self.data, self.has_data = kind, [], False
        self.setMinimumHeight(290 if kind == "hours" else 340)
        self.setMinimumWidth(300)

    def set_data(self, data, has_data):
        self.data, self.has_data = data, has_data
        self.setToolTip("\n".join(
            f"{r['label']}: {number(r['hours'])} ч" if self.kind == "hours" else
            f"{r['label']}: стоимость {number(r['revenue'])}, затраты {number(r['cost'])} SEK"
            for r in data))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink = self.palette().color(self.foregroundRole())
        painter.setPen(ink)
        area = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        painter.drawText(area, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                         "Рабочие часы · ч" if self.kind == "hours" else
                         "Известная стоимость / затраты · SEK без НДС")
        if not self.has_data or not self.data:
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "Нет исходных данных для графика")
            return
        blue, teal = QColor("#658db5"), QColor("#54a69c")
        if self.kind == "hours":
            plot = area.adjusted(56, 34, -12, -48)
            maximum = max((r["hours"] for r in self.data), default=0)
            if maximum <= 0:
                painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "Рабочих записей нет · часы не внесены")
                return
            painter.drawLine(plot.bottomLeft(), plot.topLeft())
            painter.drawLine(plot.bottomLeft(), plot.bottomRight())
            for tick in range(5):
                y = plot.bottom() - plot.height() * tick / 4
                painter.drawText(QRectF(area.left(), y-8, 50, 18), Qt.AlignmentFlag.AlignRight,
                                 f"{maximum*tick/4:g}")
            width = plot.width() / len(self.data)
            for index, item in enumerate(self.data):
                height = item["hours"] / maximum * plot.height()
                painter.fillRect(QRectF(plot.left()+index*width+1, plot.bottom()-height,
                                        max(1, width-2), height), blue)
            count = min(4, len(self.data))
            for n in range(count):
                index = round(n*(len(self.data)-1)/max(1, count-1))
                x = plot.left()+index*width
                painter.drawText(QRectF(x-38, plot.bottom()+8, 100, 28),
                                 Qt.AlignmentFlag.AlignLeft, self.data[index]["label"])
            painter.drawText(QRectF(plot.left(), area.bottom()-18, plot.width(), 18),
                             Qt.AlignmentFlag.AlignCenter, "Дата / начало недели")
        else:
            label_width = min(220, area.width()*.35)
            plot = area.adjusted(label_width, 42, -22, -36)
            values = [v for r in self.data for v in (r["revenue"], r["cost"])]
            low, high = min(0, *values), max(0, *values)
            if low == high:
                painter.drawText(area, Qt.AlignmentFlag.AlignCenter,
                                 "Рассчитанных денежных сумм нет · см. полноту данных")
                return
            def x(value):
                return plot.left() + (value-low)/(high-low)*plot.width()
            painter.setPen(QPen(ink, 1))
            painter.drawLine(int(x(0)), int(plot.top()), int(x(0)), int(plot.bottom()))
            step = plot.height()/len(self.data)
            font = painter.font()
            font.setPointSizeF(max(8, font.pointSizeF()-1))
            painter.setFont(font)
            for index, row in enumerate(self.data):
                y = plot.top()+index*step
                label = painter.fontMetrics().elidedText(row["label"], Qt.TextElideMode.ElideRight, int(label_width-10))
                painter.drawText(QRectF(area.left(), y, label_width-10, step), Qt.AlignmentFlag.AlignVCenter, label)
                for offset, key, color in ((0, "revenue", blue), (1, "cost", teal)):
                    painter.fillRect(QRectF(min(x(0), x(row[key])), y+offset*step*.35+3,
                                            abs(x(row[key])-x(0)), max(2, step*.28)), color)
            painter.drawText(QRectF(plot.left(), plot.bottom()+5, plot.width(), 18),
                             Qt.AlignmentFlag.AlignLeft, f"{low:g}")
            painter.drawText(QRectF(plot.left(), plot.bottom()+5, plot.width(), 18),
                             Qt.AlignmentFlag.AlignRight, f"{high:g} SEK")
            painter.setPen(blue)
            painter.drawText(QRectF(area.left(), 25, area.width()/2, 20), "■ Стоимость")
            painter.setPen(teal)
            painter.drawText(QRectF(area.left()+area.width()/2, 25, area.width()/2, 20), "■ Затраты")


class AnalyticsWidget(QWidget):
    def __init__(self, fetch, parent=None):
        super().__init__(parent)
        self.fetch, self.serial, self.busy = fetch, 0, False
        self.pending, self.last_parameters, self.fingerprint = False, None, None
        self.payload, self.task = None, None
        self._render_count = 0
        layout = QVBoxLayout(self)
        title = QLabel("Аналитика · объём работ и предварительная экономика")
        title.setWordWrap(True)
        title.setStyleSheet("font-size: 18px; font-weight: 600")
        layout.addWidget(title)
        filters = QHBoxLayout()
        self.preset = QComboBox()
        for text, key in (("Эта неделя", "week"), ("Прошлая неделя", "previous_week"),
                          ("Этот месяц", "month"), ("Прошлый месяц", "previous_month"),
                          ("Произвольный диапазон", "custom")):
            self.preset.addItem(text, key)
        self.preset.setCurrentIndex(2)
        self.start, self.end = QDateEdit(), QDateEdit()
        for field in (self.start, self.end):
            field.setDisplayFormat("yyyy-MM-dd")
            field.setCalendarPopup(True)
            field.setMinimumWidth(130)
        self.customer, self.object = QComboBox(), QComboBox()
        self.customer.addItem("Все заказчики", "")
        self.object.addItem("Все объекты", "")
        self.customer.setMinimumWidth(140)
        self.object.setMinimumWidth(180)
        self.refresh_button = QPushButton("Обновить")
        self.refresh_button.clicked.connect(self.refresh)
        for widget in (self.preset, self.start, self.end, self.customer, self.object, self.refresh_button):
            filters.addWidget(widget)
        layout.addLayout(filters)
        self.scope = QLabel("Расчёт ещё не выполнен")
        self.scope.setWordWrap(True)
        self.status = QLabel("Только чтение · открытие вкладки не запускает синхронизацию")
        self.status.setWordWrap(True)
        layout.addWidget(self.scope)
        layout.addWidget(self.status)
        cards = QGridLayout()
        self.cards = {}
        for column, (key, text) in enumerate((
                ("hours", "Рабочие часы"), ("employees", "Сотрудники"),
                ("revenue", "Стоимость работ"), ("cost", "Учтённые затраты"),
                ("result", "Предварительный результат"))):
            card = QFrame()
            card.setObjectName("card")
            contents = QVBoxLayout(card)
            label = QLabel(text)
            label.setWordWrap(True)
            contents.addWidget(label)
            value = QLabel("—")
            value.setWordWrap(True)
            value.setStyleSheet("font-size: 17px; font-weight: 600")
            contents.addWidget(value)
            help_button = QPushButton("Как рассчитано")
            help_button.setToolTip(HOW[key])
            help_button.clicked.connect(lambda checked=False, k=key, t=text: QMessageBox.information(
                self, t, HOW[k]))
            contents.addWidget(help_button)
            self.cards[key] = value
            cards.addWidget(card, 0, column)
        layout.addLayout(cards)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(("Объект", "Часы", "Стоимость · SEK", "Затраты · SEK",
                                             "Результат · SEK", "Полнота данных"))
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (2, 3, 4):
            self.table.setColumnWidth(column, 160)
        self.table.setColumnWidth(5, 210)
        self.table.setMinimumHeight(240)
        layout.addWidget(self.table)
        self.hours_chart, self.objects_chart = Chart("hours"), Chart("objects")
        charts = QHBoxLayout()
        charts.addWidget(self.hours_chart)
        charts.addWidget(self.objects_chart)
        layout.addLayout(charts)
        self.coverage = QLabel()
        self.coverage.setWordWrap(True)
        self.coverage.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.coverage)
        self.timer = QTimer(self)
        self.timer.setInterval(15000)
        self.timer.timeout.connect(self.auto_refresh)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(200)
        self.debounce.timeout.connect(self.refresh)
        self.set_period()
        self.preset.currentIndexChanged.connect(self.set_period)
        for field in (self.start, self.end):
            field.dateChanged.connect(self.custom_period)
        self.customer.currentIndexChanged.connect(self.customer_changed)
        self.object.currentIndexChanged.connect(self.changed)

    def set_period(self):
        if self.preset.currentData() != "custom":
            start, end = period(self.preset.currentData())
            for field, value in ((self.start, start), (self.end, end)):
                blocked = field.blockSignals(True)
                field.setDate(QDate(value.year, value.month, value.day))
                field.blockSignals(blocked)
        self.changed()

    def custom_period(self):
        blocked = self.preset.blockSignals(True)
        self.preset.setCurrentIndex(self.preset.findData("custom"))
        self.preset.blockSignals(blocked)
        self.changed()

    def customer_changed(self):
        self.update_objects(preserve=False)
        self.changed()

    def update_objects(self, preserve=True):
        selected = self.object.currentData()
        text = self.object.currentText()
        blocked = self.object.blockSignals(True)
        self.object.clear()
        self.object.addItem("Все объекты", "")
        for obj in (self.payload or {}).get("objects", []):
            if not self.customer.currentData() or obj["company"] == self.customer.currentData():
                self.object.addItem(obj["label"], obj["key"])
        index = self.object.findData(selected)
        if selected and index < 0 and preserve:
            self.object.addItem(text, selected)
            index = self.object.count()-1
        self.object.setCurrentIndex(max(0, index))
        self.object.blockSignals(blocked)

    def changed(self):
        self.serial += 1
        self.status.setText("Отбор изменён · ожидается расчёт. Ниже пока предыдущий результат.")
        if self.isVisible():
            self.debounce.start()

    def parameters(self):
        return dict(start=self.start.date().toString("yyyy-MM-dd"), end=self.end.date().toString("yyyy-MM-dd"),
                    company=self.customer.currentData() or "", object_key=self.object.currentData() or "")

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()
        self.debounce.start()

    def hideEvent(self, event):
        self.timer.stop()
        self.debounce.stop()
        super().hideEvent(event)

    def auto_refresh(self):
        if self.isVisible():
            self.refresh(background=True)

    def refresh(self, background=False):
        if self.start.date() > self.end.date():
            self.status.setText("Ошибка отбора: начало периода позже окончания.")
            return
        if self.busy:
            self.pending = True
            return
        self.serial += 1
        self.busy, self.pending = True, False
        self.last_parameters = self.parameters()
        self.status.setText("Расчёт в фоне… Предыдущие данные сохраняются до ответа.")
        self.task = ReadTask(self.serial, self.fetch, dict(self.last_parameters))
        self.task.signals.ready.connect(self.received)
        QThreadPool.globalInstance().start(self.task)

    def received(self, serial, payload, error):
        self.busy = False
        self.task = None
        if serial != self.serial or self.last_parameters != self.parameters():
            if self.isVisible():
                self.debounce.start()
            return
        if error:
            self.status.setText(f"Не удалось обновить: {error}\nПредыдущие данные не подтверждены как актуальные.")
        else:
            self.payload = payload
            stable = {key: value for key, value in payload.items() if key != "calculated_at"}
            fingerprint = json.dumps(stable, sort_keys=True, ensure_ascii=False)
            if fingerprint != self.fingerprint:
                self.render(payload)
                self.fingerprint = fingerprint
            self.status.setText(f"Расчёт: {payload['calculated_at']} · {payload['source']}. "
                                "Обновление только чтением; локальные изменения попадут после штатного обмена.")
        if self.pending and self.isVisible():
            self.debounce.start()

    def render(self, data):
        self._render_count += 1
        totals = data["totals"]
        self.scope.setText(f"{data['start']} — {data['end']} · {data['scope']}\n"
                           + ("Текущий период предварительный; данные могут быть внесены позже. "
                              if data["preliminary"] else "")
                           + "Управленческая оценка, не бухгалтерский отчёт. Денежные суммы — SEK без НДС.")
        for key, label in self.cards.items():
            if not totals["has_data"]:
                label.setText("Нет данных")
            elif key == "employees":
                label.setText(str(totals[key]))
            elif key == "hours":
                label.setText(f"{number(totals[key])} ч")
            else:
                prefix = "Частично: " if totals.get(key+"_partial", totals["partial"]) else ""
                label.setText(f"{prefix}{number(totals[key])} SEK")
        selected = self.table.currentRow()
        selected_key = self.table.item(selected, 0).data(Qt.ItemDataRole.UserRole) if selected >= 0 else None
        scroll = self.table.verticalScrollBar().value()
        horizontal = self.table.horizontalScrollBar().value()
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(data["rows"])+int(bool(data["rows"])))
        for index, row in enumerate(data["rows"]):
            label = QTableWidgetItem(row["label"])
            label.setData(Qt.ItemDataRole.UserRole, row["key"])
            self.table.setItem(index, 0, label)
            for column, key in enumerate(("hours", "revenue", "cost", "result"), 1):
                self.table.setItem(index, column, NumericItem(row[key], row["partial"] and key != "hours"))
            completeness = QTableWidgetItem("Частичные данные" if row["partial"] else "Известные внесённые данные")
            details = list(row["notes"])
            details.extend(f"Fast pris {f['start']} — {f['end']}: {number(f['amount'])} SEK · "
                           + ("включён" if f["included"] else "не отнесён к периоду") for f in row["fixed"])
            completeness.setToolTip("\n".join(details))
            label.setToolTip(row["label"]+"\n"+"\n".join(details))
            self.table.setItem(index, 5, completeness)
        if data["rows"]:
            index = len(data["rows"])
            item = QTableWidgetItem("ИТОГО")
            item.setData(Qt.ItemDataRole.UserRole, "__total__")
            self.table.setItem(index, 0, item)
            for column, key in enumerate(("hours", "revenue", "cost", "result"), 1):
                self.table.setItem(index, column, NumericItem(totals[key], totals["partial"] and key != "hours"))
            self.table.setItem(index, 5, QTableWidgetItem("Частично" if totals["partial"] else "Внесённые данные"))
        self.table.setSortingEnabled(sorting)
        for index in range(self.table.rowCount()):
            if self.table.item(index, 0).data(Qt.ItemDataRole.UserRole) == selected_key:
                blocked = self.table.blockSignals(True)
                self.table.setCurrentCell(index, 0)
                self.table.blockSignals(blocked)
                break
        self.table.verticalScrollBar().setValue(scroll)
        self.table.horizontalScrollBar().setValue(horizontal)
        self.hours_chart.set_data(data["hours_chart"], totals["has_data"])
        self.objects_chart.set_data(data["objects_chart"], totals["has_data"])
        lines = [
            "Что учтено",
            f"Клиентская стоимость рассчитана для {number(totals['known_hours'])} из {number(totals['hours'])} ч; "
            f"неполных рабочих записей: {totals['unknown_records']}.",
            f"Зарплата: {number(totals['wages'])} SEK известной части; сохранённых периодов начисления: "
            f"{totals['payroll_snapshots']}; записей с оценкой по текущей ставке: {totals['estimated_wage_records']}.",
            f"Расходы: {number(totals['expenses'])} SEK без НДС; подтверждённых документов: {totals['expense_documents']}.",
            "При частичных данных результат — только разность известных сумм, НЕ прибыль.",
            *data["notes"],
        ]
        for row in data["rows"]:
            if row["notes"]:
                lines.append(f"• {row['label']}: " + "; ".join(row["notes"]))
            for fixed in row["fixed"]:
                if not fixed["included"]:
                    lines.append(f"• Fast pris {row['label']}: {number(fixed['amount'])} SEK за "
                                 f"{fixed['start']} — {fixed['end']} отдельно; часы включены, сумма не распределена.")
        for expense in data["unknown_expenses"]:
            lines.append(f"• Документ №{expense['id']}: {expense['reason']}; "
                         f"{expense['amount'] or 'сумма неизвестна'} {expense['currency']} — отдельно, не в итоге.")
        self.coverage.setText("\n".join(lines))
        current = self.customer.currentData()
        if [self.customer.itemData(i) for i in range(1, self.customer.count())] != data["companies"]:
            blocked = self.customer.blockSignals(True)
            self.customer.clear()
            self.customer.addItem("Все заказчики", "")
            for company in data["companies"]:
                self.customer.addItem(company, company)
            if current and self.customer.findData(current) < 0:
                self.customer.addItem(current, current)
            self.customer.setCurrentIndex(max(0, self.customer.findData(current)))
            self.customer.blockSignals(blocked)
        self.update_objects()
