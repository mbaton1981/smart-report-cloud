import json
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from PyQt6.QtWidgets import QBoxLayout

import test_incoming_integration as integration
from offline_review import ReviewError


class IncomingDisplayTests(unittest.TestCase):
    setUpClass = classmethod(integration.IncomingIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(integration.IncomingIntegrationTests.tearDownClass.__func__)
    _add_company = integration.IncomingIntegrationTests._add_company
    setUp = integration.IncomingIntegrationTests.setUp
    stage = integration.IncomingIntegrationTests.stage

    def test_document_preview_controls_reflow_in_small_dialog(self):
        dialogs = (
            self.desktop.DocumentPreviewDialog(
                self.desktop.QPixmap(32, 32), "Synthetic preview"),
            self.desktop.DocumentGalleryDialog([], "Synthetic gallery"),
        )
        for dialog in dialogs:
            with self.subTest(dialog=type(dialog).__name__):
                self.addCleanup(dialog.deleteLater)
                dialog.resize(560, 500)
                dialog.show()
                self.qt_app.processEvents()
                self.assertTrue(any(
                    controller.layout.direction() == QBoxLayout.Direction.TopToBottom
                    for controller in dialog._smartreport_responsive_layouts
                ))
                dialog.close()

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

    def test_order_confirmation_exclusion_has_specific_caption(self):
        base = self.store.base(1)
        base["document_type"] = "order_confirmation"
        with self.store.conn:
            self.store.conn.execute("UPDATE documents SET base_json=? WHERE id=1",
                                    (json.dumps(base, ensure_ascii=False),))
        self.store.set_action(1, "ignore", 0)
        window = self.open_window()
        window.show_all_states.setChecked(True)
        index = window.selector.findData(1)
        self.assertGreaterEqual(index, 0)
        window.selector.setCurrentIndex(index)
        self.assertEqual(window.ignore_button.text(), "Подтверждение заказа — не учитывать")
        self.assertIn("Исключён — Подтверждение заказа, не учитывать",
                      window.selector.currentText())

    def test_missing_date_and_currency_are_localized_focused_and_do_not_clear_review(self):
        from PyQt6.QtWidgets import QMessageBox

        window = self.open_window()
        window.allocations.setRowCount(0)
        window.add_allocation({
            "destination": "object", "object_id": self.object_id, "gross_amount": "125.00",
        })
        window.inputs["gross_amount"].setText("125.00")
        window.inputs["document_date"].clear()
        window.inputs["currency_code"].clear()
        allocation_before = window.payload()["allocations"]
        gross_before = window.inputs["gross_amount"].text()

        with patch("offline_review_ui.QMessageBox.warning") as warning:
            window.confirm_transfer()

        message = warning.call_args.args[2]
        self.assertIn("Укажите дату документа", message)
        self.assertIn("Выберите валюту", message)
        self.assertNotIn("document_date", message)
        self.assertNotIn("currency_code", message)
        self.assertIn("Сверил оригинал", window.checked.text())
        self.assertIn("обязательна перед учётом расхода", window.original_check_help.text())
        self.assertEqual(window.inputs["document_date"].placeholderText(), "Укажите дату документа")
        self.assertEqual(window.inputs["currency_code"].placeholderText(), "Выберите валюту")
        self.assertIn("#c53030", window.inputs["document_date"].styleSheet())
        self.assertIn("#c53030", window.inputs["currency_code"].styleSheet())
        self.assertEqual(window.focusWidget(), window.inputs["document_date"])
        self.assertEqual(window.payload()["allocations"], allocation_before)
        self.assertEqual(window.inputs["gross_amount"].text(), gross_before)
        self.assertIsNone(self.store.card(1))
        self.assertTrue(window.save_edits())

    def test_manual_date_currency_and_original_check_make_test_copy_ready(self):
        window = self.open_window()
        window.allocations.setRowCount(0)
        window.add_allocation({
            "destination": "object", "object_id": self.object_id, "gross_amount": "125.00",
        })
        window.inputs["document_date"].setText("2026-10-21")
        window.inputs["currency_code"].setText("XYZ")
        self.assertIn("Выберите валюту.", self.store.issues(1, window.payload()))
        window.inputs["currency_code"].setText("EUR")
        window.checked.setChecked(True)

        self.assertEqual(self.store.issues(1, window.payload()), [])
        self.assertIsNone(self.store.card(1))
        self.assertEqual(window.payload()["currency_code"], "EUR")
        self.assertTrue(window.save_edits())

    def test_rerecognition_conflict_is_explained_without_replacing_saved_type(self):
        base = self.store.base(1)
        base["document_type"] = "receipt"
        with self.store.conn:
            self.store.conn.execute("UPDATE documents SET base_json=? WHERE id=1",
                                    (json.dumps(base, ensure_ascii=False),))
        conflicts = self.store.record_recognition(
            1, {"fields": self.fields, "state": "requires_review"})
        self.assertEqual(conflicts, [{
            "field": "document_type", "current": "receipt", "recognized": "invoice",
        }])
        window = self.open_window()
        self.assertIn("Тип документа: сохранено «Чек», распознано «Фактура».",
                      window.classification.text())
        self.assertIn("Существующие значения не изменены", window.classification.text())
        self.assertEqual(self.store.base(1)["document_type"], "receipt")

    def test_manual_edits_and_status_survive_refresh_reopen_and_reimport(self):
        window = self.open_window()
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

    def test_explicit_automatic_refresh_preserves_original_id_and_audit(self):
        original = self.store.original(1).read_bytes()
        before = self.store.base(1)
        source_before = json.loads(self.store.conn.execute(
            "SELECT source_json FROM documents WHERE id=1").fetchone()[0])
        result = {"fields": dict(self.fields, supplier_name="Correct issuer AB"),
                  "state": "requires_review", "errors": []}
        self.store.record_recognition(1, result, replace_automatic=True)
        self.assertEqual(self.store.latest(1)[0], 0)
        self.assertEqual(self.store.latest(1)[1]["supplier_name"], "Correct issuer AB")
        self.assertEqual(self.store.original(1).read_bytes(), original)
        source_after = json.loads(self.store.conn.execute(
            "SELECT source_json FROM documents WHERE id=1").fetchone()[0])
        self.assertEqual(source_after["result"], source_before["result"])
        self.assertEqual(source_after["recognition_history"][0]["base"], before)
        self.assertIsNone(self.store.card(1))

    def test_explicit_automatic_refresh_rejects_manual_or_allocated_draft(self):
        revision, payload = self.store.latest(1)
        payload["allocations"] = [{"object_id": 7, "gross_amount": "125.00"}]
        with self.store.conn:
            self.store.conn.execute(
                "UPDATE revisions SET payload_json=? WHERE document_id=1 AND revision=0",
                (json.dumps(payload),))
        with self.assertRaises(ReviewError):
            self.store.record_recognition(
                1, {"fields": self.fields}, replace_automatic=True)

    def test_refresh_audit_retains_raw_fields_before_fresh_classification(self):
        stored = dict(self.fields, supplier_name="Incorrect buyer AB",
                      raw_text="\n".join(["Bank details"] * 20 + ["Orderbekräftelse"]))
        with self.store.conn:
            self.store.conn.execute("UPDATE documents SET base_json=? WHERE id=1",
                                    (json.dumps(stored),))
        self.store.record_recognition(
            1, {"fields": self.fields, "state": "requires_review"}, replace_automatic=True)
        source = json.loads(self.store.conn.execute(
            "SELECT source_json FROM documents WHERE id=1").fetchone()[0])
        self.assertEqual(source["recognition_history"][0]["base"], stored)

    def test_main_register_projects_local_documents_before_confirmation_and_company_is_allocated(self):
        host = SimpleNamespace(
            _incoming_documents_root=lambda: self.store.root,
            _local_incoming_expense_documents=lambda date_from=None, date_to=None:
                self.desktop.SmartReportApp._local_incoming_expense_documents(
                    host, date_from, date_to),
            _expense_objects=self.host._expense_objects,
            _expense_document_snapshot=lambda *_args, **_kwargs: ([], False),
            exp_api_status_lbl=SimpleNamespace(setText=lambda _text: None),
        )
        documents, online = self.desktop.SmartReportApp._expense_register_snapshot(host)
        self.assertFalse(online)
        self.assertEqual(len(documents), 3)
        pending = documents[0]
        self.assertEqual(pending["verification_status"], "requires_review")
        self.assertEqual(pending["incoming_document_id"], 1)
        self.assertTrue(pending["has_original"])
        self.assertEqual(
            self.desktop.SmartReportApp._expense_register_status_key(pending), "requires_review")

        action_revision, _action = self.store.action(1)
        self.store.set_action(1, "later", action_revision)
        documents, _online = self.desktop.SmartReportApp._expense_register_snapshot(host)
        self.assertEqual(self.desktop.SmartReportApp._expense_register_status_key(documents[0]), "later")
        action_revision, _action = self.store.action(1)
        self.store.set_action(1, "ignore", action_revision)
        documents, _online = self.desktop.SmartReportApp._expense_register_snapshot(host)
        self.assertEqual(self.desktop.SmartReportApp._expense_register_status_key(documents[0]), "ignored")
        self.assertEqual(documents[0]["exclusion_reason"], "Не фактура")
        self.assertEqual(
            self.desktop.SmartReportApp._expense_register_row_tone(documents[0]), "neutral")

        company = {
            "verification_status": "confirmed",
            "allocation_basis": "gross",
            "gross_amount": "125.00",
            "allocations": [{
                "destination": "company", "object_id": None, "gross_amount": "125.00",
            }],
            "unallocated_gross": "0.00",
        }
        self.assertFalse(self.desktop.SmartReportApp._expense_register_is_unallocated(company))

    def test_main_register_explains_local_documents_outside_the_selected_period(self):
        from PyQt6.QtWidgets import QCheckBox, QComboBox, QLabel, QTableWidget

        host = SimpleNamespace(
            _incoming_documents_root=lambda: self.store.root,
            _expense_objects=self.host._expense_objects,
            _expense_document_snapshot=lambda *_args, **_kwargs: ([], False),
            exp_api_status_lbl=QLabel(),
            exp_docs_state_counts_lbl=QLabel(),
            exp_docs_gross_lbl=QLabel(),
            exp_docs_vat_lbl=QLabel(),
            exp_docs_net_lbl=QLabel(),
            exp_docs_unallocated_lbl=QLabel(),
            exp_docs_review_lbl=QLabel(),
            exp_date_from=SimpleNamespace(text=lambda: "2026-11-01"),
            exp_date_to=SimpleNamespace(text=lambda: "2026-11-30"),
            exp_filter_object=QComboBox(),
            exp_filter_unallocated=QCheckBox(),
            exp_filter_status=QComboBox(),
            table_supplier_documents=QTableWidget(0, 8),
            _expense_money_label=self.desktop.SmartReportApp._expense_money_label,
        )
        host.exp_filter_object.addItem("Все объекты", None)
        host.exp_filter_status.addItem("Все состояния", "all")
        host._local_incoming_expense_documents = lambda date_from=None, date_to=None: (
            self.desktop.SmartReportApp._local_incoming_expense_documents(
                host, date_from, date_to))
        host._expense_register_snapshot = lambda date_from=None, date_to=None, fresh=False: (
            self.desktop.SmartReportApp._expense_register_snapshot(
                host, date_from, date_to, fresh))

        self.desktop.SmartReportApp.load_expense_documents(host)

        self.assertEqual(host.table_supplier_documents.rowCount(), 0)
        self.assertIn("Входящих вне выбранного периода: 3",
                      host.exp_docs_state_counts_lbl.text())
        self.assertIn("3 входящих вне периода", host.exp_api_status_lbl.text())
        self.assertIn("Измените период", host.exp_api_status_lbl.text())

    def test_expense_register_state_colors_follow_saved_state_and_both_themes(self):
        import tempfile
        from pathlib import Path

        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QImage, QPainter
        from PyQt6.QtWidgets import QCheckBox, QComboBox, QLabel

        from desktop_theme import BACKGROUND_ROLE, ThemeManager

        documents = [
            {"id": "unassigned", "verification_status": "requires_review",
             "gross_amount": "125.00", "currency_code": "SEK", "allocations": []},
            {"id": "distributed", "verification_status": "requires_review",
             "gross_amount": "125.00", "currency_code": "SEK",
             "allocations": [{"destination": "company", "object_id": None,
                              "gross_amount": "125.00"}],
             "unallocated_gross": "0.00"},
            {"id": "accounted", "verification_status": "confirmed",
             "gross_amount": "125.00", "currency_code": "SEK",
             "allocations": [{"destination": "object", "object_id": 1,
                              "gross_amount": "125.00"}],
             "unallocated_gross": "0.00"},
            {"id": "later", "verification_status": "later",
             "gross_amount": "125.00", "currency_code": "SEK", "allocations": []},
            {"id": "ignored", "verification_status": "ignored", "supplier_name": "Bauhaus",
             "document_type": "order_confirmation", "exclusion_reason": "Не фактура",
             "gross_amount": "125.00", "currency_code": "SEK", "allocations": []},
            {"id": "error", "verification_status": "requires_review",
             "sync_state": "conflict", "sync_error": "Synthetic transfer error",
             "gross_amount": "125.00", "currency_code": "SEK",
             "allocations": [{"destination": "object", "object_id": 1,
                              "gross_amount": "125.00"}],
             "unallocated_gross": "0.00"},
            {"id": "pending", "verification_status": "requires_review",
             "sync_state": "pending", "gross_amount": "125.00", "currency_code": "SEK",
             "allocations": [{"destination": "company", "object_id": None,
                              "gross_amount": "125.00"}],
             "unallocated_gross": "0.00"},
        ]
        table = self.desktop.QTableWidget(0, 8)
        table.setItemDelegate(self.desktop.desktop_ui.ExpenseRegisterTableDelegate(table))
        table.setFixedSize(1100, 150)
        table.setAlternatingRowColors(True)
        table.show()
        self.addCleanup(table.close)
        self.addCleanup(table.deleteLater)
        host = SimpleNamespace(
            _expense_document_snapshot=lambda *_args, **_kwargs: (documents, True),
            _local_incoming_expense_documents=lambda *_args, **_kwargs: [],
            exp_api_status_lbl=QLabel(),
            exp_docs_state_counts_lbl=QLabel(),
            exp_docs_gross_lbl=QLabel(),
            exp_docs_vat_lbl=QLabel(),
            exp_docs_net_lbl=QLabel(),
            exp_docs_unallocated_lbl=QLabel(),
            exp_docs_review_lbl=QLabel(),
            exp_docs_state_legend_lbl=QLabel(),
            exp_date_from=SimpleNamespace(text=lambda: "2026-11-01"),
            exp_date_to=SimpleNamespace(text=lambda: "2026-11-30"),
            exp_filter_object=QComboBox(),
            exp_filter_unallocated=QCheckBox(),
            exp_filter_status=QComboBox(),
            table_supplier_documents=table,
            _expense_money_label=self.desktop.SmartReportApp._expense_money_label,
        )
        host.exp_filter_object.addItem("Все объекты", None)
        host.exp_filter_status.addItem("Активные", "active")
        host.exp_filter_status.addItem("Все состояния", "all")
        for name in (
                "_expense_register_snapshot", "_expense_register_status_key",
                "_expense_register_status_label", "_expense_register_row_tone",
                "_expense_register_has_transfer_error", "_expense_register_is_unallocated",
                "_expense_register_unallocated_amount", "_expense_distribution_label",
                "_update_expense_register_legend"):
            setattr(host, name, getattr(self.desktop.SmartReportApp, name))
        host._expense_register_snapshot = lambda *_args, **_kwargs: (documents, True)

        self.desktop.SmartReportApp.load_expense_documents(host)
        self.assertEqual(table.rowCount(), 6)
        self.assertFalse(any(table.item(row, 0).text() == "ignored" for row in range(table.rowCount())))
        host.exp_filter_status.setCurrentIndex(host.exp_filter_status.findData("all"))
        self.desktop.SmartReportApp.load_expense_documents(host)
        ignored_row = next(row for row in range(table.rowCount())
                           if table.item(row, 0).text() == "ignored")
        self.assertIn("Bauhaus", table.item(ignored_row, 2).text())
        self.assertIn("Подтверждение заказа", table.item(ignored_row, 2).text())
        self.assertEqual(table.item(ignored_row, 7).text(), "Исключён — Не фактура")
        self.assertIn("Причина исключения: Не фактура", table.item(ignored_row, 7).toolTip())

        previous = getattr(self.qt_app, "_smartreport_theme", None)
        previous_style = self.qt_app.styleSheet()
        with tempfile.TemporaryDirectory() as temp:
            manager = ThemeManager(
                self.qt_app, self.desktop.DARK_THEME_QSS, Path(temp) / "appearance.json")
            self.qt_app._smartreport_theme = manager
            manager.changed.connect(
                lambda *_: self.desktop.SmartReportApp._update_expense_register_legend(host))
            self.addCleanup(manager.deleteLater)
            self.addCleanup(
                lambda: setattr(self.qt_app, "_smartreport_theme", previous)
                if previous is not None else delattr(self.qt_app, "_smartreport_theme")
                if hasattr(self.qt_app, "_smartreport_theme") else None)
            self.addCleanup(
                lambda: previous.apply() if previous is not None
                else self.qt_app.setStyleSheet(previous_style))

            expected = {
                "light": {
                    "unassigned": "#fff0cf", "distributed": "#e4f2ff",
                    "accounted": "#e4efe9", "later": "#edf0f3",
                    "ignored": "#edf0f3", "error": "#f1e5e5",
                    "pending": "#e4f2ff", "selected": "#d6e5f0",
                },
                "dark": {
                    "unassigned": "#4a3b24", "distributed": "#26394b",
                    "accounted": "#1c4532", "later": "#30343b",
                    "ignored": "#30343b", "error": "#632c2c",
                    "pending": "#26394b", "selected": "#3182ce",
                },
            }

            def row_for(identity):
                return next(
                    row for row in range(table.rowCount())
                    if table.item(row, 0).text() == identity)

            for theme_name in ("light", "dark"):
                manager.name = theme_name
                manager.apply()
                self.desktop.SmartReportApp._update_expense_register_legend(host)
                self.desktop.SmartReportApp.load_expense_documents(host)
                self.qt_app.processEvents()

                self.assertIn("Назначение не выбрано", host.exp_docs_state_legend_lbl.text())
                self.assertIn("Распределено", host.exp_docs_state_legend_lbl.text())
                self.assertIn("Ошибка передачи", host.exp_docs_state_legend_lbl.text())
                for identity, color in expected[theme_name].items():
                    if identity == "selected":
                        continue
                    row = row_for(identity)
                    for column in range(1, table.columnCount()):
                        item = table.item(row, column)
                        wanted = color
                        if identity == "error" and column == 7:
                            wanted = expected[theme_name]["error"]
                        elif identity == "error":
                            wanted = expected[theme_name]["distributed"]
                        self.assertEqual(item.background().color().name(), wanted,
                                         (theme_name, identity, column))
                        original_color = expected["dark"][identity]
                        if identity == "error" and column == 7:
                            original_color = expected["dark"]["error"]
                        elif identity == "error":
                            original_color = expected["dark"]["distributed"]
                        self.assertEqual(item.data(BACKGROUND_ROLE).lower(), original_color)
                pending_status = table.item(row_for("pending"), 7)
                self.assertEqual(pending_status.text(), "Повторить передачу")
                self.assertIn("Ожидает подтверждения сервера", pending_status.toolTip())
                self.assertTrue(table.item(row_for("accounted"), 7).text().startswith("Учтён"))
                self.assertNotIn("Оплачен", table.item(row_for("accounted"), 7).text())

                selected_row = row_for("distributed")
                table.selectRow(selected_row)
                self.qt_app.processEvents()
                image = QImage(table.size(), QImage.Format.Format_ARGB32)
                image.fill(Qt.GlobalColor.transparent)
                painter = QPainter(image)
                table.render(painter)
                painter.end()
                cell = table.visualItemRect(table.item(selected_row, 1)).translated(
                    table.viewport().geometry().topLeft())
                selection_color = expected[theme_name]["selected"]
                selected_pixels = sum(
                    image.pixelColor(x, y).name() == selection_color
                    for y in range(cell.top() + 2, cell.bottom() - 1)
                    for x in range(cell.left() + 2, cell.right() - 1)
                )
                self.assertGreater(selected_pixels, 0, (theme_name, "selected row overlay"))

            scroll = table.verticalScrollBar()
            scroll.setValue(scroll.maximum())
            self.assertGreater(scroll.value(), 0)
            before_scroll = scroll.value()
            accounted = documents[1]
            accounted["verification_status"] = "confirmed"
            self.desktop.SmartReportApp.load_expense_documents(host)
            self.qt_app.processEvents()
            refreshed_row = row_for("distributed")
            self.assertEqual(
                table.item(refreshed_row, 1).background().color().name(),
                expected["dark"]["accounted"])
            self.assertEqual(scroll.value(), before_scroll)

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
