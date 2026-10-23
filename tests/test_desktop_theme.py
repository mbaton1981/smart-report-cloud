import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem

import desktop_theme as theme


class DesktopThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "appearance.json"
        self.previous = getattr(self.application, "_smartreport_theme", None)
        self.previous_style = self.application.styleSheet()
        self.previous_native = self.application.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
        self.manager = theme.ThemeManager(self.application, "QWidget {color:#e2e8f0;background:#1e2229;}", self.path)
        self.application._smartreport_theme = self.manager
        self.addCleanup(self.restore)

    def restore(self):
        if self.previous is None:
            del self.application._smartreport_theme
        else:
            self.application._smartreport_theme = self.previous
        self.application.setStyleSheet(self.previous_style)
        self.application.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, self.previous_native)
        self.manager.deleteLater()

    def test_live_toggle_persists_and_is_read_by_new_process(self):
        self.assertEqual(self.manager.name, "light")
        self.assertTrue(self.manager.toggle())
        self.assertEqual(self.manager.name, "dark")
        self.assertEqual(json.loads(self.path.read_text()), {"theme": "dark"})
        code = (
            "import sys;from PyQt6.QtWidgets import QApplication;"
            "from desktop_theme import ThemeManager;"
            "a=QApplication([]);m=ThemeManager(a,'',sys.argv[1]);print(m.name)")
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        value = subprocess.check_output([sys.executable, "-c", code, str(self.path)], env=environment, text=True)
        self.assertEqual(value.strip(), "dark")
        self.assertTrue(self.manager.toggle())
        self.assertEqual(json.loads(self.path.read_text()), {"theme": "light"})

    def test_existing_and_new_dialogs_follow_application_theme(self):
        dialog = QDialog()
        label = QLabel("Total", dialog)
        theme.apply_style(label, "color: #ffffff; background-color:#242933;")
        self.assertIn("#2c4052", label.styleSheet())
        self.manager.toggle()
        self.assertIn("#ffffff", label.styleSheet())
        theme.apply_style(QLabel("New", dialog), "color:#fca5a5;")
        self.manager.toggle()
        self.assertIn("#2c4052", label.styleSheet())
        self.assertIn("#9c3e46", dialog.findChildren(QLabel)[1].styleSheet())
        dialog.deleteLater()

    def test_table_status_colors_switch_without_changing_text_or_identity(self):
        table = QTableWidget(1, 1)
        item = QTableWidgetItem("Synthetic record")
        item.setData(256, 123)
        table.setItem(0, 0, item)
        theme.item_color(item, "#1c4532")
        edits = []
        table.cellChanged.connect(lambda *args: edits.append(args))
        self.assertEqual(item.background().color().name(), "#e4efe9")
        self.manager.toggle()
        self.assertEqual(item.background().color().name(), "#1c4532")
        self.assertEqual((item.text(), item.data(256)), ("Synthetic record", 123))
        self.manager.toggle()
        self.assertEqual(item.background().color().name(), "#e4efe9")
        self.assertEqual(edits, [])
        table.deleteLater()

    def test_header_button_is_right_of_brand_and_deleted_buttons_do_not_break_toggle(self):
        panel = theme.header()
        panel.show()
        self.application.processEvents()
        brand = panel.findChild(QLabel, "smartreportBrand")
        button = panel.findChild(QPushButton, "themeToggle")
        self.assertGreater(button.geometry().left(), brand.geometry().right())
        button.click()
        self.assertEqual(self.manager.name, "dark")
        self.assertEqual(button.text(), "Светлая тема")
        panel.deleteLater()
        self.application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertTrue(self.manager.toggle())

    def test_write_failure_is_explicit_and_does_not_switch_theme(self):
        with patch.object(theme, "QSaveFile") as output, patch.object(theme.QMessageBox, "warning") as warning:
            output.return_value.open.return_value = False
            output.return_value.errorString.return_value = "Synthetic access denied"
            self.assertFalse(self.manager.toggle())
            warning.assert_called_once()
        self.assertEqual(self.manager.name, "light")
        self.assertFalse(self.path.exists())

    def test_missing_or_invalid_theme_does_not_touch_other_settings(self):
        connection = self.path.with_name("connection.json")
        connection.write_text('{"server_url":"https://example.invalid","username":"synthetic"}')
        self.path.write_text('{"theme":"invalid"}')
        with patch.object(theme.QMessageBox, "warning") as warning:
            manager = theme.ThemeManager(self.application, "", self.path)
            warning.assert_called_once()
        self.assertEqual(manager.name, "light")
        self.assertEqual(json.loads(connection.read_text())["username"], "synthetic")
        self.assertEqual(json.loads(self.path.read_text())["theme"], "invalid")
        manager.deleteLater()

    def test_text_and_primary_action_contrast(self):
        def luminance(value):
            channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
            return sum(a * b for a, b in zip(linear, (.2126, .7152, .0722)))
        for foreground, background in (("#2c4052", "#edf2f6"), ("#ffffff", "#52718a"),
                                       ("#243b4e", "#d6e5f0"), ("#e2e8f0", "#1e2229"),
                                       ("#9c3e46", "#f7f9fb"), ("#885c21", "#f7f9fb")):
            a, b = sorted((luminance(foreground), luminance(background)))
            self.assertGreaterEqual((b + .05) / (a + .05), 4.5, (foreground, background))

    def test_theme_does_not_rewrite_pdf_bytes_or_readonly_fields(self):
        from io import BytesIO
        from reportlab.pdfgen.canvas import Canvas
        from payroll_preview import PayrollPdfPreviewDialog
        stream = BytesIO()
        canvas = Canvas(stream)
        canvas.drawString(30, 700, "Synthetic PDF")
        canvas.save()
        data = stream.getvalue()
        dialog = PayrollPdfPreviewDialog(data, self.path.with_suffix(".pdf"))
        field = QLineEdit("00100", dialog)
        field.setReadOnly(True)
        try:
            self.manager.toggle()
            self.manager.toggle()
            self.assertEqual(dialog._source_bytes, data)
            self.assertEqual(field.text(), "00100")
            self.assertTrue(field.isReadOnly())
        finally:
            dialog.release_resources()
            dialog.close()

    def test_incoming_delegate_keeps_accounting_metadata_while_changing_only_paint(self):
        from PyQt6.QtGui import QColor, QStandardItem, QStandardItemModel
        from PyQt6.QtWidgets import QComboBox, QStyle, QStyleOptionViewItem
        from offline_review_ui import IncomingDocumentDelegate
        combo = QComboBox()
        model = QStandardItemModel()
        item = QStandardItem("Synthetic accounted document")
        item.setData(QColor("#b9efc6"), Qt.ItemDataRole.BackgroundRole)
        model.appendRow(item)
        combo.setModel(model)
        delegate = IncomingDocumentDelegate(combo.view())
        combo._smartreport_delegate = delegate
        theme.apply_style(combo, "QComboBox {background:#28303d;color:#e2e8f0;}")
        combo.show()
        for expected_name in ("light", "dark"):
            if self.manager.name != expected_name:
                self.manager.toggle()
            self.application.processEvents()
            self.assertIs(combo.view().itemDelegate(), delegate)
            option = QStyleOptionViewItem()
            option.state |= QStyle.StateFlag.State_Selected
            delegate.initStyleOption(option, model.index(0, 0))
            self.assertEqual(item.data(Qt.ItemDataRole.BackgroundRole), QColor("#b9efc6"))
            expected = "#92cfaa" if expected_name == "light" else "#28573d"
            self.assertEqual(option.backgroundBrush.color().name(), expected)
        combo.deleteLater()
