from PyQt6.QtCore import QDate, QEvent, QLocale, QObject, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication, QCalendarWidget, QDateEdit, QSizePolicy, QStyledItemDelegate, QTableView,
    QToolButton, QWidget,
)

import desktop_theme


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
    def paint(self, painter, option, index):
        week_column = index.column() == 0
        heading = index.row() == 0
        rect = option.rect
        painter.save()
        painter.fillRect(rect, desktop_theme.color("#252d38" if week_column else "#303c4b"))
        painter.setPen(desktop_theme.color("#4a5568"))
        if week_column:
            painter.drawLine(rect.topRight(), rect.bottomRight())
        if heading:
            painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        font = QFont(option.font)
        font.setPixelSize(11 if week_column else 12)
        font.setWeight(QFont.Weight.DemiBold if heading else QFont.Weight.Medium)
        painter.setFont(font)
        painter.setPen(desktop_theme.color("#a0aec0" if week_column else "#e2e8f0"))
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
        view = self.findChild(QTableView, "qt_calendar_calendarview")
        if view is not None:
            self._header_delegate = CalendarHeaderDelegate(view)
            view.setItemDelegateForRow(0, self._header_delegate)
            view.setItemDelegateForColumn(0, self._header_delegate)
        self._update_navigation()
        manager = getattr(QApplication.instance(), "_smartreport_theme", None)
        if manager is not None:
            manager.changed.connect(self._update_navigation)

    def _update_navigation(self):
        stroke = desktop_theme.color("#e2e8f0").name()
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

        painter.save()
        painter.fillRect(rect, desktop_theme.color("#252d38" if weekend else "#28303d"))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        badge = QRectF(rect).adjusted(4, 3, -4, -3)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            selection = "#52718a" if desktop_theme.current_theme() == "light" else "#2b6cb0"
            painter.setBrush(QColor(selection))
            painter.drawRoundedRect(badge, 6, 6)
        elif today:
            painter.setPen(QPen(desktop_theme.color("#4299e1"), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(badge, 6, 6)

        font = QFont(self.font())
        font.setPixelSize(14 if in_month or selected else 12)
        font.setWeight(QFont.Weight.DemiBold if selected or today else QFont.Weight.Normal)
        painter.setFont(font)
        if selected:
            foreground = QColor("#ffffff")
        elif not enabled or not in_month:
            foreground = desktop_theme.color("#718096")
        else:
            foreground = desktop_theme.color("#e2e8f0")
        painter.setPen(foreground)
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
