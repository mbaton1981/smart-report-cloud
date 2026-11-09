from PyQt6.QtCore import QEvent, QObject, QSize
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QCalendarWidget, QDateEdit, QTableView, QSizePolicy, QWidget,
)


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


class ResponsiveCalendarWidget(QCalendarWidget):
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
            max(size.width(), view_size.width() + margins.left() + margins.right()),
            max(
                size.height(),
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
