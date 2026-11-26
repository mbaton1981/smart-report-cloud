"""Company spending overview and explicit batch review in the Expenses tab."""
from datetime import date, timedelta
from calendar import monthrange
from decimal import Decimal
import math

from PyQt6.QtCore import QDate, QRectF, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog,
    QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTabBar, QVBoxLayout, QWidget)

from company_expenses import ARTICLES, OPERATIONS, REVIEW, TAX, UNKNOWN, report_shares, section, totals


def month_end(day):
    return day.replace(day=monthrange(day.year, day.month)[1])


def comparison_range(start, end, monthly):
    if monthly:
        last = start.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last if end == month_end(start) else last.replace(day=min(end.day, last.day))
    return start - (end - start) - timedelta(days=1), start - timedelta(days=1)


def money(value, currency):
    if value is None:
        return "Сумма неизвестна"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " " + (currency or "(валюта неизвестна)")


class SpendingChart(QWidget):
    chosen = pyqtSignal(str)
    colors = ("#487ca7", "#be7444", "#679384", "#976b9e", "#81954b", "#587394",
              "#ad8152", "#808895", "#548f9e", "#7b79aa", "#9099a2")

    def __init__(self):
        super().__init__()
        self.setMinimumSize(185, 120)
        self.setMaximumWidth(380)
        self.values = {}
        self.caption = ""
        self.selected = None

    def set_values(self, amounts, caption, selected):
        self.values = {article: value for article, value in amounts.items() if value > 0}
        self.caption, self.selected = caption, selected
        self.setAccessibleName("Распределение расходов · " + caption)
        self.update()

    def geometry_values(self):
        radius = min((self.width() - 16) / 2, self.height() - 16)
        return self.width()/2, (self.height()+radius)/2 - 3, radius

    def segments(self):
        total = sum(self.values.values())
        start = 180.0
        for article, amount in self.values.items():
            sweep = float(amount/total)*180
            yield article, start, sweep
            start -= sweep

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy, r = self.geometry_values()
        def ring(start, sweep):
            outer = QRectF(cx-r, cy-r, r*2, r*2)
            ir = r*.68
            inner = QRectF(cx-ir, cy-ir, ir*2, ir*2)
            path = QPainterPath()
            path.arcMoveTo(outer, start)
            path.arcTo(outer, start, -sweep)
            path.arcTo(inner, start-sweep, sweep)
            path.closeSubpath()
            return path
        painter.fillPath(ring(180, 180), self.palette().mid())
        for article, start, sweep in self.segments():
            color = QColor(self.colors[(*ARTICLES, UNKNOWN).index(article)])
            if self.selected and self.selected != article:
                color.setAlpha(130)
            painter.fillPath(ring(start, sweep), color)
        painter.setPen(self.palette().windowText().color())
        painter.drawText(QRectF(cx-r, cy-r*.48, r*2, r*.48),
                         Qt.AlignmentFlag.AlignCenter, self.caption)

    def mousePressEvent(self, event):
        cx, cy, r = self.geometry_values()
        dx, dy = event.position().x()-cx, cy-event.position().y()
        if dy >= 0 and r*.68 <= math.hypot(dx, dy) <= r:
            angle = math.degrees(math.atan2(dy, dx))
            for article, start, sweep in self.segments():
                if start-sweep <= angle <= start:
                    self.chosen.emit(article)
                    break


class ReviewTable(QTableWidget):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.columnCount() >= 5 and event.size().width() != event.oldSize().width():
            other_width = sum(self.columnWidth(i) for i in range(self.columnCount()) if i != 4)
            self.setColumnWidth(4, max(180, self.viewport().width() - other_width))

    def mouseMoveEvent(self, event):
        # Review uses individual clicks. Dragging across invoices must never select or scroll.
        event.accept()


def read_table(headers, stretch, table_type=QTableWidget):
    table = table_type(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().hide()
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    return table


class CompanyExpensesWidget(QWidget):
    def __init__(self, fetch, review, save, reveal, worker, *, open_original=None, today=date.today):
        super().__init__()
        self.fetch, self.review, self.save, self.reveal, self.worker = fetch, review, save, reveal, worker
        self.open_original = open_original
        self.today = today
        self.tasks = {}
        self.sequence = self.latest_read = 0
        self.rows = []
        self.period_key = None
        self.loaded = False
        self.first_breakdown = True
        self.updating = False
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.period = QComboBox()
        self.period.addItems(["Этот месяц", "Прошлый месяц", "Свой период"])
        controls.addWidget(self.period)
        self.start, self.end = QDateEdit(), QDateEdit()
        for label, picker in (("С", self.start), ("по", self.end)):
            picker.setCalendarPopup(True)
            picker.setDisplayFormat("yyyy-MM-dd")
            controls.addWidget(QLabel(label))
            controls.addWidget(picker)
        controls.addStretch()
        self.currency, self.basis = QComboBox(), QComboBox()
        self.basis.addItem("Без НДС", "net")
        self.basis.addItem("С НДС", "gross")
        controls.addWidget(self.currency)
        controls.addWidget(self.basis)
        refresh = QPushButton("Обновить")
        controls.addWidget(refresh)
        layout.addLayout(controls)
        self.sections = QTabBar()
        self.sections.setExpanding(False)
        for title in (OPERATIONS, TAX, REVIEW):
            self.sections.addTab(title)
        layout.addWidget(self.sections)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        chart_row = QHBoxLayout()
        self.chart = SpendingChart()
        self.articles = read_table(["Статья", "Сумма", "Доля"], 0)
        chart_row.addWidget(self.chart, 2)
        chart_row.addWidget(self.articles, 3)
        layout.addLayout(chart_row, 2)
        self.documents_title = QLabel("Документы")
        layout.addWidget(self.documents_title)
        self.documents = read_table(["Дата", "Поставщик / документ", "Статья", "Сумма части"], 1)
        layout.addWidget(self.documents, 3)
        actions = QHBoxLayout()
        self.review_button = QPushButton("Разобрать статьи…")
        self.open_button = QPushButton("Открыть в расходах")
        actions.addWidget(self.review_button)
        actions.addStretch()
        actions.addWidget(self.open_button)
        layout.addLayout(actions)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.timeout.connect(self.reload)
        self.set_period()
        self.period.currentIndexChanged.connect(self.set_period)
        for picker in (self.start, self.end):
            picker.dateChanged.connect(self.dates_changed)
        refresh.clicked.connect(self.reload)
        self.currency.currentIndexChanged.connect(self.draw)
        self.basis.currentIndexChanged.connect(self.draw)
        self.sections.currentChanged.connect(self.draw)
        self.chart.chosen.connect(self.select_article)
        self.articles.itemSelectionChanged.connect(self.update_documents)
        self.documents.itemSelectionChanged.connect(self.update_open)
        self.open_button.clicked.connect(self.open_document)
        self.documents.cellDoubleClicked.connect(self.open_document)
        self.review_button.clicked.connect(self.start_review)
        self.draw()

    def set_period(self, *_):
        self.set_month_dates()
        self.dates_changed()

    def set_month_dates(self):
        index = self.period.currentIndex()
        if index != 2:
            today = self.today()
            last = today if index == 0 else today.replace(day=1)-timedelta(days=1)
            for picker, value in ((self.start, last.replace(day=1)), (self.end, last)):
                picker.blockSignals(True)
                picker.setDate(QDate(value.year, value.month, value.day))
                picker.blockSignals(False)
        for picker in (self.start, self.end):
            picker.setEnabled(index == 2)

    def dates_changed(self, *_):
        if self.isVisible():
            self.loaded = False
            self.reset_view()
            self.debounce.start(180)

    def reset_view(self):
        self.rows = []
        self.draw()

    def run(self, work, done):
        self.sequence += 1
        token = self.sequence
        task = self.worker(token, work)
        self.tasks[token] = (task, done)
        task.signals.completed.connect(self.finished)
        QThreadPool.globalInstance().start(task)
        return token

    def finished(self, token, result, error):
        _, done = self.tasks.pop(token)
        done(token, result, error)

    def dates(self):
        return self.start.date().toPyDate(), self.end.date().toPyDate()

    def reload(self, *_):
        self.debounce.stop()
        self.set_month_dates()
        start, end = self.dates()
        self.latest_read = -1
        if start > end:
            self.loaded = False
            self.reset_view()
            self.status.setText("Начало периода позже окончания.")
            return
        first, last = comparison_range(start, end, self.period.currentIndex() != 2)
        key = (start, end, first, last)
        if self.period_key != key:
            self.loaded = False
            self.reset_view()
        self.period_key = key
        self.status.setText("Загружаем подтверждённые документы…")
        def ready(token, docs, error):
            if token != self.latest_read or self.dates() != (start, end):
                return
            if error:
                self.status.setText(("Показан предыдущий снимок. " if self.loaded else "Данные не загружены. ") + error)
                return
            self.rows = report_shares(docs)
            self.loaded = True
            currencies = sorted({r.currency for r in self.rows}, key=lambda c: (c != "SEK", not c, c))
            previous = self.currency.currentData()
            self.currency.blockSignals(True)
            self.currency.clear()
            for currency in currencies:
                self.currency.addItem(currency or "Валюта неизвестна", currency)
            self.currency.setCurrentIndex(max(0, self.currency.findData(previous)))
            self.currency.blockSignals(False)
            if self.first_breakdown and self.active_rows():
                initial_group = next((i for i, group in enumerate((OPERATIONS, TAX, REVIEW))
                                      if any(section(r.article) == group for r in self.active_rows())), 0)
                self.sections.setCurrentIndex(initial_group)
                self.first_breakdown = False
            self.draw()
            undated = {r.document["id"] for r in self.rows if not r.document.get("document_date")}
            self.status.setText("По дате документа · подтверждённые расходы." +
                                (f" Без даты вне периода: {len(undated)}." if undated else ""))
        self.latest_read = self.run(lambda: self.fetch(first.isoformat(), end.isoformat()), ready)

    def active_rows(self, previous=False):
        if not self.period_key:
            return []
        start, end = self.period_key[2:] if previous else self.period_key[:2]
        lo, hi = start.isoformat(), end.isoformat()
        return [r for r in self.rows if r.currency == self.currency.currentData()
                and lo <= (r.document.get("document_date") or "") <= hi]

    def draw(self, *_):
        group = (OPERATIONS, TAX, REVIEW)[self.sections.currentIndex()]
        current = self.active_rows()
        currency = self.currency.currentData()
        basis = self.basis.currentData()
        unreviewed = sum(r.article == UNKNOWN for r in current)
        self.sections.setTabText(2, f"К разбору ({unreviewed})" if unreviewed else REVIEW)
        self.displayed = [r for r in current if section(r.article) == group]
        amounts, missing = totals(self.displayed, basis)
        total = sum(amounts.values(), Decimal(0))
        amount_text = money(total, currency) if currency and (amounts or not missing) else "Сумма неизвестна"
        caption = amount_text if self.displayed else "Нет расходов"
        if not self.loaded:
            caption = "Нет данных"
        self.article_names = sorted(set(amounts) | set(missing), key=lambda a: (-amounts.get(a, 0), a))
        old = self.selected_article()
        self.articles.blockSignals(True)
        self.articles.setRowCount(0)
        self.articles.setRowCount(len(self.article_names))
        denominator = sum(v for v in amounts.values() if v > 0)
        for i, name in enumerate(self.article_names):
            value = amounts.get(name)
            item = QTableWidgetItem(name)
            item.setData(Qt.ItemDataRole.DecorationRole, QColor(self.chart.colors[(*ARTICLES, UNKNOWN).index(name)]))
            self.articles.setItem(i, 0, item)
            self.articles.setItem(i, 1, QTableWidgetItem(money(value, currency) if value is not None else "Неизвестно"))
            percent = f"{100*value/denominator:.1f} %" if value and value > 0 and denominator else "—"
            self.articles.setItem(i, 2, QTableWidgetItem(percent))
        self.articles.blockSignals(False)
        self.chart.set_values(amounts, caption, None)
        lines = []
        if self.loaded:
            if self.displayed:
                lines.append(("Известная сумма · " if missing else "Итого · ") + amount_text)
            else:
                lines.append("За выбранный период здесь пока нет документов.")
            if missing:
                lines.append(f"Сумма или валюта неизвестна у {sum(missing.values())} частей.")
            if any(v < 0 for v in amounts.values()):
                lines.append("Возвраты учтены в итоге. Сектора показывают положительные статьи.")
            if self.period_key and currency:
                previous = self.active_rows(True)
                prev, prev_missing = totals([r for r in previous if section(r.article) == group], basis)
                if not missing and not prev_missing:
                    diff = total - sum(prev.values())
                    lo, hi = self.period_key[2:]
                    partial = group != REVIEW and (unreviewed or any(r.article == UNKNOWN for r in previous))
                    label = "По разобранным статьям, изменение" if partial else "Изменение"
                    lines.append(f"{label} к {lo:%d.%m}–{hi:%d.%m} · " + ("+" if diff > 0 else "") + money(diff, currency))
            if group == TAX:
                lines.append("По документам. Начисления и уплата по Fortnox пока не подключены.")
            if unreviewed and group != REVIEW:
                lines.append(f"Отдельно к разбору · {unreviewed}.")
            elif group == REVIEW and unreviewed:
                lines.append("Нажмите «Разобрать статьи», чтобы проверить предложения.")
        self.summary.setText("   ".join(lines))
        self.review_button.setEnabled(bool(current) and not self.updating)
        if old in self.article_names:
            self.select_article(old)
        self.update_documents()

    def selected_article(self):
        items = self.articles.selectedItems()
        return self.articles.item(items[0].row(), 0).text() if items else None

    def select_article(self, article):
        if article in self.article_names:
            self.articles.selectRow(self.article_names.index(article))

    def update_documents(self):
        name = self.selected_article()
        self.chart.selected = name
        self.chart.update()
        self.documents_title.setText("Документы" + (" · " + name if name else ""))
        rows = [r for r in self.displayed if not name or r.article == name]
        self.documents.setRowCount(0)
        self.documents.setRowCount(len(rows))
        for index, row in enumerate(rows):
            doc = row.document
            title = f"{doc.get('supplier_name') or 'Поставщик не указан'} · №{doc.get('invoice_number') or '—'}"
            for col, text in enumerate((doc["document_date"], title, row.article,
                                        money(getattr(row, self.basis.currentData()), row.currency))):
                self.documents.setItem(index, col, QTableWidgetItem(text))
            self.documents.item(index, 0).setData(Qt.ItemDataRole.UserRole, doc)
        self.update_open()

    def update_open(self):
        self.open_button.setEnabled(bool(self.documents.selectedItems()))

    def open_document(self, *_):
        if self.documents.selectedItems():
            self.reveal(self.documents.item(self.documents.currentRow(), 0).data(Qt.ItemDataRole.UserRole))

    def start_review(self):
        rows = self.active_rows()
        if not rows or self.updating:
            return
        self.updating = True
        scope = (self.dates(), self.currency.currentData())
        self.review_button.setEnabled(False)
        self.status.setText("Читаем документы и готовим предложения по статьям…")
        def ready(_token, proposals, error):
            self.status.setText(error or "")
            if error or scope != (self.dates(), self.currency.currentData()) or not self.isVisible():
                self.updating = False
                self.draw()
                return
            dialog = ArticleReview(rows, proposals, self, self.open_original)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.updating = False
                self.draw()
                return
            changes = dialog.changes()
            self.status.setText("Сохраняем подтверждённые статьи…")
            self.run(lambda: self.save(changes), self.saved)
        self.run(lambda: self.review(rows), ready)

    def saved(self, _token, _result, error):
        self.updating = False
        if error:
            QMessageBox.warning(self, "Статьи расходов", error)
        else:
            self.first_breakdown = True
        self.reload()


class ArticleReview(QDialog):
    def __init__(self, rows, proposals, parent, open_original=None):
        super().__init__(parent)
        self.setWindowTitle("Статьи расходов фирмы")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        available = self.screen().availableGeometry()
        self.resize(min(1280, available.width() - 32), min(740, available.height() - 80))
        self.rows = rows
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Проверьте предложения. Сохранение подтвердит только отмеченные строки."))
        headers = ["Включить", "Документ", "Сумма с НДС", "Статья", "Основание"]
        if open_original is not None:
            headers.append("Оригинал")
        table = read_table(headers, 4, ReviewTable)
        table.setAutoScroll(False)
        table.setStyleSheet("QTableWidget::item:hover:!selected { background-color: palette(base); }")
        table.setRowCount(len(rows))
        layout.addWidget(table)
        self.entries = []
        for index, row in enumerate(rows):
            doc = row.document
            part_id = row.allocation.get("allocation_id")
            proposal = proposals.get(doc["id"], {}).get(part_id, {})
            selected = proposal.get("category")
            initial = selected if selected in ARTICLES else row.article
            reason = proposal.get("evidence") or row.allocation.get("company_article_evidence") or "Выберите статью вручную"
            check = QCheckBox()
            check.setChecked(initial != row.article)
            table.setCellWidget(index, 0, check)
            table.setItem(index, 1, QTableWidgetItem(f"{doc['document_date']} · {doc.get('supplier_name') or '—'}\n№{doc.get('invoice_number') or '—'}"))
            table.setItem(index, 2, QTableWidgetItem(money(row.gross, row.currency)))
            combo = QComboBox()
            combo.addItems([UNKNOWN, *ARTICLES])
            combo.setCurrentText(initial)
            table.setCellWidget(index, 3, combo)
            evidence = QTableWidgetItem(reason)
            evidence.setToolTip(reason)
            table.setItem(index, 4, evidence)
            if open_original is not None:
                view = QPushButton("Открыть")
                view.setAutoDefault(False)
                view.setEnabled(bool(doc.get("has_original")))
                view.clicked.connect(lambda _checked, document=doc: open_original(document))
                table.setCellWidget(index, 5, view)
            combo.currentTextChanged.connect(lambda value, c=check, old=row.article: c.setChecked(value != old))
            check.clicked.connect(lambda _checked, row_index=index: table.setCurrentCell(row_index, 0))
            combo.currentIndexChanged.connect(lambda _choice, row_index=index: table.setCurrentCell(row_index, 3))
            combo.activated.connect(lambda _choice, row_index=index: table.setCurrentCell(row_index, 3))
            self.entries.append((check, combo, selected, reason))
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.resizeColumnToContents(0)
        table.setColumnWidth(1, 270)
        table.resizeColumnToContents(2)
        if rows:
            combo = self.entries[0][1]
            label_width = max(combo.fontMetrics().horizontalAdvance(text) for text in (UNKNOWN, *ARTICLES))
            table.setColumnWidth(3, max(combo.sizeHint().width() + 32, label_width + 100))
            if open_original is not None:
                button_width = table.cellWidget(0, 5).sizeHint().width()
                table.setColumnWidth(5, max(header.sectionSizeHint(5), button_width + 32))
        table.setColumnWidth(4, 240)
        table.resizeRowsToContents()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        if open_original is not None:
            original = buttons.addButton("Открыть оригинал", QDialogButtonBox.ButtonRole.ActionRole)
            original.setEnabled(False)
            def update_original():
                index = table.currentRow()
                original.setEnabled(index >= 0 and bool(rows[index].document.get("has_original")))
            table.itemSelectionChanged.connect(update_original)
            original.clicked.connect(lambda: open_original(rows[table.currentRow()].document))
        save = buttons.button(QDialogButtonBox.StandardButton.Save)
        save.setText("Подтвердить отмеченные")
        def enable_save():
            save.setEnabled(bool(self.changes()))
        for check, combo, _, _ in self.entries:
            check.toggled.connect(enable_save)
            combo.currentIndexChanged.connect(enable_save)
        enable_save()
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def changes(self):
        changes = []
        for row, (check, combo, suggested, reason) in zip(self.rows, self.entries):
            article = combo.currentText()
            if check.isChecked() and article != row.article:
                changes.append(dict(document_id=row.document["id"], expected_version=row.document["sync_version"],
                                    allocation_id=row.allocation["allocation_id"], article=article,
                                    evidence=reason[:500] if article == suggested else "Выбрано вручную в расходах фирмы"))
        return changes
