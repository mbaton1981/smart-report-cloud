import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QBoxLayout, QScrollArea, QWidget

import test_incoming_integration as integration
import desktop_theme
from expense_documents import ExtractionResult
from offline_review import ReviewError


class IncomingDisplayTests(unittest.TestCase):
    setUpClass = classmethod(integration.IncomingIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(integration.IncomingIntegrationTests.tearDownClass.__func__)
    _add_company = integration.IncomingIntegrationTests._add_company
    setUp = integration.IncomingIntegrationTests.setUp
    stage = integration.IncomingIntegrationTests.stage
    approve = integration.IncomingIntegrationTests.approve
    request = integration.IncomingIntegrationTests.request
    sync = integration.IncomingIntegrationTests.sync

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

    def open_window(self, document_id=None, *, select_first=True):
        from offline_review_ui import OfflineReviewWindow
        window = OfflineReviewWindow(self.store, integrated=True, document_id=document_id)
        self.addCleanup(window.close)
        window.show()
        if document_id is None and select_first and window.selector.count():
            default_index = window.selector.findData(1)
            if default_index >= 0:
                window.selector.setCurrentIndex(default_index)
        self.qt_app.processEvents()
        return window

    def test_currency_suggestion_prefills_existing_unreviewed_invoice(self):
        fields = self.store.base(1)
        fields.update(currency_code="UNKNOWN", gross_amount="125.00",
                      raw_text="Invoice\nBill to\nExample AB\n123 45 Stockholm\nSweden\nTotal 125.00kr")
        with self.store.conn:
            self.store.conn.execute("UPDATE documents SET base_json=? WHERE id=1", (json.dumps(fields),))
            self.store.conn.execute("UPDATE revisions SET payload_json=json_set(payload_json, '$.currency_code', 'UNKNOWN') "
                                    "WHERE document_id=1 AND revision=0")
        window = self.open_window(document_id=1)
        self.assertEqual(window.inputs["currency_code"].text(), "SEK")
        self.assertIn("Sweden", window.origins["currency_code"].text())
        self.assertTrue(window.origins["currency_code"].isVisible())
        self.assertEqual(self.store.latest(1)[1]["currency_code"], "UNKNOWN")
        self.assertIsNone(self.store.card(1))

    def test_project_marker_suggestion_shows_source_without_allocating_it(self):
        window = self.open_window(document_id=1)
        window.allocations.setRowCount(0)
        window.add_allocation({"gross_amount": "10970.27"})
        window.loaded_payload = window.payload()
        self.addCleanup(setattr, window, "loaded_payload", window.payload())
        before = (
            window.allocations.cellWidget(0, 0).currentData(),
            window.allocations.item(0, 1).text(),
        )
        evidence = [
            {"label": "Projektnamn", "value": "P1037", "normalized": "1037", "page": 1},
            {"label": "Rekvisitionsnr.", "value": "1037", "normalized": "1037", "page": 1},
        ]
        suggestions = [{
            "name": "Object p1037", "markning": "p1037",
            "reason": "Найдена маркировка p1037 в поле Projektnamn: P1037, стр. 1",
        }]
        proposal = {"parts": [], "warnings": [], "errors": [], "rounding": "0.00", "total": None}

        with patch.object(self.store, "allocation_proposal", return_value=proposal), \
                patch.object(window, "_object_marker_suggestions", return_value=(evidence, suggestions, None)):
            window.show_proposal()

        self.assertIn("Projektnamn", window.proposal_label.text())
        self.assertIn("Rekvisitionsnr.", window.proposal_label.text())
        self.assertIn("стр. 1", window.proposal_label.text())
        self.assertIn("Object p1037", window.proposal_label.text())
        self.assertEqual(
            (window.allocations.cellWidget(0, 0).currentData(), window.allocations.item(0, 1).text()),
            before,
        )

    def test_conflicting_project_markers_are_visible_for_reconciliation(self):
        window = self.open_window(document_id=1)
        evidence = [
            {"label": "Projektnamn", "value": "P1037", "normalized": "1037", "page": 1},
            {"label": "Rekvisitionsnr.", "value": "1022", "normalized": "1022", "page": 2},
        ]
        suggestions = [
            {"name": "Object p1037", "markning": "p1037", "reason": "стр. 1"},
            {"name": "Object p1022", "markning": "p1022", "reason": "стр. 2"},
        ]
        proposal = {"parts": [], "warnings": [], "errors": [], "rounding": "0.00", "total": None}

        with patch.object(self.store, "allocation_proposal", return_value=proposal), \
                patch.object(window, "_object_marker_suggestions", return_value=(evidence, suggestions, None)):
            window.show_proposal()

        self.assertIn("Найдены разные маркировки", window.proposal_label.text())
        self.assertIn("P1037", window.proposal_label.text())
        self.assertIn("1022", window.proposal_label.text())
        self.assertIn("стр. 1", window.proposal_label.text())
        self.assertIn("стр. 2", window.proposal_label.text())
        self.assertIn("автоматически не распределены", window.proposal_label.text())
        self.assertFalse(window.apply_proposal_button.isEnabled())

    def test_marker_only_proposal_selects_object_but_leaves_amount_manual_and_unsaved(self):
        self.host._expense_objects = [{
            "id": self.object_id, "name": "Isolated p1037 object",
            "markning": "p1037", "is_active": 1,
        }]
        window = self.open_window(document_id=1)
        window.original_pages = ()
        self.addCleanup(lambda: setattr(window, "loaded_payload", window.payload()))
        evidence = [
            {"label": "Rekvisitionsnr.", "value": "1037", "normalized": "1037", "page": 1},
        ]
        suggestions = [{
            "object_id": self.object_id, "name": "Isolated p1037 object",
            "markning": "p1037", "is_active": 1, "score": 100,
            "reason": "Найдена маркировка p1037 в поле Projektnamn: P1037, стр. 1",
        }]
        proposal = {
            "parts": [], "warnings": [],
            "errors": ["Подтверждённые суммы заказов не распознаны."],
            "rounding": "0.00", "total": None,
        }

        with patch.object(self.store, "allocation_proposal", return_value=proposal), \
                patch.object(window, "_object_marker_suggestions",
                             return_value=(evidence, suggestions, None)):
            window.show_proposal()
            self.assertTrue(window.apply_proposal_button.isEnabled())
            existing_amount = window.allocations.item(0, 1).text()
            self.assertEqual(existing_amount, "125.00")
            self.assertIn("сумму части вручную", window.proposal_label.text())
            self.assertIn("не считается суммой этой доли", window.proposal_label.text())
            window.apply_proposal_button.click()
            window.amount_choices.addItem("Сумма документа", "125.00")
            window.amount_choices.setCurrentIndex(window.amount_choices.count() - 1)

        self.assertEqual(window.allocations.rowCount(), 1)
        self.assertEqual(window.allocations.cellWidget(0, 0).currentData(), self.object_id)
        self.assertEqual(window.allocations.item(0, 1).text(), existing_amount)
        self.assertEqual(window.inputs["gross_amount"].text(), "125.00")
        self.assertIsNone(self.store.card(1))
        self.assertEqual(self.store.latest(1)[0], 0)
        self.assertIn("Сумма 125.00 сохранена", window.status.text())
        self.assertIn("не сохранено и не учтено", window.status.text())

    def test_full_invoice_proposal_works_without_an_explicit_rounding_line(self):
        self.host._expense_objects = [{
            "id": self.object_id, "name": "Isolated p1037 object",
            "markning": "p1037", "is_active": 1,
        }]
        window = self.open_window(document_id=1)
        evidence = [
            {"label": "Rekvisitionsnr.", "value": "1037", "normalized": "1037", "page": 1},
        ]
        suggestions = [{
            "object_id": self.object_id, "name": "Isolated p1037 object",
            "markning": "p1037", "is_active": 1, "score": 100,
            "reason": "Rekvisitionsnr. 1037, стр. 1",
        }]
        parsed_fields = {
            "net_amount": "100.00", "vat_amount": "25.00",
            "gross_amount": "125.00", "rounding_amount": None,
            "amount_due": "125.00", "prepaid": False,
            "field_sources": {
                "net_amount": [{"kind": "invoice_total", "page": 1}],
                "vat_amount": [{"kind": "invoice_total", "page": 1}],
                "amount_due": [{"kind": "invoice_total", "page": 1}],
            },
        }
        proposal = {"parts": [], "warnings": [], "errors": [], "rounding": "0.00", "total": None}

        with patch.object(self.store, "base", return_value={
            "document_type": "invoice", "document_subtype": None, "order_proposals": [],
        }), patch.object(self.store, "allocation_proposal", return_value=proposal), \
                patch.object(window, "_object_marker_suggestions",
                             return_value=(evidence, suggestions, None)), \
                patch("offline_review_ui.parse_invoice_pages", return_value=parsed_fields):
            window.original_page_texts = ("Faktura\nRekvisitionsnr. 1037\nFakturabelopp 125,00 SEK",)
            window.original_pages = ({"page": 1, "text": window.original_page_texts[0]},)
            window.show_proposal()

        self.assertIn("Вся фактура на Isolated p1037 object — 125,00 SEK с НДС",
                      window.apply_proposal_button.text())
        self.assertIn("округление в документе не указано", window.proposal_label.text())
        self.assertNotIn("Округление документа: 0.00 SEK", window.proposal_label.text())
        self.assertEqual(self.store.latest(1)[0], 0)
        self.assertIsNone(self.store.card(1))

    def test_requisition_only_full_invoice_saves_syncs_and_reports_once(self):
        from PyQt6.QtWidgets import QMessageBox

        self.host._expense_objects = [{
            "id": self.object_id, "name": "Isolated p1037 object",
            "markning": "p1037", "is_active": 1,
        }, {
            "id": self.object_id + 100000, "name": "Other object",
            "markning": "p1041", "is_active": 1,
        }]
        get_base = self.store.base

        def bauhaus_test_base(document_id):
            base = get_base(document_id)
            if document_id == 1:
                base.update(
                    net_amount="854.96", vat_amount="213.74", gross_amount="1068.70",
                    rounding_amount="0.00", amount_due="1068.70", order_proposals=[],
                )
            return base

        with patch.object(self.store, "base", side_effect=bauhaus_test_base):
            _, initial_review = self.store.latest(1)
            initial_review.update(
                net_amount="854.96", vat_amount="213.74", gross_amount="1068.70",
                rounding_amount="0.00", amount_due="1068.70",
            )
            self.store.conn.execute(
                "UPDATE revisions SET payload_json=? WHERE document_id=1 AND revision=0",
                (json.dumps(initial_review, ensure_ascii=False, sort_keys=True),),
            )
            self.store.conn.commit()
            window = self.open_window(document_id=1)
            self.addCleanup(lambda: setattr(window, "loaded_payload", window.payload()))
            self.assertEqual(window.loaded_review["amount_due"], "1068.70")
            self.assertEqual(window.inputs["rounding_amount"].text(), "0.00")
            window.original_page_texts = (
                "\n".join((
                    "Faktura", "Fakturanummer TEST-123", "Fakturadatum 2026-10-21",
                    "Rekvisitionsnr.: 1037",
                )),
                "\n".join((
                    "Fakturasammanställning", "Netto 854.96 SEK", "Moms 25 % 213.74 SEK",
                    "Avrundning 0.00 SEK", "Att betala 1068.70 SEK",
                )),
            )

            def cell(text, x, y, width=65):
                return {"text": text, "x0": x, "y0": y, "x1": x + width, "y1": y + 10}

            window.original_pages = ({
                "page": 2, "source": "synthetic-bauhaus-full-invoice.pdf",
                "text": (
                    "Fakturasammanställning\nArtiklar ex. moms\nMoms\nAvrundning\n"
                    "Fakturabelopp\nTotalsumma 854,96 SEK 213,74 SEK 0,00 SEK 1 068,70 SEK\n"
                    "Att betala 1 068,70 SEK"
                ),
                "elements": [
                    cell("Artiklar ex. moms", 266, 518, 87),
                    cell("Moms", 402, 518, 30),
                    cell("Avrundning", 463, 518, 64),
                    cell("Fakturabelopp", 534, 518, 78),
                    cell("Totalsumma", 32, 549, 58),
                    cell("854,96 SEK", 294, 549, 52),
                    cell("213,74 SEK", 379, 549, 48),
                    cell("0,00 SEK", 463, 549, 48),
                    cell("1 068,70 SEK", 534, 549, 62),
                    cell("Att betala 1 068,70 SEK", 484, 565, 98),
                ],
            },)
            evidence = [
                {"label": "Rekvisitionsnr.", "value": "1037", "normalized": "1037", "page": 1},
            ]
            suggestions = [{
                "object_id": self.object_id, "name": "Isolated p1037 object",
                "markning": "p1037", "is_active": 1, "score": 100,
                "reason": "Найдена маркировка p1037 в поле Rekvisitionsnr.: 1037, стр. 1",
            }]
            proposal = {
                "parts": [], "warnings": [],
                "errors": ["Подтверждённые суммы заказов не распознаны."],
                "rounding": "0.00", "total": None,
            }

            with patch.object(self.store, "allocation_proposal", return_value=proposal), \
                    patch.object(window, "_object_marker_suggestions",
                                 return_value=(evidence, suggestions, None)):
                self.assertEqual(window.allocations.rowCount(), 1)
                self.assertEqual(window.allocations.item(0, 1).text(), "1068.70")
                other_index = window.allocations.cellWidget(0, 0).findData(self.object_id + 100000)
                window.allocations.cellWidget(0, 0).setCurrentIndex(other_index)
                window.allocations.item(0, 1).setText("500.00")
                window.show_proposal()
                self.assertTrue(window.apply_proposal_button.isEnabled())
                self.assertIn("Найден объект Isolated p1037 object по реквизиции 1037, стр. 1.",
                              window.proposal_summary_label.text())
                self.assertIn("Вся фактура на Isolated p1037 object — 1 068,70 SEK с НДС",
                              window.apply_proposal_button.text())
                self.assertFalse(window.proposal_label.isVisible())
                self.assertIn("854.96 SEK", window.proposal_label.text())
                window.proposal_details_button.click()
                self.assertTrue(window.proposal_label.isVisible())

                with patch.object(QMessageBox, "question",
                                  return_value=QMessageBox.StandardButton.No) as question:
                    window.apply_proposal_button.click()
                replacement_text = question.call_args.args[2]
                self.assertIn("Строка 1: Other object (p1041) — 500,00 SEK", replacement_text)
                self.assertIn("Isolated p1037 object — 1 068,70 SEK", replacement_text)
                self.assertEqual(
                    window.allocations.cellWidget(0, 0).currentData(),
                    self.object_id + 100000,
                )
                self.assertEqual(window.allocations.item(0, 1).text(), "500.00")

                combo = window.allocations.cellWidget(0, 0)
                combo.setCurrentIndex(combo.findData("unselected"))
                window.allocations.item(0, 1).setText("1068.70")
                window.inputs["gross_amount"].setText("1068.70")
                window.apply_proposal_button.click()

            self.assertEqual(window.allocations.cellWidget(0, 0).currentData(), self.object_id)
            self.assertEqual(window.allocations.item(0, 1).text(), "1068.70")
            self.assertEqual(window.inputs["gross_amount"].text(), "1068.70")
            self.assertIsNone(self.store.card(1))
            self.assertEqual(self.store.latest(1)[0], 0)
            self.assertIn("Ничего не сохранено", window.status.text())

            window.checked.setChecked(True)
            payload = window.payload()
            payload["confirmed_type"] = "invoice"
            self.assertEqual(self.store.save(1, payload, 0), 1)
            window.loaded_payload = window.payload()
            window.close()

            reopened = self.open_window(document_id=1)
            revision, saved = self.store.latest(1)
            self.assertEqual(revision, 1)
            self.assertEqual(
                tuple(saved[key] for key in ("net_amount", "vat_amount", "rounding_amount", "amount_due")),
                ("854.96", "213.74", "0.00", "1068.70"),
            )
            self.assertEqual(reopened.allocations.rowCount(), 1)
            self.assertEqual(reopened.allocations.cellWidget(0, 0).currentData(), self.object_id)
            self.assertEqual(reopened.allocations.item(0, 1).text(), "1068.70")

            card = self.store.transfer(1, revision)
            self.assertEqual(self.sync()["synced"], 1, self.last_sync)
            documents = self.server_client.get(
                "/api/expense-documents",
                headers={"X-Sync-Key": "isolated-sync-test-key"},
            ).get_json()["documents"]
            self.assertEqual(len(documents), 1)
            synced = documents[0]
            self.assertEqual(synced["client_id"], card["client_id"])
            self.assertEqual(synced["verification_status"], "confirmed")
            self.assertEqual(
                tuple(synced[key] for key in ("net_amount", "vat_amount", "rounding_amount", "gross_amount")),
                ("854.96", "213.74", "0.00", "1068.70"),
            )
            self.assertEqual(len(synced["allocations"]), 1)
            self.assertEqual(synced["allocations"][0]["object_id"], self.object_id)
            self.assertEqual(
                tuple(synced["allocations"][0][key]
                      for key in ("net_amount", "vat_amount", "rounding_amount", "gross_amount")),
                ("854.96", "213.74", "0.00", "1068.70"),
                synced["allocations"],
            )

            conn = self.desktop.get_db_connection()
            object_row = conn.execute(
                "SELECT id, sync_id FROM objects WHERE id=?", (self.object_id,)
            ).fetchone()
            conn.close()
            self.assertIsNotNone(object_row)
            overview = self.desktop.SmartReportApp._read_object_overview(
                None, object_row["id"], object_row["sync_id"], self.object_id,
                None, None, documents, True, True,
            )
            self.assertEqual(overview["expense_count"], 1)
            self.assertEqual(overview["expense_sums"]["net_amount"], {"SEK": "854.96"})
            self.assertEqual(overview["expense_sums"]["vat_amount"], {"SEK": "213.74"})
            self.assertEqual(overview["expense_sums"]["gross_amount"], {"SEK": "1068.70"})

    def test_split_button_preserves_sum_and_allocation_breakdown_splits_vat(self):
        from decimal import Decimal
        from expense_documents import allocation_breakdown, validate_allocations

        window = self.open_window(document_id=1)
        self.addCleanup(lambda: setattr(window, "loaded_payload", window.payload()))
        original_amount = window.allocations.item(0, 1).text()
        window.add_allocation_button.click()
        self.assertEqual(window.allocations.rowCount(), 2)
        self.assertEqual(window.allocations.item(0, 1).text(), original_amount)
        self.assertEqual(window.allocations.item(1, 1).text(), "")

        parts = [
            {"object_id": self.object_id, "gross_amount": "500.00"},
            {"object_id": self.object_id + 1, "gross_amount": "568.70"},
        ]
        valid, _message = validate_allocations("1068.70", parts)
        self.assertTrue(valid)
        breakdown = allocation_breakdown("1068.70", "854.96", "213.74", parts)["allocations"]
        self.assertEqual(
            (sum(Decimal(part["net_amount"]) for part in breakdown),
             sum(Decimal(part["vat_amount"]) for part in breakdown)),
            (Decimal("854.96"), Decimal("213.74")),
        )
        self.assertNotEqual(breakdown[0]["vat_amount"], Decimal("213.74"))
        self.assertNotEqual(breakdown[1]["vat_amount"], Decimal("213.74"))
        valid, _message = validate_allocations(
            "1068.70",
            [{"object_id": self.object_id, "gross_amount": "1068.71"}],
        )
        self.assertFalse(valid)

    def test_marker_suggestion_is_blocked_when_any_original_page_is_unreadable(self):
        window = self.open_window(document_id=1)
        extraction = ExtractionResult(
            "P1037Projektnamn:\nInvoice header and details",
            "test", 1.0,
            ("P1037Projektnamn:\nInvoice header and details", ""),
        )

        with patch("offline_review_ui.extract_local_document", return_value=extraction):
            evidence, suggestions, warning = window._object_marker_suggestions()

        self.assertEqual([(item["value"], item["page"]) for item in evidence], [("P1037", 1)])
        self.assertEqual(suggestions, [])
        self.assertIn("страницах 2", warning)

    def test_partial_page_text_keeps_known_markings_visible_without_candidate(self):
        window = self.open_window(document_id=1)
        evidence = [
            {"label": "Projektnamn", "value": "P1037", "normalized": "1037", "page": 1},
            {"label": "Rekvisitionsnr.", "value": "1022", "normalized": "1022", "page": 2},
        ]
        proposal = {"parts": [], "warnings": [], "errors": [], "rounding": "0.00", "total": None}

        with patch.object(self.store, "allocation_proposal", return_value=proposal), \
                patch.object(
                    window, "_object_marker_suggestions",
                    return_value=(evidence, [], "На странице 3 не удалось проверить полный текст."),
                ):
            window.show_proposal()

        label = window.proposal_label.text()
        self.assertIn("Не удалось проверить все страницы", label)
        self.assertIn("P1037", label)
        self.assertIn("1022", label)
        self.assertIn("стр. 1", label)
        self.assertIn("стр. 2", label)
        self.assertNotIn("Кандидаты (не назначены)", label)

    def test_api_upload_does_not_offer_object_when_any_page_text_is_incomplete(self):
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        self.addCleanup(window.close)
        captured = []
        window._expense_object_candidates = lambda: [{
            "id": 14, "name": "Object p1037", "markning": "p1037", "is_active": 1,
        }]
        window._api_expense_document_review_dialog = (
            lambda *args, **kwargs: captured.append((args, kwargs))
        )
        extraction = ExtractionResult(
            "P1037Projektnamn:\nInvoice header and details", "test", 1.0,
            ("P1037Projektnamn:\nInvoice header and details", ""),
        )
        source = Path(self._case.name) / "incomplete-pages.pdf"

        with patch.object(self.desktop.QFileDialog, "getOpenFileName", return_value=(str(source), "")), \
                patch.object(self.desktop, "extract_local_document", return_value=extraction), \
                patch.object(self.desktop, "parse_document_text", return_value={"review_reasons": []}), \
                patch.object(self.desktop, "parse_project_order_allocations", return_value={
                    "orders": [], "warnings": [], "requires_review": False,
                }), \
                patch.object(self.desktop, "match_objects") as match_objects:
            self.desktop.SmartReportApp._api_upload_expense_document(window)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0][3], [])
        self.assertIn("страницах 2", captured[0][1]["marker_warning"])
        self.assertEqual(captured[0][1]["marker_evidence"][0]["value"], "P1037")
        match_objects.assert_not_called()

    def test_incoming_defaults_to_new_unprocessed_documents_without_preview(self):
        revision = self.approve(2)
        self.store.transfer(2, revision)
        window = self.open_window(select_first=False)

        visible_ids = [window.selector.itemData(i) for i in range(window.selector.count())]
        self.assertEqual(visible_ids, [3, 2, 1])
        self.assertIsNone(window.selector.currentData())
        self.assertIsNone(window.document_id)
        self.assertIsNone(window.gallery)
        self.assertTrue(window.empty_state.isVisible())
        self.assertIn("Выберите документ", window.empty_state.text())

        window.show_all_states.setChecked(True)
        self.assertEqual(
            [window.selector.itemData(i) for i in range(window.selector.count())], [3, 2, 1])
        self.assertIsNone(window.selector.currentData())

    def test_long_amount_evidence_does_not_force_horizontal_form_scrolling(self):
        window = self.open_window(document_id=1)
        costs = dict(self.store.cost_fields(1))
        evidence = " ".join(["recognized source detail"] * 30)
        costs["amount_candidates"] = [{
            "amount": "1072.00",
            "evidence": [{"page": 1, "text": evidence}],
        }]
        with patch.object(self.store, "cost_fields", return_value=costs):
            window.select_document()
        window.resize(1450, 940)
        self.qt_app.processEvents()

        scroll = window.findChild(QScrollArea)
        self.assertIsNotNone(scroll)
        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
        self.assertEqual(
            window.amount_choices.itemData(1, Qt.ItemDataRole.ToolTipRole),
            f"стр. 1: {evidence}",
        )

    def test_queue_reasons_are_combined_and_totals_keep_currencies_separate(self):
        revision, fields = self.store.latest(1)
        fields.update(gross_amount=None, currency_code="SEK", original_checked=False,
                      allocations=[])
        self.store.save(1, fields, revision)
        revision, fields = self.store.latest(2)
        fields.update(currency_code="EUR", gross_amount="100.00", original_checked=True,
                      allocations=[{"destination": "company", "object_id": None,
                                    "gross_amount": "25.00"}])
        self.store.save(2, fields, revision)
        window = self.open_window(select_first=False)
        self.assertEqual(sum(window.selector.itemData(i) == 1
                             for i in range(window.selector.count())), 1)
        first = window.selector.itemText(window.selector.findData(1))
        self.assertIn("Не определена сумма", first)
        self.assertIn("Не выбрано назначение", first)
        self.assertIn("сверить данные", first)
        second = window.selector.itemText(window.selector.findData(2))
        self.assertIn("Осталось распределить 75.00 EUR", second)
        self.assertIn("EUR", window.queue_summary.text())
        self.assertIn("SEK", window.queue_summary.text())
        self.assertIn("итог неполный", window.queue_summary.text())

    def test_later_preserves_edits_and_moves_to_another_document(self):
        from PyQt6.QtWidgets import QMessageBox
        window = self.open_window(document_id=1)
        window.inputs["supplier_name"].setText("Saved for later")
        with patch("offline_review_ui.QMessageBox.question",
                   return_value=QMessageBox.StandardButton.Cancel):
            window.set_action("later")
        self.assertEqual(window.document_id, 1)
        self.assertIsNone(self.store.action(1)[1])
        with patch("offline_review_ui.QMessageBox.question",
                   return_value=QMessageBox.StandardButton.Save):
            window.set_action("later")
        self.assertEqual(self.store.action(1)[1], "later")
        self.assertEqual(self.store.latest(1)[1]["supplier_name"], "Saved for later")
        self.assertNotEqual(window.document_id, 1)
        self.assertGreaterEqual(window.selector.findData(1), 0)
        self.assertIsNone(self.store.card(1))

    def test_failed_later_action_does_not_forget_discarded_form_edits(self):
        from PyQt6.QtWidgets import QMessageBox
        window = self.open_window(document_id=1)
        window.inputs["supplier_name"].setText("Still visible")
        with patch("offline_review_ui.QMessageBox.question",
                   return_value=QMessageBox.StandardButton.Discard), \
                patch.object(self.store, "set_action", side_effect=ReviewError("action failed")), \
                patch("offline_review_ui.QMessageBox.warning"):
            window.set_action("later")
        self.assertEqual(window.document_id, 1)
        self.assertNotEqual(window.payload(), window.loaded_payload)
        self.assertEqual(window.inputs["supplier_name"].text(), "Still visible")
        window.loaded_payload = window.payload()

    def test_confirm_moves_on_success_but_not_on_error(self):
        window = self.open_window(document_id=1)
        window.checked.setChecked(True)
        window.allocations.cellWidget(0, 0).setCurrentIndex(
            window.allocations.cellWidget(0, 0).findData(self.object_id))
        with patch.object(self.store, "transfer", side_effect=ReviewError("isolated failure")), \
                patch("offline_review_ui.QMessageBox.warning"):
            window.confirm_transfer()
        self.assertEqual(window.document_id, 1)
        self.assertIsNone(self.store.card(1))
        window.confirm_transfer()
        self.assertIsNotNone(self.store.card(1))
        self.assertEqual(window.document_id, 1)
        self.assertFalse(window.transfer_button.isEnabled())
        self.assertTrue(window.next_button.isVisible())
        window.next_button.click()
        self.assertNotEqual(window.document_id, 1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM register").fetchone()[0], 1)

    def test_reload_error_is_not_an_empty_queue(self):
        window = self.open_window(document_id=1)
        with patch.object(self.store, "documents", side_effect=ReviewError("read failed")), \
                patch("offline_review_ui.QMessageBox.warning") as warning:
            window.reload_documents()
        warning.assert_called_once()
        self.assertEqual(window.document_id, 1)
        self.assertIn("read failed", window.status.text())
        self.assertGreater(window.selector.count(), 0)

    def test_original_load_error_remains_visible_and_blocks_confirmation(self):
        with patch.object(self.store, "original", side_effect=ReviewError("original unreadable")):
            window = self.open_window(document_id=1)
        self.assertIn("original unreadable", window.status.text())
        self.assertFalse(window.transfer_button.isEnabled())
        self.assertIsNone(self.store.card(1))

    def test_registered_document_opens_explicitly_even_when_accounted(self):
        revision = self.approve(2)
        self.store.transfer(2, revision)
        window = self.open_window(document_id=2)

        self.assertEqual(window.selector.currentData(), 2)
        self.assertEqual(window.document_id, 2)
        self.assertIsNotNone(window.gallery)
        self.assertFalse(window.empty_state.isVisible())

    def test_returning_to_empty_selection_allows_switching_to_another_document(self):
        window = self.open_window(document_id=1)
        window.selector.setCurrentIndex(-1)
        self.assertIsNone(window.document_id)
        self.assertTrue(window.empty_state.isVisible())
        window.inputs["supplier_name"].setText("not saved")
        window.show_all_states.setChecked(True)
        window.selector.setCurrentIndex(window.selector.findData(2))
        self.assertEqual(window.document_id, 2)
        self.assertNotEqual(window.inputs["supplier_name"].text(), "not saved")

    def test_explicit_navigation_preserves_unsaved_card_until_confirmed(self):
        from PyQt6.QtWidgets import QMessageBox

        window = self.open_window(document_id=1)
        host = QWidget()
        self.addCleanup(host.deleteLater)
        host._incoming_viewer = window
        window.inputs["supplier_name"].setText("Unsaved correction")
        with patch("offline_review_ui.QMessageBox.question",
                   return_value=QMessageBox.StandardButton.Cancel):
            self.desktop.SmartReportApp.open_incoming_documents(host, 2)
        self.assertEqual(window.document_id, 1)
        self.assertEqual(window.selector.currentData(), 1)
        self.assertEqual(window.inputs["supplier_name"].text(), "Unsaved correction")

        with patch("offline_review_ui.QMessageBox.question",
                   return_value=QMessageBox.StandardButton.Discard):
            self.desktop.SmartReportApp.open_incoming_documents(host, 2)
        self.assertEqual(window.document_id, 2)
        self.assertEqual(window.selector.currentData(), 2)
        self.assertNotEqual(window.inputs["supplier_name"].text(), "Unsaved correction")

        window.inputs["supplier_name"].setText("Saved correction")
        with patch("offline_review_ui.QMessageBox.question",
                   return_value=QMessageBox.StandardButton.Save):
            self.desktop.SmartReportApp.open_incoming_documents(host, 1)
        self.assertEqual(window.document_id, 1)
        self.assertEqual(self.store.latest(2)[1]["supplier_name"], "Saved correction")

        with patch("app.QMessageBox.warning") as warning:
            self.desktop.SmartReportApp.open_incoming_documents(host, 99999)
        warning.assert_called_once()
        self.assertEqual(window.document_id, 1)
        self.assertEqual(window.selector.currentData(), 1)

    def test_expense_mail_reminder_uses_both_themes_and_does_not_mark_failed_check(self):
        from datetime import date, datetime, timezone
        import folder_mail
        from desktop_theme import ThemeManager

        entry_calls = []
        tab_host = SimpleNamespace(
            load_dropdowns=lambda **_kwargs: None,
            load_expense_document_filters=lambda: None,
            load_expense_documents=lambda: None,
            exp_document_tabs=SimpleNamespace(currentIndex=lambda: 0),
            _maybe_show_mail_reminder=lambda: entry_calls.append(True),
        )
        self.desktop.SmartReportApp.on_tab_changed(tab_host, 3)
        self.assertEqual(entry_calls, [True])

        host = QWidget()
        host.tab_expenses = QWidget()
        host.tab_object_expenses = QWidget()
        mail_check_calls = []
        host.open_mail_queue = lambda *_args: mail_check_calls.append(True)
        host._format_mail_check_time = lambda value: (
            self.desktop.SmartReportApp._format_mail_check_time(host, value))
        methods = (
            "upload_expense_document", "open_manual_document_dialog",
            "open_incoming_documents", "reprocess_selected_document", "calculate_allocation_vat",
            "review_selected_document", "open_selected_document_original",
            "assign_selected_expense_document", "expense_detail_primary_action",
            "delete_selected_expense_document", "_update_expense_register_legend",
            "_on_expense_view_changed", "load_expense_document_filters", "load_expense_documents",
            "load_object_expense_overview", "_update_expense_detail", "_on_expense_row_changed",
        )
        for name in methods:
            setattr(host, name, lambda *_args, **_kwargs: None)
        host._expense_original_archive_button = self.desktop.SmartReportApp._expense_original_archive_button.__get__(host)
        previous = getattr(self.qt_app, "_smartreport_theme", None)
        previous_style = self.qt_app.styleSheet()
        with tempfile.TemporaryDirectory() as temp:
            manager = ThemeManager(
                self.qt_app, self.desktop.DARK_THEME_QSS, Path(temp) / "appearance.json")
            self.qt_app._smartreport_theme = manager
            self.desktop.SmartReportApp.setup_expenses_tab(host)
            self.addCleanup(manager.deleteLater)
            self.addCleanup(
                lambda: setattr(self.qt_app, "_smartreport_theme", previous)
                if previous is not None else delattr(self.qt_app, "_smartreport_theme")
                if hasattr(self.qt_app, "_smartreport_theme") else None)
            self.addCleanup(lambda: self.qt_app.setStyleSheet(previous_style))

            for name, background in (("dark", "#26394b"), ("light", "#e4f2ff")):
                manager.name = name
                manager.apply()
                self.assertIn(background, host.mail_reminder_banner.styleSheet())
            window = self.open_window(select_first=False)
            self.assertTrue(window.empty_state.isVisible())
            for name, background in (("dark", "#242933"), ("light", "#f7f9fb")):
                manager.name = name
                manager.apply()
                self.assertIn(background, window.empty_state.styleSheet())
            self.assertEqual(host.mail_reminder_title.text(), "Проверить новые фактуры в почте?")
            self.assertEqual(host.mail_reminder_check.text(), "Проверить")
            self.assertEqual(host.mail_reminder_later.text(), "Позже")
            self.assertIn("Проверить почту", host.exp_actions)
            host.mail_reminder_check.click()
            self.assertEqual(mail_check_calls, [True])

            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with patch.object(folder_mail, "load_reminder_state", return_value={
                    "last_successful_check": stamp,
                    "last_suggestion_date": date.today().isoformat()}) as load, \
                    patch.object(folder_mail, "save_reminder_state") as save:
                self.desktop.SmartReportApp._maybe_show_mail_reminder(host)
                load.assert_called_once_with()
                save.assert_not_called()
            self.assertIn(date.today().strftime("%d.%m.%Y"), host.mail_reminder_last_check.text())
            self.assertFalse(host.mail_reminder_banner.isHidden())

            host.mail_reminder_later.click()
            self.assertTrue(host.mail_reminder_banner.isHidden())
            with patch.object(folder_mail, "load_reminder_state", return_value={
                    "last_suggestion_date": date.today().isoformat()}) as load, \
                    patch.object(folder_mail, "save_reminder_state") as save:
                self.desktop.SmartReportApp._maybe_show_mail_reminder(host)
                load.assert_called_once_with()
                save.assert_not_called()
            self.assertFalse(host.mail_reminder_banner.isHidden())

            with patch.object(folder_mail, "save_reminder_state") as save:
                self.desktop.SmartReportApp.mail_import_completed(host, False, None)
                save.assert_not_called()
            self.assertIn("не изменено", host.mail_reminder_result.text())

            with patch.object(folder_mail, "save_reminder_state") as save:
                self.desktop.SmartReportApp.mail_import_completed(
                    host, True, {
                        "new_drafts": 4, "phase": "finished", "completed": 2,
                        "total": 2, "stopped": False,
                    })
                save.assert_called_once()
            self.assertIn("Новых документов: 4", host.mail_reminder_result.text())
            with patch.object(folder_mail, "save_reminder_state") as save:
                self.desktop.SmartReportApp.mail_import_completed(
                    host, True, {
                        "new_drafts": 1, "phase": "finished", "completed": 1,
                        "total": 2, "stopped": False,
                    })
                save.assert_not_called()
            self.assertIn("не изменено", host.mail_reminder_result.text())
        host.close()

    def background_refresh(self, window):
        self.host._incoming_viewer = window
        if not hasattr(self.host, "_expense_read_cache"):
            self.host._expense_read_cache = {}
        if not hasattr(self.host, "_expense_online"):
            self.host._expense_online = False
        if not hasattr(self.host, "_expense_read_force_refresh"):
            self.host._expense_read_force_refresh = False
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
        self.assertNotEqual(window.document_id, 1)
        self.assertIn("Разобрать позже", window.selector.itemText(window.selector.findData(1)))
        self.assertFalse(self.store.add_pdf(self.store.original(1).read_bytes(), "renamed.pdf",
                                           {"fields": self.fields}))
        window.reload_documents()
        window.selector.setCurrentIndex(window.selector.findData(1))
        self.assertEqual(window.inputs["supplier_name"].text(), "Manually checked supplier")
        self.assertEqual(self.store.history(1), history)
        window.inputs["supplier_name"].setText("Unsaved correction")
        self.background_refresh(window)
        self.assertEqual(window.inputs["supplier_name"].text(), "Unsaved correction")
        window.loaded_payload = window.payload()

    def test_explicit_automatic_refresh_preserves_original_id_and_audit(self):
        original = self.store.original(1).read_bytes()
        before = self.store.base(1)
        source_before = json.loads(self.store.conn.execute(
            "SELECT source_json FROM documents WHERE id=1").fetchone()[0])
        recognized = dict(self.fields, supplier_name="Correct issuer AB")
        recognized.pop("gross_amount")
        result = {"fields": recognized,
                  "state": "requires_review", "errors": []}
        self.store.record_recognition(1, result, replace_automatic=True)
        self.assertEqual(self.store.latest(1)[0], 0)
        self.assertEqual(self.store.latest(1)[1]["supplier_name"], "Correct issuer AB")
        self.assertEqual(self.store.latest(1)[1]["gross_amount"], self.fields["gross_amount"])
        self.assertEqual(self.store.original(1).read_bytes(), original)
        source_after = json.loads(self.store.conn.execute(
            "SELECT source_json FROM documents WHERE id=1").fetchone()[0])
        self.assertEqual(source_after["result"], source_before["result"])
        self.assertEqual(source_after["recognition_history"][0]["base"], before)
        self.assertIsNone(self.store.card(1))

    def test_rerecognition_preview_shows_changes_before_updating_unconfirmed_draft(self):
        from threading import Event

        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QMessageBox

        window = self.open_window(document_id=1)
        original = self.store.original(1).read_bytes()
        recognized = dict(
            self.fields,
            supplier_name="Sickla IF",
            recipient_name="Bygger och renoverar i Sthlm AB",
            supplier_org_number="814000-5797",
            net_amount="724.74",
            vat_amount="181.19",
            gross_amount="905.93",
            rounding_amount=None,
            amount_due="905.93",
            field_sources={
                "supplier_name": [{"page": 1, "text": "Sickla IF"}],
                "recipient_name": [{"page": 1, "text": "Bygger och renoverar i Sthlm AB"}],
                "net_amount": [{"page": 1, "text": "724,74SEK"}],
                "vat_amount": [{"page": 1, "text": "181,19SEK"}],
                "gross_amount": [{
                    "kind": "derived_from_stated_totals",
                    "operation": "net_plus_vat",
                    "sources": [{"page": 1, "text": "724,74SEK"}, {"page": 1, "text": "181,19SEK"}],
                }],
                "amount_due": [{"page": 1, "text": "905,93SEK"}],
            },
        )
        result = {"fields": recognized, "state": "requires_review", "errors": []}
        ui_ticked = Event()
        responsive_during_recognition = []

        def recognize_while_waiting_for_ui(_originals):
            responsive_during_recognition.append(ui_ticked.wait(2))
            return result

        with patch("invoice_processing.recognize_invoice", side_effect=recognize_while_waiting_for_ui) as recognize, \
                patch.object(QMessageBox, "question",
                             return_value=QMessageBox.StandardButton.Yes) as confirm, \
                patch.object(QMessageBox, "information"):
            QTimer.singleShot(10, ui_ticked.set)
            window.rerecognition_button.click()

        recognize.assert_called_once()
        self.assertEqual(responsive_during_recognition, [True])
        self.assertIn("Isolated Supplier AB", confirm.call_args.args[2])
        self.assertIn("Sickla IF", confirm.call_args.args[2])
        self.assertIn("стр. 1: нетто + НДС", confirm.call_args.args[2])
        self.assertNotIn("Указанное округление", confirm.call_args.args[2])
        self.assertNotIn("стр. None", confirm.call_args.args[2])
        self.assertEqual(self.store.latest(1)[1]["supplier_name"], "Sickla IF")
        self.assertIsNone(self.store.latest(1)[1]["rounding_amount"])
        self.assertIsNone(self.store.latest(1)[1]["confirmed_type"])
        self.assertFalse(self.store.latest(1)[1]["original_checked"])
        self.assertIsNone(self.store.card(1))
        self.assertEqual(self.store.original(1).read_bytes(), original)

    def test_rerecognition_apply_rejects_changes_since_preview(self):
        revision, payload = self.store.latest(1)
        base = self.store.base(1)
        changed_base = dict(base, raw_text="changed after preview")
        with self.store.conn:
            self.store.conn.execute("UPDATE documents SET base_json=? WHERE id=1",
                                    (json.dumps(changed_base, ensure_ascii=False),))

        with self.assertRaisesRegex(ReviewError, "изменился во время повторного распознавания"):
            self.store.record_recognition(
                1, {"fields": self.fields, "state": "requires_review"},
                replace_automatic=True, expected_revision=revision,
                expected_payload=payload, expected_base=base,
            )

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
            table_supplier_documents=QTableWidget(0, 9),
            _expense_money_label=self.desktop.SmartReportApp._expense_money_label,
            _expense_totals_label=self.desktop.SmartReportApp._expense_totals_label,
            _expense_selected_allocations=self.desktop.SmartReportApp._expense_selected_allocations,
            _expense_allocation_total=self.desktop.SmartReportApp._expense_allocation_total,
            _expense_document_identity=self.desktop.SmartReportApp._expense_document_identity,
            _update_expense_detail=lambda: None,
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

    def test_incoming_copy_of_synced_original_merges_into_single_register_row(self):
        import hashlib
        from PyQt6.QtWidgets import QLabel
        from test_mail_automation import pdf

        content = pdf("BEIJER synthetic original")
        self.store.add_pdf(content, "beijer.pdf", {
            "fields": dict(self.fields, supplier_name="BEIJER BYGGMATERIAL AB",
                           invoice_number="260414428128", document_date="2026-10-28"),
            "state": "requires_review"})
        document_id = self.store.documents()[-1]["id"]
        sha = hashlib.sha256(content).hexdigest()
        server_document = {
            "id": 38, "client_id": "server-client", "sync_version": 3,
            "supplier_name": "BEIJER BYGGMATERIAL AB", "invoice_number": "260414428128",
            "document_date": "2026-10-28", "currency_code": "SEK",
            "gross_amount": "139629.77", "vat_amount": "27926.00",
            "verification_status": "draft", "allocations": [],
            "attachments": [{"id": 1, "sha256": sha, "has_original": True}],
        }
        host = SimpleNamespace(
            _incoming_documents_root=lambda: self.store.root,
            _expense_objects=self.host._expense_objects,
            _expense_document_snapshot=lambda *_a, **_k: ([dict(server_document)], True),
            exp_api_status_lbl=QLabel(),
        )
        host._local_incoming_expense_documents = lambda date_from=None, date_to=None: (
            self.desktop.SmartReportApp._local_incoming_expense_documents(
                host, date_from, date_to))
        documents, online = self.desktop.SmartReportApp._expense_register_snapshot(host)
        self.assertTrue(online)
        rows = [document for document in documents
                if document.get("id") == 38
                or document.get("incoming_document_id") == document_id]
        self.assertEqual(len(rows), 1)
        merged = rows[0]
        self.assertEqual(merged["id"], 38)
        self.assertEqual(merged["incoming_document_id"], document_id)
        self.assertTrue(merged["incoming_local"])

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
        table = self.desktop.QTableWidget(0, 9)
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
            _expense_totals_label=self.desktop.SmartReportApp._expense_totals_label,
            _expense_selected_allocations=self.desktop.SmartReportApp._expense_selected_allocations,
            _expense_allocation_total=self.desktop.SmartReportApp._expense_allocation_total,
            _expense_document_identity=self.desktop.SmartReportApp._expense_document_identity,
            _update_expense_detail=lambda: None,
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
        self.assertEqual(table.item(ignored_row, 8).text(), "Исключён — Не фактура")
        self.assertIn("Причина исключения: Не фактура", table.item(ignored_row, 8).toolTip())

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
                        if identity == "error" and column == 8:
                            wanted = expected[theme_name]["error"]
                        elif identity == "error":
                            wanted = expected[theme_name]["distributed"]
                        self.assertEqual(item.background().color().name(), wanted,
                                         (theme_name, identity, column))
                        original_color = expected["dark"][identity]
                        if identity == "error" and column == 8:
                            original_color = expected["dark"]["error"]
                        elif identity == "error":
                            original_color = expected["dark"]["distributed"]
                        self.assertEqual(item.data(BACKGROUND_ROLE).lower(), original_color)
                pending_status = table.item(row_for("pending"), 8)
                self.assertEqual(pending_status.text(), "Повторить передачу")
                self.assertIn("Ожидает подтверждения сервера", pending_status.toolTip())
                self.assertTrue(table.item(row_for("accounted"), 8).text().startswith("Учтён"))
                self.assertNotIn("Оплачен", table.item(row_for("accounted"), 8).text())

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
        document_id = self.store.documents()[-1]["id"]
        row = window.selector.findData(document_id)
        label = window.selector.itemText(row)
        for value in ("Поставщик не распознан", "Дата не распознана", "Сумма не определена — нужна проверка"):
            self.assertIn(value, label)
        window.selector.setCurrentIndex(row)
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
        self.assertIn("Разобрать позже", window.selector.itemText(window.selector.findData(other)))
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
