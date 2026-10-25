import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QDate, QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QComboBox, QDateEdit, QDoubleSpinBox, QSpinBox, QWidget
except ImportError:  # pragma: no cover
    QApplication = None

if QApplication is not None:
    import input_safety


def wheel(widget, steps=-3):
    target = widget
    point = QPointF(target.rect().center())
    event = QWheelEvent(point, QPointF(target.mapToGlobal(point.toPoint())), QPoint(),
                        QPoint(0, 120 * steps), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(target, event)
    return event


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class WheelGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        input_safety.install_wheel_guard(cls.app)

    def setUp(self):
        self.host = QWidget()
        self.host.resize(300, 200)

    def tearDown(self):
        self.host.close()
        self.host.deleteLater()

    def test_install_is_idempotent(self):
        self.assertIs(input_safety.install_wheel_guard(self.app), input_safety.install_wheel_guard(self.app))

    def test_wheel_does_not_change_value_with_or_without_focus(self):
        date = QDateEdit(QDate(2026, 10, 31), self.host)
        date.setDisplayFormat("yyyy-MM-dd")
        date.setCalendarPopup(True)
        combo = QComboBox(self.host)
        combo.addItems(["a", "b", "c"])
        number = QDoubleSpinBox(self.host)
        number.setRange(0, 1000)
        number.setValue(520)
        integer = QSpinBox(self.host)
        integer.setValue(5)
        self.host.show()
        for focused in (False, True):
            for widget, read in ((date, date.text), (combo, combo.currentText),
                                 (number, number.value), (integer, integer.value)):
                if focused:
                    widget.setFocus()
                    self.app.processEvents()
                before = read()
                event = wheel(widget)
                self.assertEqual(read(), before, f"{type(widget).__name__} focused={focused}")
                self.assertFalse(event.isAccepted())

    def test_keyboard_and_explicit_changes_still_work(self):
        date = QDateEdit(QDate(2026, 10, 31), self.host)
        date.setDisplayFormat("yyyy-MM-dd")
        number = QDoubleSpinBox(self.host)
        number.setValue(5)
        combo = QComboBox(self.host)
        combo.addItems(["a", "b"])
        self.host.show()
        date.setFocus()
        date.setCurrentSection(QDateEdit.Section.YearSection)
        QTest.keyClick(date, Qt.Key.Key_Down)
        self.assertEqual(date.text(), "2025-10-31")
        number.stepUp()
        self.assertEqual(number.value(), 6)
        combo.setFocus()
        QTest.keyClick(combo, Qt.Key.Key_Down)
        self.assertEqual(combo.currentText(), "b")

    def test_other_widgets_still_receive_wheel(self):
        events = []

        class Probe(QWidget):
            def wheelEvent(self, event):
                events.append(event.angleDelta().y())
                event.accept()

        probe = Probe(self.host)
        self.host.show()
        wheel(probe)
        self.assertEqual(events, [-360])

    def test_period_reversed(self):
        self.assertTrue(input_safety.period_reversed("2026-10-01", "2023-10-31"))
        self.assertFalse(input_safety.period_reversed("2026-10-01", "2026-10-01"))
        self.assertFalse(input_safety.period_reversed("2026-10-01", "2026-10-31"))
        self.assertFalse(input_safety.period_reversed("", "2023-10-31"))


if __name__ == "__main__":
    unittest.main()
