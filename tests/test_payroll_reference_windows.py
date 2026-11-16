import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QTableWidget, QWidget

import app
from qt_test_support import cleanup_new_top_level_widgets, snapshot_top_level_widgets


class _ThemeProbe(QObject):
    changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.name = "dark"


class PayrollReferenceWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widgets_before = snapshot_top_level_widgets(self.qt_app)
        self.host = QWidget()
        self.host._salary_reference_windows = {}
        self.theme_before = getattr(self.qt_app, "_smartreport_theme", None)
        self.theme = _ThemeProbe()
        self.qt_app._smartreport_theme = self.theme

    def tearDown(self):
        if self.theme_before is None:
            delattr(self.qt_app, "_smartreport_theme")
        else:
            self.qt_app._smartreport_theme = self.theme_before
        cleanup_new_top_level_widgets(self.qt_app, self.widgets_before)

    def test_reference_windows_reopen_show_original_and_do_not_touch_payroll_data(self):
        expected = {
            "ob": [
                ("Пн–Пт", "05:00–06:00", "OB 1", "+20%"),
                ("Пн–Пт", "06:00–18:00", "Обычная оплата", "без OB"),
                ("Пн–Пт", "18:00–22:00", "OB 2", "+40%"),
                ("Пн–Пт", "22:00–05:00", "OB 3", "+70%"),
                ("Сб, Вс и праздничные", "00:00–24:00", "OB 3", "+70%"),
            ],
            "overtime": [
                ("Пн–Пт", "05:00–06:00", "Övertid B", "+50%"),
                ("Пн–Пт", "06:00–17:00", "Övertid A", "+30%"),
                ("Пн–Пт", "17:00–19:00", "Övertid B", "+50%"),
                ("Пн–Пт", "19:00–22:00", "Övertid C", "+70%"),
                ("Пн–Пт", "22:00–05:00", "Övertid D", "+100%"),
                ("Сб, Вс и праздничные", "00:00–24:00", "Övertid D", "+100%"),
            ],
        }
        with patch.object(app, "get_db_connection",
                          side_effect=AssertionError("Reference view must not access payroll DB")):
            for kind, rows in expected.items():
                with self.subTest(kind=kind):
                    app.SmartReportApp.open_salary_reference_window(self.host, kind)
                    dialog = self.host._salary_reference_windows[kind]
                    table = dialog.findChild(QTableWidget)
                    self.assertIsNotNone(table)
                    self.assertFalse(dialog.isModal())
                    self.assertEqual(table.rowCount(), len(rows))
                    self.assertEqual(table.columnCount(), 4)
                    for row_index, expected_row in enumerate(rows):
                        self.assertEqual(
                            tuple(table.item(row_index, column).text() for column in range(4)),
                            expected_row,
                        )
                    self.assertEqual(table.item(0, 1).font().pointSize(), 15)
                    self.assertEqual(
                        table.horizontalScrollBarPolicy().name,
                        "ScrollBarAlwaysOff",
                    )
                    labels = " ".join(
                        label.text() for label in dialog.findChildren(app.QLabel)
                    )
                    self.assertIn("По предоставленной памятке", labels)
                    if kind == "ob":
                        self.assertIn("§3, пункте 6.2", labels)
                        self.assertIn("utgående lön", labels)
                        self.assertIn("(месячная зарплата × 12)", labels)
                        self.assertIn("OB не выплачивается", labels)
                    else:
                        self.assertIn("OB и сверхурочную доплату не складывать", labels)
                        self.assertIn("предварительное соглашение", labels)

                    app.SmartReportApp.open_salary_reference_window(self.host, kind)
                    self.assertIs(self.host._salary_reference_windows[kind], dialog)
                    self.assertTrue(dialog.isVisible())

                    original_button = next(
                        button for button in dialog.findChildren(app.QPushButton)
                        if button.text() == "Посмотреть оригинал"
                    )
                    original_button.click()
                    preview = dialog._source_preview_window
                    self.assertTrue(preview.isVisible())
                    preview.close()

                    dialog.close()
                    app.SmartReportApp.open_salary_reference_window(self.host, kind)
                    self.assertIs(self.host._salary_reference_windows[kind], dialog)
                    self.assertTrue(dialog.isVisible())

                    self.theme.name = "light"
                    self.theme.changed.emit("light")
                    self.assertEqual(
                        table.item(0, 1).background().color().name(),
                        "#f4e3cd",
                    )
                    self.theme.name = "dark"
                    self.theme.changed.emit("dark")


if __name__ == "__main__":
    unittest.main()
