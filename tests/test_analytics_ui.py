import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QBoxLayout, QWidget

from analytics_ui import AnalyticsWidget
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
