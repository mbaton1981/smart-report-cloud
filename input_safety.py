from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import QAbstractSpinBox, QComboBox

PERIOD_WARNING = "Конец периода раньше начала — проверьте даты «с» и «по». Записи не показаны."


class WheelGuard(QObject):
    """Stops the mouse wheel from changing combo boxes, dates and numeric fields.

    The ignored event continues to the parent widget, so the surrounding page scrolls.
    An open combo popup receives wheel events in its own view and keeps scrolling.
    """

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel and isinstance(watched, (QComboBox, QAbstractSpinBox)):
            event.ignore()
            return True
        return False


def install_wheel_guard(application):
    guard = getattr(application, "_smartreport_wheel_guard", None)
    if guard is None:
        guard = WheelGuard(application)
        application.installEventFilter(guard)
        application._smartreport_wheel_guard = guard
    return guard


def period_reversed(date_from, date_to):
    date_from, date_to = (date_from or "").strip(), (date_to or "").strip()
    return bool(date_from and date_to and date_from > date_to)
