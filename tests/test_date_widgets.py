import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QDate, QPoint, Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QTableView, QWidget
except ImportError:  # pragma: no cover
    QApplication = None

if QApplication is not None:
    from date_widgets import CalendarDateEdit


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class CalendarDateEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.layout = QHBoxLayout(self.host)
        self.field = CalendarDateEdit(QDate(2026, 8, 1), self.host)
        self.field.setDisplayFormat("yyyy-MM-dd")
        self.layout.addWidget(QLabel("Период с:", self.host))
        self.layout.addWidget(self.field)
        self.host.resize(520, 180)

    def tearDown(self):
        self.host.close()
        self.host.deleteLater()

    def open_calendar(self):
        self.host.show()
        self.app.processEvents()
        QTest.mouseClick(
            self.field,
            Qt.MouseButton.LeftButton,
            pos=QPoint(self.field.width() - 8, self.field.height() // 2),
        )
        self.app.processEvents()
        return self.field.calendarWidget()

    def test_calendar_size_includes_navigation_and_six_week_rows(self):
        calendar = self.field.calendarWidget()
        view = calendar.findChild(QTableView, "qt_calendar_calendarview")
        navigation = calendar.findChild(QWidget, "qt_calendar_navigationbar")

        self.assertIsNotNone(view)
        self.assertIsNotNone(navigation)
        self.assertGreaterEqual(
            calendar.minimumSize().height(),
            view.sizeHint().height() + navigation.sizeHint().height(),
        )
        self.assertGreaterEqual(calendar.minimumSize().width(), view.sizeHint().width())
        self.assertGreaterEqual(view.model().columnCount(), 7)
        self.assertGreaterEqual(view.model().rowCount(), 7)

    def test_date_field_stays_compact_and_calendar_opens_by_field(self):
        for width in (520, 360):
            self.host.resize(width, 180)
            calendar = self.open_calendar()

            self.assertLess(self.field.width(), self.host.width() * 0.55)
            self.assertTrue(calendar.isVisible())
            view = calendar.findChild(QTableView, "qt_calendar_calendarview")
            navigation = calendar.findChild(QWidget, "qt_calendar_navigationbar")
            self.assertGreaterEqual(view.height(), view.sizeHint().height())
            self.assertTrue(navigation.isVisible())
            popup = calendar.parentWidget()
            screen = self.app.primaryScreen().availableGeometry()
            desired_x = self.field.mapToGlobal(QPoint(0, 0)).x()
            max_x = max(screen.left(), screen.x() + screen.width() - popup.width())
            expected_x = min(max(desired_x, screen.left()), max_x)
            self.assertLessEqual(abs(popup.x() - expected_x), 12)
            popup.hide()
            self.app.processEvents()

    def test_calendar_popup_stays_inside_available_screen(self):
        screen = self.app.primaryScreen().availableGeometry()
        self.host.resize(360, 80)
        self.host.move(
            screen.right() - self.host.width() - 2,
            screen.bottom() - self.host.height() - 2,
        )
        calendar = self.open_calendar()
        popup = calendar.parentWidget()
        geometry = popup.frameGeometry()

        self.assertGreaterEqual(geometry.left(), screen.left())
        self.assertGreaterEqual(geometry.top(), screen.top())
        self.assertLessEqual(geometry.x() + geometry.width(), screen.x() + screen.width())
        self.assertLessEqual(geometry.y() + geometry.height(), screen.y() + screen.height())

    def test_keyboard_entry_and_open_calendar_survive_date_refresh(self):
        calendar = self.open_calendar()
        self.field.setFocus()
        self.field.setCurrentSection(self.field.Section.YearSection)
        QTest.keyClick(self.field, Qt.Key.Key_Down)
        self.app.processEvents()

        self.assertEqual(self.field.date(), QDate(2025, 8, 1))
        self.assertTrue(calendar.isVisible())
        self.field.setSelectedSection(self.field.Section.DaySection)
        QTest.keyClicks(self.field, "15")
        self.app.processEvents()
        self.assertEqual(self.field.date(), QDate(2025, 8, 15))
        calendar.clicked.emit(QDate(2025, 8, 18))
        self.app.processEvents()
        self.assertEqual(self.field.date(), QDate(2025, 8, 18))

        calendar = self.open_calendar()
        self.field.setDate(QDate(2026, 8, 1))
        self.app.processEvents()
        self.assertTrue(calendar.isVisible())
        self.assertEqual(calendar.selectedDate(), QDate(2026, 8, 1))


if __name__ == "__main__":
    unittest.main()
