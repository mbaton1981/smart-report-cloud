import re

from PyQt6.QtCore import QEvent, QRect, QSize, Qt, QTimer
from PyQt6.QtGui import QFontMetrics, QTextDocument, QTextOption
from PyQt6.QtWidgets import QHeaderView, QStyledItemDelegate, QTableWidget


def combo_items_need_refresh(combo, items):
    """Items contain display text, followed by consecutive UserRole values."""
    if combo.view().isVisible():
        return False
    if combo.count() != len(items):
        return True
    return any(
        combo.itemText(index) != item[0]
        or any(combo.itemData(index, Qt.ItemDataRole.UserRole + offset) != value
               for offset, value in enumerate(item[1:]))
        for index, item in enumerate(items)
    )


def column_role(title):
    label = " ".join(title.casefold().split())
    if re.search(r"сотруд|фио|anställd|фирм|объект|поставщик|описан|коммент|помет|примеч|категор|специфик|specifikation|детализ|sammanfatt|назначение", label):
        return "text"
    if re.search(r"дата|datum|date|статус|status|состояние|действие|^тип$|^id$|\bid\b|пин|источник|markning|proj\.nr", label):
        return "center"
    if re.search(r"ндс|сумм|стоим|баланс|долг|час|\btim\b|\btimmar\b|\bh\b|kr/|resa|moms|ставк|pris|процент|%|net|vat|gross|lön|skatt|värde|база|sek|kostnad|изменение|транспорт", label):
        return "number"
    return "text"


class TableDelegate(QStyledItemDelegate):
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        role = self.parent().role_for_column(index.column())
        horizontal = (Qt.AlignmentFlag.AlignRight if role == "number" else
                      Qt.AlignmentFlag.AlignHCenter if role == "center" else Qt.AlignmentFlag.AlignLeft)
        option.displayAlignment = horizontal | Qt.AlignmentFlag.AlignVCenter
        option.textElideMode = Qt.TextElideMode.ElideNone

    def sizeHint(self, option, index):
        self.initStyleOption(option, index)
        base = super().sizeHint(option, index)
        table = self.parent()
        text = str(index.data() or "")
        width = max(24, table.columnWidth(index.column()) - 32)
        document = QTextDocument()
        document.setDocumentMargin(0)
        document.setDefaultFont(option.font)
        wrapping = QTextOption()
        wrapping.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        document.setDefaultTextOption(wrapping)
        document.setPlainText(text)
        document.setTextWidth(width)
        metrics = QFontMetrics(option.font)
        preferred = metrics.horizontalAdvance(max(text.splitlines() or [""], key=len)) + 24
        if table.role_for_column(index.column()) != "number":
            preferred = min(preferred, 220)
        wrapped = metrics.boundingRect(QRect(0, 0, width, 100000), Qt.TextFlag.TextWordWrap, text)
        return QSize(max(48, preferred), max(32, base.height(), int(document.size().height()) + 16, wrapped.height() + 16))

    def helpEvent(self, event, view, option, index):
        if event.type() == QEvent.Type.ToolTip and index.isValid():
            from PyQt6.QtWidgets import QToolTip
            QToolTip.showText(event.globalPos(), str(index.data(Qt.ItemDataRole.ToolTipRole) or index.data() or ""), view)
            return True
        return super().helpEvent(event, view, option, index)


class ReportTable(QTableWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._refresh_pending = False
        self._refreshing = False
        self.setItemDelegate(TableDelegate(self))
        self.setAlternatingRowColors(True)
        self.setWordWrap(True)
        self.setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.verticalHeader().setMinimumSectionSize(32)
        self.verticalHeader().setDefaultSectionSize(34)
        self.horizontalHeader().setMinimumSectionSize(48)
        self.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.horizontalHeader().setResizeContentsPrecision(100)
        for signal in (self.model().headerDataChanged, self.model().rowsInserted,
                       self.model().dataChanged, self.model().modelReset):
            signal.connect(self.schedule_layout)
        self.itemDelegate().closeEditor.connect(self.schedule_layout)

    def role_for_column(self, column):
        item = self.horizontalHeaderItem(column)
        return column_role(item.text() if item else "")

    def suspend_content_measurement(self):
        if not getattr(self, "_refreshing", False):
            self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

    def setRowCount(self, count):
        self.suspend_content_measurement()
        super().setRowCount(count)
        self.schedule_layout()

    def insertRow(self, row):
        self.suspend_content_measurement()
        super().insertRow(row)
        self.schedule_layout()

    def schedule_layout(self, *_):
        if not self._refresh_pending and not self._refreshing:
            self._refresh_pending = True
            QTimer.singleShot(0, self.refresh_layout)

    def refresh_layout(self):
        self._refresh_pending = False
        if self._refreshing or self.state() == self.State.EditingState:
            return
        self._refreshing = True
        try:
            header = self.horizontalHeader()
            header.setStretchLastSection(False)
            for column in range(self.columnCount()):
                mode = (QHeaderView.ResizeMode.Stretch if self.role_for_column(column) == "text"
                        else QHeaderView.ResizeMode.ResizeToContents)
                header.setSectionResizeMode(column, mode)
            text_columns = [column for column in range(self.columnCount())
                            if not self.isColumnHidden(column) and self.role_for_column(column) == "text"]
            fixed_width = sum(header.sectionSize(column) for column in range(self.columnCount())
                              if not self.isColumnHidden(column) and column not in text_columns)
            minimum_text_width = 0
            for column in text_columns:
                item = self.horizontalHeaderItem(column)
                lines = item.text().splitlines() if item and item.text() else [""]
                minimum_text_width += max(120, header.fontMetrics().horizontalAdvance(max(lines, key=len)) + 24)
            if self.viewport().width() - fixed_width < minimum_text_width:
                for column in text_columns:
                    header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
            self.setTextElideMode(Qt.TextElideMode.ElideNone)
            self.resizeRowsToContents()
        finally:
            self._refreshing = False

    def showEvent(self, event):
        super().showEvent(event)
        self.schedule_layout()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.schedule_layout()