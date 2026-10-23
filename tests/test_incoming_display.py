import json
import unittest
from io import BytesIO
from unittest.mock import patch

import test_incoming_integration as integration
from offline_review import ReviewError


class IncomingDisplayTests(unittest.TestCase):
    setUpClass = classmethod(integration.IncomingIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(integration.IncomingIntegrationTests.tearDownClass.__func__)
    _add_company = integration.IncomingIntegrationTests._add_company
    setUp = integration.IncomingIntegrationTests.setUp
    stage = integration.IncomingIntegrationTests.stage

    def open_window(self):
        from offline_review_ui import OfflineReviewWindow
        window = OfflineReviewWindow(self.store, integrated=True)
        self.addCleanup(window.close)
        window.show()
        self.qt_app.processEvents()
        return window

    def background_refresh(self, window):
        self.host._incoming_viewer = window
        if not hasattr(self.host, "_expense_read_cache"):
            self.host._expense_read_cache = {}
        self.host._expense_read_attempts = {}
        self.desktop.SmartReportApp._on_expense_read_completed(
            self.host, (None, None), self.host._expense_objects, [], None)
        self.qt_app.processEvents()

    def test_post_ocr_fields_not_immutable_pre_ocr_source_are_displayed(self):
        from test_mail_automation import pdf
        self.store.add_pdf(pdf("BAUHAUS synthetic invoice"), "original-file.pdf",
                           {"fields": {}, "state": "pending"})
        document_id = self.store.documents()[-1]["id"]
        recognized = dict(self.fields, supplier_name="BAUHAUS & Co KB Servicecenter")
        self.store.record_recognition(document_id, {"fields": recognized, "state": "requires_review"})
        source = self.store.conn.execute("SELECT source_json FROM documents WHERE id=?",
                                         (document_id,)).fetchone()[0]
        self.assertIsNone(json.loads(source)["result"]["fields"].get("supplier_name"))
        window = self.open_window()
        label = window.selector.itemText(window.selector.findData(document_id))
        for value in ("BAUHAUS", "2026-10-21", "125.00", "SEK", "расход не создан"):
            self.assertIn(value, label)
        self.assertNotIn("original-file.pdf", label)
        window.selector.setCurrentIndex(window.selector.findData(document_id))
        self.assertEqual(window.inputs["supplier_name"].text(), recognized["supplier_name"])

    def test_manual_edits_and_status_survive_refresh_reopen_and_reimport(self):
        window = self.open_window()
        window.expense_button.click()
        window.inputs["supplier_name"].setText("Manually checked supplier")
        window.inputs["gross_amount"].setText("130.00")
        self.assertTrue(window.save_edits())
        history = self.store.history(1)
        with self.assertRaises(ReviewError):
            self.store.record_recognition(1, {"fields": self.fields})
        self.background_refresh(window)
        self.assertIn("Manually checked supplier", window.selector.currentText())
        self.assertIn("130.00", window.selector.currentText())
        window.later_button.click()
        self.assertIn("Разобрать позже", window.selector.currentText())
        self.assertFalse(self.store.add_pdf(self.store.original(1).read_bytes(), "renamed.pdf",
                                           {"fields": self.fields}))
        window.reload_documents()
        self.assertEqual(window.inputs["supplier_name"].text(), "Manually checked supplier")
        self.assertEqual(self.store.history(1), history)
        window.inputs["supplier_name"].setText("Unsaved correction")
        self.background_refresh(window)
        self.assertEqual(window.inputs["supplier_name"].text(), "Unsaved correction")

    def test_missing_fields_are_explicit_not_invented(self):
        from test_mail_automation import pdf
        self.store.add_pdf(pdf("No recognizable fields"), "unknown.pdf", {"fields": {}, "state": "failed"})
        window = self.open_window()
        label = window.selector.itemText(window.selector.count() - 1)
        for value in ("Поставщик не распознан", "Дата не распознана", "Сумма не определена — нужна проверка"):
            self.assertIn(value, label)
        window.selector.setCurrentIndex(window.selector.count() - 1)
        self.assertEqual(window.inputs["gross_amount"].text(), "")
        self.assertIn("Сумма не определена — нужна проверка", window.classification.text())

    def test_accounted_document_is_readable_selected_hovered_and_inactive_without_data_changes(self):
        from PyQt6.QtGui import QColor, QPalette
        from PyQt6.QtWidgets import QStyle, QStyleOptionViewItem
        window = self.open_window()
        before = list(self.store.conn.iterdump())
        with patch.object(self.store, "accounting_status", return_value=("Учтён на объекте", True)):
            window._label_token = None
            window._label_signatures = {}
            window.refresh_document_labels()
        index = window.selector.model().index(0, 0)
        self.assertIn("Учтён на объекте", index.data())
        self.assertEqual(index.data(self.desktop.Qt.ItemDataRole.ForegroundRole), QColor("#12351f"))
        delegate = window.selector.view().itemDelegate()
        from offline_review_ui import IncomingDocumentDelegate
        self.assertIsInstance(delegate, IncomingDocumentDelegate)

        def luminance(color):
            def linear(value):
                value /= 255
                return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            return sum(weight * linear(channel) for weight, channel in
                       zip((0.2126, 0.7152, 0.0722), (color.red(), color.green(), color.blue())))

        for state in (QStyle.StateFlag.State_Enabled,
                      QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_Selected,
                      QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_MouseOver,
                      QStyle.StateFlag.State_Selected):
            option = QStyleOptionViewItem()
            option.initFrom(window.selector.view())
            option.state = state
            delegate.initStyleOption(option, index)
            for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive,
                          QPalette.ColorGroup.Disabled):
                foreground = option.palette.color(group, QPalette.ColorRole.HighlightedText)
                background = option.palette.color(group, QPalette.ColorRole.Highlight)
                darker, lighter = sorted((luminance(background), luminance(foreground)))
                contrast = (lighter + 0.05) / (darker + 0.05)
                self.assertGreaterEqual(contrast, 7, (foreground.name(), background.name()))
        self.assertEqual(list(self.store.conn.iterdump()), before)

    def test_idle_refresh_does_not_reparse_or_read_documents_or_rerender_pdf(self):
        window = self.open_window()
        queries = []
        self.store.conn.set_trace_callback(queries.append)
        with patch.object(self.store, "base", side_effect=AssertionError("No unchanged OCR/classification")), \
                patch.object(self.store, "original", side_effect=AssertionError("No unchanged PDF reads")), \
                patch.object(window.gallery, "show_page", side_effect=AssertionError("No rerender")):
            for _ in range(10):
                window.refresh_document_labels()
        self.assertEqual(len(queries), 10)
        self.assertTrue(all(query == "PRAGMA data_version" for query in queries))
        self.store.conn.set_trace_callback(None)

    def test_only_changed_document_label_is_rebuilt_and_unsaved_fields_survive_reload(self):
        from test_mail_automation import pdf
        self.store.add_pdf(pdf("Other document"), "other.pdf", {"fields": self.fields})
        window = self.open_window()
        window.expense_button.click()
        window.inputs["supplier_name"].setText("Unsaved correction")
        other = window.selector.itemData(1)
        self.store.set_action(other, "later", 0)
        with patch.object(window, "document_label", wraps=window.document_label) as label:
            window.refresh_document_labels()
            self.assertEqual([call.args[0] for call in label.call_args_list], [other])
        window.reload_documents()
        self.assertEqual(window.inputs["supplier_name"].text(), "Unsaved correction")
        self.assertIn("Разобрать позже", window.selector.itemText(1))
        window.loaded_payload = window.payload()

    def test_unchanged_server_snapshot_does_not_rebuild_selected_card(self):
        window = self.open_window()
        self.background_refresh(window)
        with patch.object(window, "select_document", side_effect=AssertionError("No full card rebuild")), \
                patch.object(window, "refresh_objects", side_effect=AssertionError("No unchanged objects")):
            self.background_refresh(window)

    def test_old_invoice_label_with_collection_demand_is_blocked_without_rewriting_draft(self):
        from offline_review import packed
        fields = dict(self.fields, raw_text="Inkassokrav\nFakturanummer TEST-123")
        with self.store.conn:
            self.store.conn.execute("UPDATE documents SET base_json=? WHERE id=1", (packed(fields),))
        before = self.store.conn.execute("SELECT source_json,base_json FROM documents WHERE id=1").fetchone()
        history = self.store.history(1)
        window = self.open_window()
        window.expense_button.click()
        self.assertFalse(window.transfer_button.isEnabled())
        self.assertIn("не новая покупка", window.selector.currentText())
        self.assertTrue(any("Напоминание" in issue for issue in self.store.issues(1, window.payload())))
        self.assertEqual(self.store.history(1), history)
        self.assertEqual(self.store.conn.execute("SELECT source_json,base_json FROM documents WHERE id=1").fetchone(),
                         before)

    def test_background_refresh_preserves_page_zoom_and_both_scrollbars(self):
        from reportlab.pdfgen.canvas import Canvas
        content = BytesIO()
        canvas = Canvas(content)
        for index in range(3):
            canvas.drawString(50, 750, f"Test page {index + 1}")
            canvas.showPage()
        canvas.save()
        self.store.add_pdf(content.getvalue(), "three-pages.pdf",
                           {"fields": self.fields, "state": "requires_review"})
        window = self.open_window()
        window.selector.setCurrentIndex(window.selector.count() - 1)
        self.qt_app.processEvents()
        gallery = window.gallery
        gallery.show_page(1)
        gallery.preview.zoom(2.5)
        gallery.preview.horizontalScrollBar().setValue(120)
        gallery.preview.verticalScrollBar().setValue(530)
        before = (gallery._page_index, gallery.preview.transform(),
                  gallery.preview.horizontalScrollBar().value(), gallery.preview.verticalScrollBar().value())
        self.assertGreater(before[3], 0)
        for _ in range(5):
            self.background_refresh(window)
            window.refresh_document_labels()
            window.reload_documents()
            self.qt_app.processEvents()
            self.assertIs(window.gallery, gallery)
            after = (gallery._page_index, gallery.preview.transform(),
                     gallery.preview.horizontalScrollBar().value(), gallery.preview.verticalScrollBar().value())
            self.assertEqual(after, before)
        with patch.object(gallery.preview, "fit_width", wraps=gallery.preview.fit_width) as fit:
            window.resize(window.width() - 50, window.height() - 50)
            self.qt_app.processEvents()
            self.assertEqual(fit.call_count, 0)
            self.assertEqual(gallery._page_index, before[0])
            self.assertEqual(gallery.preview.transform(), before[1])
