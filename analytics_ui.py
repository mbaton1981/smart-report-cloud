"""Director-only, asynchronous presentation of the shared analytics projection."""
import json
import logging
import sqlite3
import uuid
from decimal import Decimal

from PyQt6.QtCore import QEventLoop, QObject, QRunnable, QThreadPool, QTimer, Qt, QRectF, QDate, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QBoxLayout, QGridLayout, QLabel,
    QPushButton, QComboBox, QDialog, QFrame, QLineEdit, QProgressDialog, QTableWidget,
    QTableWidgetItem, QMessageBox,
)
import desktop_theme
from date_widgets import CalendarDateEdit
from desktop_ui import ReportTable
from analytics import BILLING_LABELS, HOW, period
from expense_documents import is_service_category

logger = logging.getLogger(__name__)
CORRECTION_PATH = "/api/admin/analytics/billing-correction"
OBJECT_STATUS_COLORS = {
    "light": {"active": "#ffffff", "archived": "#e5e8eb", "service": "#fce8e8"},
    "dark": {"active": "#28303d", "archived": "#373b42", "service": "#482e36"},
}


def number(value):
    return f"{value:,.2f}".replace(",", " ")


def billing_summary(counts):
    if not counts:
        return "—"
    parts = counts["expenses"]
    if parts % 10 == 1 and parts % 100 != 11:
        unit = "доля расхода"
    elif 2 <= parts % 10 <= 4 and not 12 <= parts % 100 <= 14:
        unit = "доли расходов"
    else:
        unit = "долей расходов"
    return f"{counts['hours']:g} ч · {parts} {unit}"


def open_expense_breakdown(parent, documents, total, *, exact, open_document=None):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Фактуры без точной разбивки")
    dialog.resize(1080, 460)
    layout = QVBoxLayout(dialog)
    explanation = QLabel(
        f"{number(total)} SEK без НДС уже учтено в общих затратах. "
        "Это часть фактур, для которой нужно сверить суммы без НДС по объектам.\n"
        + ("" if exact else "В этом снимке доступны документы для сверки по внутреннему номеру. "
           "Суммы по каждому документу появятся после обновления серверного расчёта.")
    )
    explanation.setWordWrap(True)
    layout.addWidget(explanation)
    table = ReportTable(len(documents), 7, dialog, stretch_columns=(2,),
                        column_widths={0: 70, 1: 111, 2: 200, 3: 142, 4: 150, 5: 155, 6: 155})
    table.setHorizontalHeaderLabels(("ID", "Дата", "Поставщик", "№ фактуры", "Фактура без НДС",
                                     "Разнесено без НДС", "К сверке без НДС"))
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    for index, document in enumerate(documents):
        values = [str(document["id"]), document.get("date") or "—", document.get("supplier") or "—",
                  document.get("invoice_number") or "—"]
        values.extend(number(Decimal(document[key])) if document.get(key) is not None else "—"
                      for key in ("net_amount", "assigned_net", "remainder"))
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, document["id"])
            table.setItem(index, column, item)
    layout.addWidget(table)
    actions = QHBoxLayout()
    if open_document:
        hint = QLabel("Выберите фактуру и сверьте маркировки заказов и суммы по ним в оригинале.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        show = QPushButton("Открыть фактуру в расходах", dialog)
        show.setEnabled(False)
        table.itemSelectionChanged.connect(lambda: show.setEnabled(bool(table.selectedItems())))

        def show_selected(row):
            item = table.item(row, 0)
            if item and open_document(dialog, item.data(Qt.ItemDataRole.UserRole)):
                dialog.accept()

        show.clicked.connect(lambda: show_selected(table.currentRow()))
        table.cellDoubleClicked.connect(lambda row, _column: show_selected(row))
        actions.addWidget(show)
    actions.addStretch()
    close = QPushButton("Закрыть", dialog)
    close.clicked.connect(dialog.accept)
    actions.addWidget(close)
    layout.addLayout(actions)
    dialog.exec()


def open_billing_details(parent, entries, scope, start, end, *, correct=None):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Состав клиентских счетов")
    dialog.resize(1220, 680)
    layout = QVBoxLayout(dialog)
    context = QLabel(
        f"{scope} · {start} — {end}\n"
        "Отбор по датам смен и документов поставщиков. Состав по данным SmartReport."
    )
    context.setTextFormat(Qt.TextFormat.PlainText)
    context.setWordWrap(True)
    layout.addWidget(context)
    filters = QHBoxLayout()
    status, kind = QComboBox(), QComboBox()
    status.addItem("Все статусы", "")
    for key, label in BILLING_LABELS.items():
        status.addItem(label, key)
    for key, label in (("", "Часы и расходы"), ("shift", "Только часы"), ("expense", "Только расходы")):
        kind.addItem(label, key)
    filters.addWidget(status)
    filters.addWidget(kind)
    filters.addStretch()
    layout.addLayout(filters)
    table = ReportTable(0, 7, dialog, stretch_columns=(1, 2, 4),
                        column_widths={0: 112, 1: 220, 2: 285, 3: 142, 4: 155, 5: 145, 6: 152})
    table.setHorizontalHeaderLabels(("Дата", "Объект", "Смена / документ", "Часы / без НДС",
                                     "Категория", "Статус", "Клиентский счёт"))
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setColumnHidden(1, bool(entries) and all(entry["object_label"] == scope for entry in entries))
    layout.addWidget(table)
    count = QLabel()
    layout.addWidget(count)
    actions = QHBoxLayout()
    edit = QPushButton("Исправить расходы в счёте…", dialog)
    edit.setEnabled(False)
    edit.setToolTip("Выберите строку с номером клиентского счёта.")
    if correct is not None:
        actions.addWidget(edit)
    else:
        edit.hide()
    actions.addStretch()
    close = QPushButton("Закрыть", dialog)
    close.clicked.connect(dialog.accept)
    actions.addWidget(close)
    layout.addLayout(actions)

    def invoice_selection():
        current = table.item(table.currentRow(), 0)
        entry = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        if entry and entry["invoice_number"] and entry.get("invoice_object_id") is not None:
            return entry["invoice_object_id"], entry["invoice_number"]
        return None

    def edit_selected():
        invoice = invoice_selection()
        if invoice and correct(dialog, *invoice):
            dialog.accept()

    def fill():
        shown = [entry for entry in entries
                 if status.currentData() in ("", entry["status"]) and kind.currentData() in ("", entry["kind"])]
        table.setRowCount(len(shown))
        table.setCurrentCell(-1, -1)
        for index, entry in enumerate(shown):
            if entry["kind"] == "shift":
                description = entry["description"]
                quantity, category = f"{entry['hours']:g} ч", "Рабочая смена"
            else:
                description = f"{entry['description']} · доля {entry['share']}"
                quantity = "Нетто неизвестно" if entry["net"] is None else f"{number(entry['net'])} {entry['currency']}"
                category = entry["category"]
            invoice = f"№{entry['invoice_number']} · {entry['invoice_date']}" if entry["invoice_number"] else "—"
            values = (entry["date"], entry["object_label"], description, quantity,
                      category, BILLING_LABELS[entry["status"]], invoice)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry)
                table.setItem(index, column, item)
        table.resizeRowsToContents()
        count.setText(f"Строк в отборе: {len(shown)}" if shown else "В этом отборе записей нет.")

    status.currentIndexChanged.connect(fill)
    kind.currentIndexChanged.connect(fill)
    if correct is not None:
        table.currentCellChanged.connect(lambda *_: edit.setEnabled(invoice_selection() is not None))
        edit.clicked.connect(edit_selected)
    fill()
    dialog.exec()


def _run_correction(parent, message, callback):
    from app import ObjectOverviewTask

    loop = QEventLoop()
    result = []
    progress = QProgressDialog(message, "", 0, 0, parent)
    progress.setCancelButton(None)
    progress.setWindowModality(Qt.WindowModality.WindowModal)

    def finished(_, value, error):
        result.append((value, error))
        loop.quit()

    task = ObjectOverviewTask(0, callback)
    task.signals.completed.connect(finished)
    progress.show()
    QThreadPool.globalInstance().start(task)
    loop.exec()
    progress.close()
    value, error = result[0]
    if error:
        raise RuntimeError(error)
    return value


def edit_invoice_expenses(parent, request, object_id, invoice_number):
    params = {"object_id": object_id, "invoice_number": invoice_number}
    try:
        data = _run_correction(parent, "Загружаем расходы объекта…",
                               lambda: request("GET", CORRECTION_PATH, params=params))["correction"]
    except (KeyError, RuntimeError) as error:
        QMessageBox.warning(parent, "Состав счёта", str(error))
        return False
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"Исправить расходы · счёт №{invoice_number}")
    dialog.resize(1050, 690)
    layout = QVBoxLayout(dialog)
    context = QLabel(f"{data['object_label']} · {data['invoice_date']}\n"
                     "Галочкой отмечены расходы этого счёта. Снятый расход вернётся в очередь на выставление. "
                     "Команда меняет только состав счёта в SmartReport.")
    context.setWordWrap(True)
    context.setTextFormat(Qt.TextFormat.PlainText)
    layout.addWidget(context)
    search = QLineEdit()
    search.setPlaceholderText("Найти по поставщику, номеру или дате")
    layout.addWidget(search)
    expenses = data["expenses"]
    table = ReportTable(len(expenses), 5, dialog, stretch_columns=(2,),
                        column_widths={0: 105, 1: 114, 2: 330, 3: 82, 4: 165})
    table.setHorizontalHeaderLabels(("В счёте", "Дата", "Поставщик / документ", "Доля", "Без НДС"))
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    layout.addWidget(table)
    checks = []
    for index, entry in enumerate(expenses):
        item = QTableWidgetItem()
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if entry["included"] else Qt.CheckState.Unchecked)
        table.setItem(index, 0, item)
        checks.append(item)
        snapshot = entry["snapshot"]
        amount = (f"{number(Decimal(snapshot['net_amount']))} {snapshot['currency']}"
                  if snapshot["net_amount"] is not None else "Нетто неизвестно")
        for column, value in enumerate((entry["date"], entry["label"], str(snapshot["index"] + 1), amount), 1):
            table.setItem(index, column, QTableWidgetItem(value))

    summary = QLabel()
    layout.addWidget(summary)
    controls = QHBoxLayout()
    controls.addStretch()
    save = QPushButton("Сохранить исправление")
    cancel = QPushButton("Отмена")
    controls.addWidget(save)
    controls.addWidget(cancel)
    layout.addLayout(controls)
    previous = None

    def changes():
        removed, added = [], []
        for entry, item in zip(expenses, checks):
            marked = item.checkState() == Qt.CheckState.Checked
            if marked and not entry["included"]:
                added.append(entry["snapshot"])
            elif entry["included"] and not marked:
                removed.append(entry["snapshot"]["key"])
        return removed, added

    def update():
        removed, added = changes()
        summary.setText(f"Снять · {len(removed)}     Добавить · {len(added)}")
        save.setEnabled(bool(removed or added))

    def filter_rows(text):
        terms = text.casefold().split()
        for index, entry in enumerate(expenses):
            searchable = f"{entry['date']} {entry['label']}".casefold()
            table.setRowHidden(index, not all(term in searchable for term in terms))

    def apply():
        nonlocal previous
        removed, added = changes()
        if QMessageBox.question(
                dialog, f"Сохранить исправление счёта №{invoice_number}?",
                f"Исключить расходов: {len(removed)}. Добавить: {len(added)}. "
                "Все часы и поездки останутся в этом счёте.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        payload = {**params, "revision": data["revision"], "remove": removed, "add": added}
        if previous is None or previous[0] != payload:
            previous = payload, uuid.uuid4().hex
        try:
            response = _run_correction(
                dialog, "Сохраняем исправление…",
                lambda: request("POST", CORRECTION_PATH, json={**payload, "correction_id": previous[1]}),
            )
            if not response.get("ok"):
                raise RuntimeError("Сервер не подтвердил сохранение состава. Откройте счёт заново для проверки.")
        except RuntimeError as error:
            QMessageBox.warning(dialog, "Исправление состава", str(error))
            return
        dialog.accept()

    table.itemChanged.connect(update)
    search.textChanged.connect(filter_rows)
    save.clicked.connect(apply)
    cancel.clicked.connect(dialog.reject)
    update()
    return dialog.exec() == QDialog.DialogCode.Accepted


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
        self.setMinimumHeight(220)
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
    def __init__(self, fetch, parent=None, *, object_catalog=None, request_correction=None, reveal_expense=None):
        super().__init__(parent)
        self.fetch, self.serial, self.busy = fetch, 0, False
        self.request_correction = request_correction
        self.reveal_expense = reveal_expense
        self.object_catalog = object_catalog
        self.pending, self.last_parameters, self.fingerprint = False, None, None
        self.payload, self.task = None, None
        self._render_count = 0
        layout = QVBoxLayout(self)
        title = QLabel("Аналитика · объём работ и предварительная экономика")
        title.setWordWrap(True)
        title.setStyleSheet("font-size: 18px; font-weight: 600")
        layout.addWidget(title)
        filters = QGridLayout()
        self.preset = QComboBox()
        for text, key in (("Эта неделя", "week"), ("Прошлая неделя", "previous_week"),
                          ("Этот месяц", "month"), ("Прошлый месяц", "previous_month"),
                          ("Произвольный диапазон", "custom")):
            self.preset.addItem(text, key)
        self.preset.setCurrentIndex(2)
        self.start, self.end = CalendarDateEdit(), CalendarDateEdit()
        for field in (self.start, self.end):
            field.setDisplayFormat("yyyy-MM-dd")
            field.setCalendarPopup(True)
        self.customer, self.object = QComboBox(), QComboBox()
        self.customer.addItem("Все заказчики", "")
        self.object.addItem("Все объекты", "")
        self.customer.setMinimumWidth(0)
        self.object.setMinimumWidth(0)
        self.object.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.object.setMinimumContentsLength(18)
        self.object.view().setTextElideMode(Qt.TextElideMode.ElideNone)
        self.object.view().setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        theme = getattr(QApplication.instance(), "_smartreport_theme", None)
        if theme is not None:
            theme.changed.connect(self.update_object_colors)
        self.object.currentTextChanged.connect(self.object.setToolTip)
        self.refresh_button = QPushButton("Обновить")
        self.refresh_button.setObjectName("primaryAction")
        self.refresh_button.clicked.connect(self.refresh)
        period_row = QWidget(self)
        period_layout = QHBoxLayout(period_row)
        period_layout.setContentsMargins(0, 0, 0, 0)
        period_layout.addWidget(QLabel("Период с:", period_row))
        period_layout.addWidget(self.start)
        period_layout.addWidget(QLabel("по:", period_row))
        period_layout.addWidget(self.end)
        period_layout.addStretch(1)
        self.filter_widgets = (period_row, self.preset, self.customer, self.refresh_button)
        self.filters_layout = filters
        layout.addLayout(filters)
        self.scope = QLabel("Расчёт ещё не выполнен")
        self.scope.setWordWrap(True)
        self.status = QLabel("Только чтение · открытие вкладки не запускает синхронизацию")
        self.status.setWordWrap(True)
        layout.addWidget(self.scope)
        layout.addWidget(self.status)
        cards = QGridLayout()
        self.cards_layout = cards
        self.cards = {}
        self.card_widgets = []
        for column, (key, text) in enumerate((
                ("hours", "Рабочие часы"), ("employees", "Сотрудники"),
                ("revenue", "Оценка работ"), ("cost", "Учтённые затраты"),
                ("result", "Расчётная разница"))):
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
            help_button.setObjectName("secondaryAction")
            help_button.setToolTip(HOW[key])
            help_button.clicked.connect(lambda checked=False, k=key, t=text: QMessageBox.information(
                self, t, HOW[k]))
            contents.addWidget(help_button)
            self.cards[key] = value
            self.card_widgets.append(card)
        layout.addLayout(cards)
        self.table = ReportTable(0, 8, stretch_columns=(0, 5, 6, 7),
                                 column_widths={0: 235, 1: 74, 2: 151, 3: 142, 4: 150,
                                                5: 170, 6: 144, 7: 164})
        self.table.setHorizontalHeaderLabels(("Объект", "Часы", "Оценка работ · SEK", "Затраты · SEK",
                                             "Результат · SEK", "Полнота данных", "В счёте", "Ожидает счёта"))
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.setMinimumHeight(240)
        layout.addWidget(self.table)
        table_actions = QHBoxLayout()
        self.billing_button = QPushButton("Состав счетов…")
        self.billing_button.setEnabled(False)
        self.billing_button.setToolTip("Откройте состав выбранного объекта. Для всего отбора выберите строку «ИТОГО».")
        self.billing_button.clicked.connect(self.show_billing)
        table_actions.addWidget(self.billing_button)
        self.expense_breakdown_button = QPushButton("Фактуры без точной разбивки…")
        self.expense_breakdown_button.setEnabled(False)
        self.expense_breakdown_button.clicked.connect(self.show_expense_breakdown)
        table_actions.addWidget(self.expense_breakdown_button)
        table_actions.addStretch()
        layout.addLayout(table_actions)
        self.table.cellDoubleClicked.connect(self.show_row_details)
        self.hours_chart, self.objects_chart = Chart("hours"), Chart("objects")
        charts = QHBoxLayout()
        self.charts_layout = charts
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
        self._refresh_responsive_layout()
        self.preset.currentIndexChanged.connect(self.set_period)
        for field in (self.start, self.end):
            field.dateChanged.connect(self.custom_period)
        self.customer.currentIndexChanged.connect(self.customer_changed)
        self.object.currentIndexChanged.connect(self.changed)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_responsive_layout()

    def _refresh_responsive_layout(self):
        width = self.width()
        filter_columns = 4 if width >= 1120 else 2 if width >= 650 else 1
        while self.filters_layout.count():
            self.filters_layout.takeAt(0)
        for index, widget in enumerate(self.filter_widgets):
            self.filters_layout.addWidget(widget, index // filter_columns, index % filter_columns)
        self.filters_layout.addWidget(self.object, (len(self.filter_widgets) + filter_columns - 1) // filter_columns,
                                      0, 1, filter_columns)

        card_columns = 5 if width >= 1180 else 3 if width >= 760 else 2 if width >= 520 else 1
        while self.cards_layout.count():
            self.cards_layout.takeAt(0)
        for index, card in enumerate(self.card_widgets):
            self.cards_layout.addWidget(card, index // card_columns, index % card_columns)

        self.charts_layout.setDirection(
            QBoxLayout.Direction.LeftToRight if width >= 980
            else QBoxLayout.Direction.TopToBottom
        )

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
        self.object.setToolTip(self.object.currentText())
        for index in range(self.object.count()):
            self.object.setItemData(index, self.object.itemText(index), Qt.ItemDataRole.ToolTipRole)
        self.update_object_colors()
        self.object.blockSignals(blocked)

    def update_object_colors(self):
        if self.object_catalog is None:
            return
        try:
            catalog = {f"object:{item['id']}": item for item in self.object_catalog()}
        except sqlite3.Error:
            logger.warning("Unable to read the analytics object catalog", exc_info=True)
            return
        colors = OBJECT_STATUS_COLORS[desktop_theme.current_theme()]
        background_role = Qt.ItemDataRole.BackgroundRole
        for index in range(1, self.object.count()):
            item = catalog.get(self.object.itemData(index))
            if item is None:
                status = None
            elif is_service_category(item):
                status = "service"
            else:
                status = "active" if item.get("is_active", 1) else "archived"
            color = QColor(colors[status]) if status is not None else None
            if self.object.itemData(index, background_role) != color:
                self.object.setItemData(index, color, background_role)

    def changed(self):
        self.serial += 1
        self.billing_button.setEnabled(False)
        self.expense_breakdown_button.setEnabled(False)
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
            self.billing_button.setEnabled(False)
            self.expense_breakdown_button.setEnabled(False)
        else:
            self.payload = payload
            stable = {key: value for key, value in payload.items() if key != "calculated_at"}
            fingerprint = json.dumps(stable, sort_keys=True, ensure_ascii=False)
            if fingerprint != self.fingerprint:
                self.render(payload)
                self.fingerprint = fingerprint
            else:
                self.update_object_colors()
            self.status.setText(f"Расчёт: {payload['calculated_at']} · {payload['source']}. "
                                "Обновление только чтением; локальные изменения попадут после штатного обмена.")
            self.billing_button.setEnabled("billing" in payload)
            self.expense_breakdown_button.setEnabled(self.unresolved_row() is not None)
            if "billing" not in payload:
                self.status.setText("Сервер использует прежний расчёт аналитики. Состав счетов появится после обновления сервера.")
        if self.pending and self.isVisible():
            self.debounce.start()

    def unresolved_row(self):
        return next((row for row in (self.payload or {}).get("rows", []) if row["key"] == "unresolved_net"), None)

    def show_row_details(self, row, _column):
        if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == "unresolved_net":
            self.show_expense_breakdown()
        else:
            self.show_billing()

    def show_expense_breakdown(self):
        if not self.expense_breakdown_button.isEnabled():
            return
        data = self.payload
        exact = "unresolved_expenses" in data
        documents = data["unresolved_expenses"] if exact else [
            item for item in data["unknown_expenses"] if item["reason"] == "Часть расхода без НДС требует сверки"
        ]
        open_expense_breakdown(self, documents, self.unresolved_row()["expenses"], exact=exact,
                               open_document=self._open_expense if self.reveal_expense and self.request_correction else None)

    def _open_expense(self, parent, document_id):
        try:
            document = _run_correction(parent, "Загружаем документ…", lambda: self.request_correction(
                "GET", f"/api/expense-documents/{document_id}")["document"])
            if not isinstance(document, dict) or document.get("id") != document_id:
                raise RuntimeError("Сервер не вернул выбранную фактуру. Обновите аналитику и повторите переход.")
        except (KeyError, RuntimeError) as error:
            QMessageBox.warning(parent, "Фактура", str(error))
            return False
        self.reveal_expense(document)
        return True

    def show_billing(self):
        if not self.billing_button.isEnabled():
            return
        data = self.payload
        selected = self.table.item(self.table.currentRow(), 0)
        key = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        if key and key != "__total__":
            scope = selected.text()
            entries = [entry for entry in data["billing"] if entry["object_key"] == key]
        else:
            scope, entries = data["scope"], data["billing"]
        correction = self._correct_invoice if self.request_correction and data.get("billing_correction") else None
        open_billing_details(self, entries, scope, data["start"], data["end"], correct=correction)

    def _correct_invoice(self, parent, object_id, number):
        if edit_invoice_expenses(parent, self.request_correction, object_id, number):
            self.refresh()
            return True
        return False

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
            unresolved = row["key"] == "unresolved_net"
            label = QTableWidgetItem("Остаток фактур без разбивки по объектам" if unresolved else row["label"])
            label.setData(Qt.ItemDataRole.UserRole, row["key"])
            self.table.setItem(index, 0, label)
            for column, key in enumerate(("hours", "revenue", "cost", "result"), 1):
                value = NumericItem(row[key], row["partial"] and key != "hours" and not unresolved)
                if unresolved and key == "cost":
                    value.setToolTip(f"{number(row[key])} SEK без НДС учтено в общих затратах.")
                self.table.setItem(index, column, value)
            completeness = QTableWidgetItem("Сверить доли" if unresolved else (
                "Частичные данные" if row["partial"] else "Известные внесённые данные"))
            details = list(row["notes"])
            details.extend(f"Fast pris {f['start']} — {f['end']}: {number(f['amount'])} SEK · "
                           + ("включён" if f["included"] else "не отнесён к периоду") for f in row["fixed"])
            completeness.setToolTip("\n".join(details))
            label.setToolTip(label.text()+"\n"+"\n".join(details))
            self.table.setItem(index, 5, completeness)
            for column, status in ((6, "included"), (7, "pending")):
                counts = row.get("billing", {})
                item = QTableWidgetItem(billing_summary(counts.get(status)))
                item.setToolTip("\n".join(f"{BILLING_LABELS[key]} · {billing_summary(value)}"
                                          for key, value in counts.items()))
                self.table.setItem(index, column, item)
            extra_billing = [BILLING_LABELS[key] for key in ("legacy", "review", "changed")
                             if key in row.get("billing", {})]
            if extra_billing:
                completeness.setText(completeness.text() + "\n" + "\n".join(extra_billing))
        if data["rows"]:
            index = len(data["rows"])
            item = QTableWidgetItem("ИТОГО")
            item.setData(Qt.ItemDataRole.UserRole, "__total__")
            self.table.setItem(index, 0, item)
            for column, key in enumerate(("hours", "revenue", "cost", "result"), 1):
                self.table.setItem(index, column, NumericItem(totals[key], totals["partial"] and key != "hours"))
            self.table.setItem(index, 5, QTableWidgetItem("Частично" if totals["partial"] else "Внесённые данные"))
            for column, status in ((6, "included"), (7, "pending")):
                entries = [row["billing"][status] for row in data["rows"] if status in row.get("billing", {})]
                summed = {key: sum(entry[key] for entry in entries) for key in ("hours", "expenses")}
                self.table.setItem(index, column, QTableWidgetItem(billing_summary(summed) if entries else "—"))
        self.table.setSortingEnabled(sorting)
        for index in range(self.table.rowCount()):
            if self.table.item(index, 0).data(Qt.ItemDataRole.UserRole) == selected_key:
                blocked = self.table.blockSignals(True)
                self.table.setCurrentCell(index, 0)
                self.table.blockSignals(blocked)
                break
        self.table.verticalScrollBar().setValue(scroll)
        self.table.horizontalScrollBar().setValue(horizontal)
        self.table.resizeRowsToContents()
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
                         f"сумма документа {expense['amount'] or 'неизвестна'} {expense['currency']}.")
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
