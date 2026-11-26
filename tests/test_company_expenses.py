import unittest
from contextlib import closing
from datetime import date
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4
from PyQt6.QtWidgets import QPushButton, QTableWidget

import test_catalog_sync as catalog
from company_expenses import (FUEL, ROAD, SOFTWARE, TAX, UNKNOWN, VEHICLE_TAX, report_shares,
                              review_proposals, section, suggest, totals)
from company_expenses_ui import ArticleReview, CompanyExpensesWidget, comparison_range
from expense_documents import migrate_invoice_net_schema


def example():
    return dict(id=10, sync_version=1, verification_status="confirmed", currency_code="SEK",
                supplier_name="St1", document_date=date.today().isoformat(), invoice_number="EXAMPLE",
                allocations=[dict(allocation_id=7, object_id=99, destination="object", gross_amount="100"),
                             dict(allocation_id=8, destination="company", net_amount="80", gross_amount="100", note="Diesel")])


class ReportingTests(unittest.TestCase):
    def test_company_shares_only_unknown_net_credit_currency_and_tax(self):
        doc = example()
        share = doc["allocations"][1]
        documents = [doc, dict(doc, verification_status="draft"), dict(doc, supporting_to_document_id=1),
                     dict(doc, allocations=[dict(share, destination="unselected")]),
                     dict(doc, allocations=[dict(share, net_amount=None)]),
                     dict(doc, allocations=[dict(share, net_amount="-20")])]
        rows = report_shares(documents)
        self.assertEqual(totals(rows, "net"), ({UNKNOWN: Decimal(60)}, {UNKNOWN: 1}))
        self.assertEqual(totals(rows, "gross"), ({UNKNOWN: Decimal(300)}, {}))
        self.assertEqual(totals(report_shares([dict(doc, currency_code=None)]), "gross"), ({}, {UNKNOWN: 1}))
        self.assertEqual(suggest(doc, share)[0], FUEL)
        self.assertEqual(suggest(dict(doc, recognized_text="Diesel", supplier_name="OpenAI"), {})[0], UNKNOWN)
        self.assertEqual(suggest({}, dict(note="Fordonsskatt"))[0], VEHICLE_TAX)
        self.assertEqual(section(VEHICLE_TAX), TAX)
        fora = dict(doc, allocations=[{}], supplier_name="Fora AB")
        self.assertEqual(section(suggest(fora, {})[0]), TAX)
        self.assertEqual(suggest(dict(fora, allocations=[{}, {}]), {})[0], UNKNOWN)
        self.assertEqual(suggest({}, dict(note="Trängselskatt"))[0], ROAD)
        self.assertEqual(suggest(dict(doc, allocations=[share], supplier_name="GitHub, Inc."), {})[0], SOFTWARE)

    def test_review_uses_real_ai_response_and_only_company_description(self):
        doc = example()
        doc["allocations"][1]["note"] = "ANONYM TJÄNST"
        calls = []
        def request(method, path, **kw):
            if method == "GET":
                return {"document": doc}
            calls.append(kw["json"]["groups"])
            return {"suggestions": [dict(id="1", category=SOFTWARE, evidence_lines=[1], confidence=.99)]}
        chosen = review_proposals(report_shares([doc]), request, Mock(), [])
        self.assertEqual(calls, [[dict(id="1", lines=["ANONYM TJÄNST"])]])
        self.assertEqual(chosen[10][8]["category"], SOFTWARE)
        self.assertNotIn("company_article", doc["allocations"][1])

    def test_equal_elapsed_months_and_leap_year(self):
        self.assertEqual(comparison_range(date(2024, 3, 1), date(2024, 3, 31), True),
                         (date(2024, 2, 1), date(2024, 2, 29)))
        self.assertEqual(comparison_range(date(2026, 1, 1), date(2026, 1, 4), True),
                         (date(2025, 12, 1), date(2025, 12, 4)))

    def test_company_ai_taxonomy_has_separate_provider_cache(self):
        import json
        import os
        import sqlite3
        from types import SimpleNamespace
        from unittest.mock import patch
        import assistant_api
        from expense_category_ai import request_categories
        from expense_categories import CATEGORY_OTHER
        groups = [dict(id="1", lines=["Fordonskostnad"])]
        replies = iter((CATEGORY_OTHER, VEHICLE_TAX))
        def post(_url, **kw):
            label = next(replies)
            item = dict(id="1", category=label, confidence=.99, evidence_lines=[1])
            schema = kw["json"]["text"]["format"]["schema"]
            self.assertIn(label, schema["properties"]["suggestions"]["items"]["properties"]["category"]["enum"])
            payload = json.dumps({"suggestions": [item]})
            return SimpleNamespace(status_code=200, json=lambda: dict(status="completed", output=[
                dict(type="message", content=[dict(type="output_text", text=payload)])]))
        with closing(sqlite3.connect(":memory:")) as conn, patch.dict(os.environ, OPENAI_API_KEY="synthetic"):
            assistant_api.ensure_schema(conn)
            conn.execute("UPDATE assistant_settings SET enabled=1")
            conn.commit()
            for company, expected in ((False, CATEGORY_OTHER), (True, VEHICLE_TAX), (True, VEHICLE_TAX)):
                result = request_categories(conn, {"user_id": 1}, dict(groups=groups, operation_id=str(uuid4())),
                                            post, company=company)
                self.assertEqual(result[0]["category"], expected)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM expense_category_ai_cache").fetchone()[0], 2)


class CompanyFlowTests(unittest.TestCase):
    setUpClass = classmethod(catalog.CatalogSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog.CatalogSyncTests.tearDownClass.__func__)
    setUp = catalog.CatalogSyncTests.setUp
    tearDown = catalog.CatalogSyncTests.tearDown
    _add_company = catalog.CatalogSyncTests._add_company

    def test_endpoint_atomic_review_preserves_money_and_survives_legacy_edit(self):
        with closing(self.server.get_db_connection()) as conn:
            migrate_invoice_net_schema(conn)
        headers = {"X-Sync-Key": "isolated-sync-test-key"}
        payload = dict(client_id=uuid4().hex, supplier_name="Test", invoice_number="11", currency_code="SEK",
                       document_date="2026-11-01", verification_status="confirmed", allocation_basis="net",
                       net_amount="100.00", vat_amount="25.00", gross_amount="125.00", amount_due="125.00",
                       rounding_amount="0.00", allocations=[
                           dict(destination="company", net_amount="80.00", vat_amount="20.00", note="diesel"),
                           dict(destination="company", net_amount="20.00", vat_amount="5.00", note="parkering")])
        client = self.server_client
        base = "/api/expense-documents"
        created = client.post(base, json=payload, headers=headers)
        self.assertEqual(created.status_code, 201, created.get_json())
        doc = created.get_json()["document"]
        path = base + "/company-articles"
        changes = [dict(document_id=doc["id"], expected_version=doc["sync_version"], allocation_id=s["allocation_id"],
                        article=a, evidence="Выбрано вручную") for s, a in zip(doc["allocations"], (FUEL, ROAD), strict=True)]
        self.assertEqual(client.post(path, json={"changes": changes}).status_code, 403)
        doc_path = base + "/" + str(doc["id"])
        self.assertEqual(client.post(path, json={"changes": [changes[0], dict(changes[1], expected_version=99)]}, headers=headers).status_code, 409)
        self.assertFalse(client.get(doc_path, headers=headers).get_json()["document"]["allocations"][0]["company_article"])
        result = client.post(path, json={"changes": changes}, headers=headers)
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual(client.post(path, json={"changes": changes}, headers=headers).status_code, 409)
        current = client.get(doc_path, headers=headers).get_json()["document"]
        payload.update(expected_version=current["sync_version"], document_comment="Проверено")
        saved = client.put(doc_path, json=payload, headers=headers)
        self.assertEqual(saved.status_code, 200, saved.get_json())
        parts = saved.get_json()["document"]["allocations"]
        self.assertEqual([(p["company_article"], p["net_amount"]) for p in parts], [(FUEL, "80.00"), (ROAD, "20.00")])

    def test_qt_overview_review_and_open_original_document_identity(self):
        from PyQt6.QtTest import QTest
        reveal = Mock()
        doc = example()
        rows = report_shares([doc])
        editor = ArticleReview(rows, {10: {8: dict(category=FUEL, evidence="Diesel")}}, None)
        self.assertEqual(editor.changes()[0]["allocation_id"], 8)
        editor.entries[0][0].setChecked(False)
        self.assertEqual(editor.changes(), [])
        editor.close()
        doc["allocations"][1]["company_article"] = FUEL
        tax = dict(doc, id=20, allocations=[dict(doc["allocations"][1], company_article=VEHICLE_TAX, net_amount="500")])
        window = CompanyExpensesWidget(lambda *_: [doc, tax], Mock(), Mock(), reveal, self.desktop.ObjectOverviewTask)
        window.resize(900, 600)
        window.show()
        window.reload()
        for _ in range(100):
            QTest.qWait(10)
            if window.loaded:
                break
        self.assertTrue(window.loaded)
        self.assertEqual(window.chart.values, {FUEL: Decimal(80)})
        window.select_article(FUEL)
        window.documents.selectRow(0)
        window.open_button.click()
        self.assertEqual(reveal.call_args.args[0]["id"], 10)
        window.sections.setCurrentIndex(1)
        self.assertEqual(window.chart.values, {VEHICLE_TAX: Decimal(500)})
        window.close()

    def test_article_review_hover_does_not_select_and_category_pick_enables_original(self):
        from PyQt6.QtTest import QTest
        from PyQt6.QtCore import QPoint
        doc = example()
        doc["has_original"] = True
        rows = report_shares([doc])
        opened = Mock()
        editor = ArticleReview(rows, {10: {8: dict(category=FUEL, evidence="Diesel")}}, None, opened)
        table = editor.findChild(QTableWidget)
        editor.show()
        QTest.qWait(20)
        table.setCurrentCell(-1, -1)
        QTest.mouseMove(table.viewport(), QPoint(400, 30))
        self.assertEqual(table.currentRow(), -1)
        editor.entries[0][1].setCurrentText(UNKNOWN)
        self.assertEqual(table.currentRow(), 0)
        original = next(button for button in editor.findChildren(QPushButton)
                        if button.text() == "Открыть оригинал")
        self.assertTrue(original.isEnabled())
        original.click()
        opened.assert_called_once_with(doc)
        editor.close()

    def test_fortnox_selection_survives_pointer_crossing_other_invoices(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        docs = [dict(example(), id=i, supplier_name=name, has_original=True)
                for i, name in enumerate(("Fortnox AB", "Fora AB", "Parkeringsservice Svenska AB"), 1)]
        opened = Mock()
        editor = ArticleReview(report_shares(docs), {}, None, opened)
        self.addCleanup(editor.close)
        editor.show()
        QTest.qWait(20)
        table = editor.findChild(QTableWidget)
        viewport = table.viewport()
        def point(row):
            return table.visualItemRect(table.item(row, 1)).center()
        QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=point(0))
        for row in (1, 2):
            QTest.mouseMove(viewport, point(row))
        self.assertEqual(table.currentRow(), 0)
        original = next(b for b in editor.findChildren(QPushButton) if b.text() == "Открыть оригинал")
        QTest.mouseClick(original, Qt.MouseButton.LeftButton)
        opened.assert_called_once_with(docs[0])
        QTest.mouseClick(table.cellWidget(2, 5), Qt.MouseButton.LeftButton)
        self.assertEqual(opened.call_args.args, (docs[2],))

    def test_review_column_drag_maximize_and_control_text(self):
        from types import SimpleNamespace
        from PyQt6.QtCore import QPoint, Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QHeaderView, QStyle, QStyleOptionComboBox, QStyleOptionButton
        import desktop_theme
        from company_expenses import ARTICLES
        self.addCleanup(self.qt_app.setStyleSheet, self.qt_app.styleSheet())
        themed = SimpleNamespace(name="light", dark_style=self.desktop.DARK_THEME_QSS)
        self.qt_app.setStyleSheet(desktop_theme.ThemeManager.stylesheet(themed))
        doc = dict(example(), has_original=True)
        editor = ArticleReview(report_shares([doc] * 29), {}, None, Mock())
        self.addCleanup(editor.close)
        editor.resize(1280, 640)
        editor.show()
        QTest.qWait(25)
        table = editor.findChild(QTableWidget)
        header = table.horizontalHeader()
        self.assertTrue(editor.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint)
        self.assertTrue(all(header.sectionResizeMode(i) == QHeaderView.ResizeMode.Interactive
                            for i in range(table.columnCount())))
        combo = table.cellWidget(0, 3)
        opt = QStyleOptionComboBox()
        combo.initStyleOption(opt)
        text_rect = combo.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, opt,
                                                 QStyle.SubControl.SC_ComboBoxEditField, combo)
        self.assertGreaterEqual(text_rect.width(), max(combo.fontMetrics().horizontalAdvance(a) for a in ARTICLES) + 4)
        button = table.cellWidget(0, 5)
        bopt = QStyleOptionButton()
        button.initStyleOption(bopt)
        rect = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, bopt, button)
        self.assertGreaterEqual(rect.width(), button.fontMetrics().horizontalAdvance(button.text()))
        for col in (3, 5):
            table.horizontalScrollBar().setValue(table.horizontalScrollBar().maximum() if col == 5 else 0)
            before = table.columnWidth(col)
            edge = QPoint(header.sectionViewportPosition(col) + before - 2, header.height() // 2)
            end = edge - QPoint(18, 0)
            QTest.mousePress(header.viewport(), Qt.MouseButton.LeftButton, pos=edge)
            QTest.mouseMove(header.viewport(), end)
            QTest.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, pos=end)
            self.assertEqual(table.columnWidth(col), before - 18)
        editor.showMaximized()
        QTest.qWait(10)
        self.assertTrue(editor.isMaximized())
        editor.showNormal()
        editor.resize(800, 500)
        QTest.qWait(10)
        bar = table.horizontalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum())
        self.assertLessEqual(header.sectionViewportPosition(5) + table.columnWidth(5), table.viewport().width())
