import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QBoxLayout, QDialog, QLineEdit, QMessageBox, QPushButton, QTableWidget, QWidget

from analytics_ui import AnalyticsWidget, edit_invoice_expenses
from assistant_ui import AssistantDialog


class AnalyticsResponsiveLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_filters_cards_and_charts_reflow_for_narrow_window(self):
        widget = AnalyticsWidget(lambda _parameters: {})
        self.addCleanup(widget.deleteLater)

        widget.resize(1300, 800)
        widget._refresh_responsive_layout()
        self.assertEqual(widget.charts_layout.direction(), QBoxLayout.Direction.LeftToRight)
        self.assertEqual(widget.cards_layout.getItemPosition(4)[:2], (0, 4))

        widget.resize(700, 600)
        widget._refresh_responsive_layout()
        self.assertEqual(widget.charts_layout.direction(), QBoxLayout.Direction.TopToBottom)
        self.assertEqual(widget.cards_layout.getItemPosition(2)[:2], (1, 0))
        self.assertEqual(widget.filters_layout.getItemPosition(2)[:2], (1, 0))
        index = widget.filters_layout.indexOf(widget.object)
        self.assertEqual(widget.filters_layout.getItemPosition(index), (2, 0, 1, 2))
        self.assertEqual(widget.object.view().textElideMode(), Qt.TextElideMode.ElideNone)

    def test_correction_dialog_submits_only_changed_expenses_after_confirmation(self):
        parent = QWidget()
        self.addCleanup(parent.deleteLater)
        original = {"key": "expense:1:0", "net_amount": "100.00", "currency": "SEK", "index": 0}
        replacement = {"key": "expense:2:0", "net_amount": "250.00", "currency": "SEK", "index": 0}
        data = {"object_label": "P9999", "invoice_date": "2026-11-24", "revision": 4, "expenses": [
            {"snapshot": original, "included": True, "label": "First supplier", "date": "2026-11-22"},
            {"snapshot": replacement, "included": False, "label": "Second supplier", "date": "2026-11-23"},
        ]}
        calls = []

        def request(method, _path, **kwargs):
            calls.append((method, kwargs))
            return {"ok": True, "correction": data}

        def inspect(dialog):
            table = dialog.findChild(QTableWidget)
            search = dialog.findChild(QLineEdit)
            save = next(button for button in dialog.findChildren(QPushButton) if button.text() == "Сохранить исправление")
            self.assertFalse(save.isEnabled())
            table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
            search.setText("second")
            self.assertTrue(table.isRowHidden(0))
            table.item(1, 0).setCheckState(Qt.CheckState.Checked)
            self.assertTrue(save.isEnabled())
            save.click()
            return dialog.result()

        with patch("analytics_ui._run_correction", side_effect=lambda _parent, _message, callback: callback()), \
                patch.object(QDialog, "exec", new=inspect), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.assertTrue(edit_invoice_expenses(parent, request, 99, "INV-7"))
        self.assertEqual([method for method, _ in calls], ["GET", "POST"])
        self.assertEqual({key: value for key, value in calls[1][1]["json"].items() if key != "correction_id"},
                         {"object_id": 99, "invoice_number": "INV-7", "revision": 4,
                          "remove": ["expense:1:0"], "add": [replacement]})

    def test_assistant_dialog_keeps_controls_accessible_when_narrow(self):
        parent = QWidget()
        with patch.object(AssistantDialog, "call"):
            dialog = AssistantDialog(parent)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(parent.deleteLater)
        dialog.resize(700, 500)
        dialog.show()
        self.application.processEvents()

        content = dialog.content_scroll.widget()
        self.assertIsNotNone(content)
        self.assertGreater(dialog.content_scroll.verticalScrollBar().maximum(), 0)
        self.assertTrue(any(
            controller.layout.direction() == QBoxLayout.Direction.TopToBottom
            for controller in content._smartreport_responsive_layouts
        ))
        self.assertTrue(dialog.send.isVisible())
        dialog.close()


if __name__ == "__main__":
    unittest.main()
