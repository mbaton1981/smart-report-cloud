import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import (
    QApplication, QBoxLayout, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from qt_test_support import cleanup_new_top_level_widgets, snapshot_top_level_widgets
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
        self._qt_widgets_before = snapshot_top_level_widgets(self.application)
        self.manager = theme.ThemeManager(self.application, "QWidget {color:#e2e8f0;background:#1e2229;}", self.path)
        self.application._smartreport_theme = self.manager
        self.addCleanup(self.restore)

    def restore(self):
        if self.previous is None:
            del self.application._smartreport_theme
        else:
            self.application._smartreport_theme = self.previous
        self.manager.deleteLater()
        cleanup_new_top_level_widgets(self.application, self._qt_widgets_before)
        self.application.setStyleSheet(self.previous_style)
        self.application.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, self.previous_native)

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

    def test_colored_items_added_before_insertion_stay_tracked_after_sorting(self):
        table = QTableWidget(2, 1)
        colored = QTableWidgetItem("z-colored")
        theme.item_color(colored, "#1c4532")
        table.setItem(0, 0, colored)
        table.setItem(1, 0, QTableWidgetItem("a-plain"))
        theme.refresh_table_item_colors(table)
        table.setSortingEnabled(True)
        table.sortItems(0, Qt.SortOrder.AscendingOrder)

        self.assertTrue(self.manager.toggle())
        colored_row = table.findItems("z-colored", Qt.MatchFlag.MatchExactly)[0].row()
        self.assertEqual(table.item(colored_row, 0).background().color().name(), "#1c4532")
        table.deleteLater()

    def test_repeated_theme_switch_preserves_open_table_and_invoice_form_state(self):
        tabs = QTabWidget()
        first_tab = QWidget()
        first_layout = QVBoxLayout(first_tab)
        filter_input = QLineEdit("P1034", first_tab)
        first_layout.addWidget(filter_input)
        table = QTableWidget(80, 8, first_tab)
        first_layout.addWidget(table)
        tabs.addTab(first_tab, "Journal")
        tabs.addTab(QWidget(), "Expenses")
        tabs.setCurrentIndex(1)

        dialog = QDialog(tabs)
        draft_input = QLineEdit("Unsaved invoice review", dialog)
        dialog_layout = QVBoxLayout(dialog)
        dialog_layout.addWidget(draft_input)
        invoice_table = QTableWidget(24, 6, dialog)
        dialog_layout.addWidget(invoice_table)

        item = QTableWidgetItem("Reviewed record")
        table.setItem(0, 0, item)
        theme.item_color(item, "#1c4532")
        invoice_item = QTableWidgetItem("Pending document")
        invoice_table.setItem(0, 0, invoice_item)
        theme.item_color(invoice_item, "#1c4532")
        paused_table = QTableWidget(1, 1)
        paused_table.setUpdatesEnabled(False)
        signal_changes = []
        table.cellChanged.connect(lambda *args: signal_changes.append(args))
        invoice_table.cellChanged.connect(lambda *args: signal_changes.append(args))

        tabs.show()
        dialog.show()
        self.application.processEvents()
        try:
            for expected_theme in ("dark", "light", "dark", "light"):
                self.assertTrue(self.manager.toggle())
                self.application.processEvents()
                self.assertEqual(self.manager.name, expected_theme)
                self.assertEqual(tabs.currentIndex(), 1)
                self.assertEqual(filter_input.text(), "P1034")
                self.assertEqual(draft_input.text(), "Unsaved invoice review")
                self.assertEqual(item.text(), "Reviewed record")
                self.assertEqual(invoice_item.text(), "Pending document")
                self.assertTrue(table.updatesEnabled())
                self.assertTrue(invoice_table.updatesEnabled())
                self.assertFalse(paused_table.updatesEnabled())
                self.assertEqual(
                    item.background().color().name(),
                    "#1c4532" if expected_theme == "dark" else "#e4efe9",
                )
            self.assertEqual(signal_changes, [])
        finally:
            dialog.close()
            tabs.close()
            paused_table.deleteLater()

    def test_switch_does_not_remeasure_content_sized_table_columns(self):
        import desktop_ui

        table = desktop_ui.ReportTable(150, 8)
        table.setHorizontalHeaderLabels(["Дата", "Поставщик", "Сумма", "Часы", "Статус", "Объект", "НДС", "Источник"])
        for row in range(150):
            for column in range(8):
                table.setItem(row, column, QTableWidgetItem(f"Запись {row}-{column} " + "текст " * (row % 4)))
        table.resize(1100, 500)
        table.show()
        self.application.processEvents()
        table.refresh_layout()
        self.application.processEvents()
        table.selectRow(40)
        table.verticalScrollBar().setValue(300)
        self.application.processEvents()
        table.refresh_layout()
        self.application.processEvents()
        header = table.horizontalHeader()
        modes = [header.sectionResizeMode(index) for index in range(8)]
        widths = [header.sectionSize(index) for index in range(8)]
        scroll = table.verticalScrollBar().value()
        measured = []
        original = desktop_ui.TableDelegate.sizeHint

        def counted(delegate, option, index):
            measured.append(1)
            return original(delegate, option, index)

        try:
            for _ in range(2):
                self.assertTrue(self.manager.toggle())
                self.application.processEvents()
            with patch.object(desktop_ui.TableDelegate, "sizeHint", counted):
                for _ in range(4):
                    self.assertTrue(self.manager.toggle())
                    self.application.processEvents()
            # Одно перемеривание после смены стиля допустимо; раньше шло в два раза больше.
            self.assertLess(len(measured), 4 * 150 * 8)
            self.assertEqual([header.sectionResizeMode(index) for index in range(8)], modes)
            self.assertEqual([header.sectionSize(index) for index in range(8)], widths)
            self.assertEqual(table.currentRow(), 40)
            self.assertEqual(table.verticalScrollBar().value(), scroll)
        finally:
            table.close()
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
        self.assertEqual(button.text(), "Светлая")
        panel.deleteLater()
        self.application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertTrue(self.manager.toggle())

    def test_dense_control_rows_stack_below_their_width_threshold(self):
        owner = QWidget()
        row = QHBoxLayout(owner)
        row.addWidget(QLabel("Start", owner))
        row.addWidget(QLabel("End", owner))
        theme.adapt_box_layout(owner, row, compact_width=800)
        owner.show()
        self.application.processEvents()

        owner.resize(900, 120)
        self.application.processEvents()
        self.assertEqual(row.direction(), QBoxLayout.Direction.LeftToRight)
        owner.resize(700, 120)
        self.application.processEvents()
        self.assertEqual(row.direction(), QBoxLayout.Direction.TopToBottom)
        owner.resize(900, 120)
        self.application.processEvents()
        self.assertEqual(row.direction(), QBoxLayout.Direction.LeftToRight)
        owner.deleteLater()

    def test_dialog_windows_are_kept_inside_the_available_screen(self):
        screen = self.application.primaryScreen().availableGeometry()
        dialog = QDialog()
        dialog.setMinimumSize(screen.width() + 80, screen.height() + 80)
        dialog.resize(screen.width() + 120, screen.height() + 120)
        dialog.move(screen.right(), screen.bottom())
        dialog.show()
        self.application.processEvents()

        geometry = dialog.frameGeometry()
        self.assertLessEqual(geometry.width(), screen.width())
        self.assertLessEqual(geometry.height(), screen.height())
        self.assertGreaterEqual(geometry.left(), screen.left())
        self.assertGreaterEqual(geometry.top(), screen.top())
        self.assertLessEqual(geometry.right(), screen.right())
        self.assertLessEqual(geometry.bottom(), screen.bottom())
        dialog.close()

    def test_splitter_switches_to_vertical_layout_for_narrow_windows(self):
        owner = QWidget()
        splitter = QSplitter(Qt.Orientation.Horizontal, owner)
        splitter.addWidget(QLabel("Left", splitter))
        splitter.addWidget(QLabel("Right", splitter))
        theme.adapt_splitter(owner, splitter, compact_width=800)
        owner.resize(900, 600)
        owner.show()
        self.application.processEvents()
        self.assertEqual(splitter.orientation(), Qt.Orientation.Horizontal)

        owner.resize(700, 600)
        self.application.processEvents()
        self.assertEqual(splitter.orientation(), Qt.Orientation.Vertical)
        owner.resize(900, 600)
        self.application.processEvents()
        self.assertEqual(splitter.orientation(), Qt.Orientation.Horizontal)
        owner.deleteLater()

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

    def test_shift_status_colors_are_readable_and_selection_differs_in_both_themes(self):
        def luminance(value):
            channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
            return sum(a * b for a, b in zip(linear, (.2126, .7152, .0722)))

        def contrast(first, second):
            a, b = sorted((luminance(first), luminance(second)))
            return (b + .05) / (a + .05)

        for background, foreground in ((theme.SHIFT_CONFIRMED_BG, theme.SHIFT_CONFIRMED_FG),
                                       (theme.SHIFT_INVOICED_BG, theme.SHIFT_INVOICED_FG)):
            selection = theme.STATUS_SELECTION[background]
            for index, light in enumerate((False, True)):
                row_background = theme.COLORS[background] if light else background
                row_foreground = theme.COLORS[foreground] if light else foreground
                self.assertGreaterEqual(contrast(row_foreground, row_background), 4.5)
                self.assertGreaterEqual(contrast(*selection[index]), 4.5)
                self.assertGreaterEqual(contrast(selection[index][0], row_background), 1.5)
        self.assertEqual(theme.COLORS[theme.SHIFT_CONFIRMED_BG], "#dcecf8")
        self.assertEqual(theme.COLORS[theme.SHIFT_INVOICED_BG], "#dcf1e4")

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
