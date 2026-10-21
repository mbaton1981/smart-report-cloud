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
        for value in ("Поставщик не распознан", "Дата не распознана", "Сумма не распознана"):
            self.assertIn(value, label)

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
