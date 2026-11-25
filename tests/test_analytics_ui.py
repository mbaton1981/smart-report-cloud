import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication, QBoxLayout, QDialog, QLineEdit, QMessageBox, QPushButton,
    QTableWidget, QWidget,
)

from analytics_ui import AnalyticsWidget, billing_summary, edit_invoice_expenses
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

        widget.resize(1800, 900)
        widget.show()
        self.application.processEvents()
        self.assertGreaterEqual(widget.table.columnWidth(7), 164)
        self.assertLess(widget.table.columnWidth(0), widget.table.viewport().width() / 2)
        self.assertEqual(widget.table.horizontalScrollBar().maximum(), 0)
        widget.close()

    def test_object_dropdown_uses_catalog_status_by_server_id(self):
        catalog = [
            {"id": 31, "is_active": 1},
            {"id": 32, "is_active": 0},
            {"id": 33, "is_active": 0, "service_type": "unpaid_leave"},
        ]
        widget = AnalyticsWidget(lambda _: {}, object_catalog=lambda: catalog)
        self.addCleanup(widget.deleteLater)
        widget.payload = {"objects": [
            {"key": f"object:{item['id']}", "label": label, "company": "Test"}
            for item, label in zip(catalog, ("Active job", "Archived job", "Service category"))
        ]}
        widget.update_objects()
        self.assertEqual(widget.object.currentData(), "")
        for theme, expected in (
            ("light", ("#ffffff", "#e5e8eb", "#fce8e8")),
            ("dark", ("#28303d", "#373b42", "#482e36")),
        ):
            with self.subTest(theme=theme), patch("analytics_ui.desktop_theme.current_theme", return_value=theme):
                widget.update_object_colors()
                backgrounds = tuple(widget.object.itemData(row, Qt.ItemDataRole.BackgroundRole).name()
                                    for row in range(1, widget.object.count()))
                self.assertEqual(backgrounds, expected)
        widget.object.setCurrentIndex(1)
        catalog[0]["is_active"] = 0
        with patch("analytics_ui.desktop_theme.current_theme", return_value="light"):
            widget.update_object_colors()
            self.assertEqual(widget.object.currentData(), "object:31")
            self.assertEqual(widget.object.itemData(1, Qt.ItemDataRole.BackgroundRole).name(), "#e5e8eb")

    def test_unresolved_expense_breakdown_opens_the_exact_document(self):
        detail = dict(id=38, date="2026-11-10", supplier="Test supplier", invoice_number="MAT-42",
                      net_amount="1000", assigned_net="750.50", remainder="249.50")
        other = dict(detail, id=37, invoice_number="MAT-41", remainder="100")
        reveal = Mock()
        request = Mock(return_value={"document": {"id": 38, "client_id": "client-38"}})
        widget = AnalyticsWidget(lambda _: {}, request_correction=request, reveal_expense=reveal)
        self.addCleanup(widget.deleteLater)
        widget.payload = dict(rows=[dict(key="unresolved_net", expenses=349.5)], unresolved_expenses=[other, detail])
        widget.expense_breakdown_button.setEnabled(True)

        def inspect(dialog):
            table = dialog.findChild(QTableWidget)
            self.assertEqual(table.horizontalHeaderItem(5).text(), "Разнесено без НДС")
            self.assertEqual(table.horizontalHeaderItem(6).text(), "К сверке без НДС")
            self.assertEqual(table.item(1, 3).text(), "MAT-42")
            self.assertEqual(table.item(1, 6).text(), "249.50")
            show = next(button for button in dialog.findChildren(QPushButton)
                        if button.text() == "Открыть фактуру в расходах")
            self.assertFalse(show.isEnabled())
            table.selectRow(1)
            show.click()
            return dialog.result()

        with patch("analytics_ui._run_correction", side_effect=lambda _parent, _message, callback: callback()), \
                patch.object(QDialog, "exec", new=inspect):
            widget.expense_breakdown_button.click()
        request.assert_called_once_with("GET", "/api/expense-documents/38")
        reveal.assert_called_once_with({"id": 38, "client_id": "client-38"})

    def test_billing_summary_spells_out_expense_shares(self):
        for count, label in ((0, "долей расходов"), (1, "доля расхода"), (2, "доли расходов"),
                             (7, "долей расходов"), (11, "долей расходов"), (22, "доли расходов")):
            with self.subTest(count=count):
                self.assertEqual(billing_summary({"hours": 6, "expenses": count}), f"6 ч · {count} {label}")

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
