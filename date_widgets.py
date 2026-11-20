from PyQt6.QtCore import QDate, QEvent, QLocale, QObject, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication, QCalendarWidget, QDateEdit, QSizePolicy, QStyledItemDelegate, QTableView,
    QToolButton, QWidget,
)

import desktop_theme


CALENDAR_COLORS = {
    "light": {
        "navigation": "#532c76",
        "button": "#6d4191",
        "button_hover": "#8455a7",
        "button_border": "#a274c0",
        "navigation_text": "#fff8ff",
        "header": "#e9d7f7",
        "weeks": "#dbc2ef",
        "divider": "#bc99d6",
        "grid": "#faf1ff",
        "weekend": "#f9e1ec",
        "adjacent": "#efe1f7",
        "text": "#352146",
        "weekend_text": "#802951",
        "muted": "#6c567a",
        "selected": "#08796c",
        "selected_text": "#ffffff",
        "today": "#a84511",
    },
    "dark": {
        "navigation": "#39214e",
        "button": "#573579",
        "button_hover": "#714798",
        "button_border": "#8b63ab",
        "navigation_text": "#fff2ff",
        "header": "#4a305e",
        "weeks": "#563a6a",
        "divider": "#8967a1",
        "grid": "#2e203b",
        "weekend": "#452640",
        "adjacent": "#352741",
        "text": "#f3e4ff",
        "weekend_text": "#ffc4e1",
        "muted": "#b6a1c5",
        "selected": "#69dfbb",
        "selected_text": "#142e2a",
        "today": "#ffac63",
    },
}


class PopupBoundsGuard(QObject):
    def eventFilter(self, watched, event):
        if event.type() != QEvent.Type.Show:
            return False
        screen = watched.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return False
        available = screen.availableGeometry()
        frame = watched.frameGeometry()
        max_left = max(available.left(), available.x() + available.width() - frame.width())
        max_top = max(available.top(), available.y() + available.height() - frame.height())
        left = min(max(frame.left(), available.left()), max_left)
        top = min(max(frame.top(), available.top()), max_top)
        if left != frame.left() or top != frame.top():
            watched.move(left, top)
        return False


class CalendarHeaderDelegate(QStyledItemDelegate):
    def __init__(self, view, calendar):
        super().__init__(view)
        self.calendar = calendar

    def paint(self, painter, option, index):
        week_column = index.column() == 0
        heading = index.row() == 0
        rect = option.rect
        colors = self.calendar.colors
        weekend_heading = heading and index.column() >= 6
        background = "weeks" if week_column else "weekend" if weekend_heading else "header"
        painter.save()
        painter.fillRect(rect, QColor(colors[background]))
        painter.setPen(QColor(colors["divider"]))
        if week_column:
            painter.drawLine(rect.topRight(), rect.bottomRight())
        if heading:
            painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        font = QFont(option.font)
        font.setPixelSize(11 if week_column else 12)
        font.setWeight(QFont.Weight.DemiBold if heading else QFont.Weight.Medium)
        painter.setFont(font)
        painter.setPen(QColor(colors["weekend_text" if weekend_heading else "text"]))
        label = "Нед." if week_column and heading else index.data()
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(label or ""))
        painter.restore()


class ResponsiveCalendarWidget(QCalendarWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setLocale(QLocale(QLocale.Language.Russian))
        self.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        self.setHorizontalHeaderFormat(self.HorizontalHeaderFormat.ShortDayNames)
        self.setVerticalHeaderFormat(self.VerticalHeaderFormat.ISOWeekNumbers)
        self._update_colors(desktop_theme.current_theme())
        view = self.findChild(QTableView, "qt_calendar_calendarview")
        if view is not None:
            self._header_delegate = CalendarHeaderDelegate(view, self)
            view.setItemDelegateForRow(0, self._header_delegate)
            view.setItemDelegateForColumn(0, self._header_delegate)
        manager = getattr(QApplication.instance(), "_smartreport_theme", None)
        if manager is not None:
            manager.changed.connect(self._update_colors)

    def _update_colors(self, theme):
        colors = self.colors = CALENDAR_COLORS[theme]
        self.setStyleSheet(f"""
            QWidget#qt_calendar_navigationbar {{
                background-color: {colors["navigation"]};
            }}
            QCalendarWidget QToolButton, QSpinBox#qt_calendar_yearedit {{
                color: {colors["navigation_text"]};
                background-color: {colors["button"]};
                border: 1px solid {colors["button_border"]};
            }}
            QCalendarWidget QToolButton:hover, QCalendarWidget QToolButton:pressed,
            QCalendarWidget QToolButton:focus, QSpinBox#qt_calendar_yearedit:focus {{
                background-color: {colors["button_hover"]};
                border-color: {colors["navigation_text"]};
            }}
            QCalendarWidget QToolButton:disabled {{
                color: {colors["muted"]};
                background-color: {colors["adjacent"]};
                border-color: {colors["divider"]};
            }}
            QCalendarWidget QAbstractItemView, QCalendarWidget QMenu {{
                color: {colors["text"]};
                background-color: {colors["grid"]};
                border: 1px solid {colors["divider"]};
                selection-color: {colors["selected_text"]};
                selection-background-color: {colors["selected"]};
            }}
            QCalendarWidget QMenu::item:selected {{
                color: {colors["selected_text"]};
                background-color: {colors["selected"]};
            }}
        """)

        stroke = colors["navigation_text"]
        for name, icon, label in (
            ("qt_calendar_prevmonth", "chevron-left", "Предыдущий месяц"),
            ("qt_calendar_nextmonth", "chevron-right", "Следующий месяц"),
        ):
            button = self.findChild(QToolButton, name)
            if button is not None:
                button.setArrowType(Qt.ArrowType.NoArrow)
                button.setIcon(desktop_theme.line_icon(icon, stroke, size=16))
                button.setToolTip(label)
                button.setAccessibleName(label)

    def paintCell(self, painter, rect, date):
        in_month = date.month() == self.monthShown() and date.year() == self.yearShown()
        enabled = self.minimumDate() <= date <= self.maximumDate()
        selected = date == self.selectedDate() and enabled
        today = date == QDate.currentDate() and in_month and enabled
        weekend = date.dayOfWeek() >= Qt.DayOfWeek.Saturday.value
        colors = self.colors
        background = "adjacent" if not in_month else "weekend" if weekend else "grid"

        painter.save()
        painter.fillRect(rect, QColor(colors[background]))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        badge = QRectF(rect).adjusted(4, 3, -4, -3)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colors["selected"]))
            painter.drawRoundedRect(badge, 6, 6)
        elif today:
            painter.setPen(QPen(QColor(colors["today"]), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(badge, 6, 6)

        font = QFont(self.font())
        font.setPixelSize(14 if in_month or selected else 12)
        font.setWeight(QFont.Weight.DemiBold if selected or today else QFont.Weight.Normal)
        painter.setFont(font)
        if selected:
            foreground = "selected_text"
        elif not enabled or not in_month:
            foreground = "muted"
        else:
            foreground = "weekend_text" if weekend else "text"
        painter.setPen(QColor(colors[foreground]))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(date.day()))
        painter.restore()

    def sizeHint(self):
        size = super().sizeHint()
        calendar_view = self.findChild(QTableView, "qt_calendar_calendarview")
        navigation_bar = self.findChild(QWidget, "qt_calendar_navigationbar")
        if calendar_view is None or navigation_bar is None:
            return size

        layout = self.layout()
        margins = layout.contentsMargins() if layout is not None else self.contentsMargins()
        spacing = max(0, layout.spacing()) if layout is not None else 0
        view_size = calendar_view.sizeHint()
        navigation_size = navigation_bar.sizeHint()
        return QSize(
            max(336, size.width(), max(view_size.width(), navigation_size.width())
                + margins.left() + margins.right()),
            max(
                268, size.height(),
                view_size.height() + navigation_size.height() + spacing
                + margins.top() + margins.bottom(),
            ),
        )


class CalendarDateEdit(QDateEdit):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setCalendarPopup(True)
        calendar = ResponsiveCalendarWidget(self)
        self.setCalendarWidget(calendar)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        calendar.ensurePolished()
        calendar.setMinimumSize(calendar.sizeHint())
        popup = calendar.parentWidget()
        self._calendar_bounds_guard = PopupBoundsGuard(popup)
        popup.installEventFilter(self._calendar_bounds_guard)
