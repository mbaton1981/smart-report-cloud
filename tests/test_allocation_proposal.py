import copy
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import test_incoming_integration as integration
from document_costs import propose_object_allocations
from expense_documents import match_objects, object_marking_matches, parse_project_order_allocations


def fixture():
    amounts = [("P1022", "473.86", "118.47", 1), ("P1035", "840.26", "210.07", 2),
               ("P1035", "1088.60", "272.15", 2), ("P1027", "1265.37", "316.35", 3),
               ("P1027", "560.79", "140.20", 3)]
    return {
        "document_type": "invoice", "supplier_name": "Isolated BEIJER example", "invoice_number": "TEST-PROJECT",
        "document_date": "2026-09-25", "currency_code": "SEK", "raw_text": "Faktura",
        "net_amount": "4228.88", "vat_amount": "1057.24", "gross_amount": "5286.12",
        "rounding_amount": "-0.12", "amount_due": "5286.00",
        "order_proposals": [
            {"order_id": str(index), "project_code": marker, "net_amount": net, "vat_amount": vat,
             "vat_items": [{"page": page, "net_amount": net, "vat_amount": vat}]}
            for index, (marker, net, vat, page) in enumerate(amounts)
        ],
    }


OBJECTS = [{"id": 5, "name": "Object A", "markning": "p1022", "is_active": 1},
           {"id": 13, "name": "Object B", "markning": "p1035", "is_active": 1},
           {"id": 12, "name": "Object C", "markning": "p1027", "is_active": 0}]

BEIJER_OBJECTS = [
    {"id": 613148, "name": "HOTEL Hazeliusbacken 20, på Djurgården",
     "markning": "p1039", "is_active": 1},
    {"id": 14, "name": "Tallvagen 11 1:116, Lidingö Kejill ÄTA",
     "markning": "p1037", "is_active": 1},
]


def beijer_fixture():
    orders = [
        ("8345806-01", "1039", "258.56", "64.64",
         [("41.60", "10.40", 1), ("216.96", "54.24", 1)]),
        ("8345807-01", None, "101.76", "25.44", [("101.76", "25.44", 1)]),
        ("8782319-01", "P1037", "4182.96", "1045.74",
         [("1845.36", "461.34", 2), ("345.60", "86.40", 2), ("1992.00", "498.00", 2)]),
    ]
    return {
        "document_type": "invoice", "supplier_name": "BEIJER BYGGMATERIAL AB",
        "invoice_number": "260420323123", "document_date": "2026-11-05",
        "currency_code": "SEK", "raw_text": "Beijer order allocations",
        "net_amount": "4543.28", "vat_amount": "1135.82", "gross_amount": "5679.10",
        "rounding_amount": "-0.10", "amount_due": "5679.00",
        "order_proposals": [
            {"order_id": order_id, "project_code": marker, "net_amount": net, "vat_amount": vat,
             "vat_items": [{"item": str(index + 1), "net_amount": item_net,
                            "vat_amount": item_vat, "page": page}
                           for index, (item_net, item_vat, page) in enumerate(items)]}
            for order_id, marker, net, vat, items in orders
        ],
    }


class ProposalTests(unittest.TestCase):
    def test_groups_orders_preserves_net_vat_and_reconciles_stated_rounding(self):
        fields = fixture()
        before = copy.deepcopy(fields)
        result = propose_object_allocations(fields, OBJECTS)
        self.assertFalse(result["errors"])
        self.assertEqual([part["gross_amount"] for part in result["parts"]], ["592.32", "2411.02", "2282.66"])
        self.assertEqual([part["rounding"] for part in result["parts"]], ["-0.01", "-0.06", "-0.05"])
        self.assertEqual(result["total"], "5286.00")
        self.assertEqual([part["object_id"] for part in result["parts"]], [5, 13, None])
        self.assertTrue(result["parts"][2]["warning"].startswith("Объект неактивен"))
        self.assertEqual(fields, before)

    def test_beijer_9_autoselects_only_unique_active_objects_and_reconciles_rounding(self):
        result = propose_object_allocations(beijer_fixture(), BEIJER_OBJECTS)
        self.assertFalse(result["errors"])
        self.assertEqual(result["total"], "5679.00")
        self.assertEqual([part["gross_amount"] for part in result["parts"]],
                         ["323.19", "127.20", "5228.61"])
        self.assertEqual([part["object_id"] for part in result["parts"]], [613148, None, 14])
        self.assertEqual(result["parts"][1]["warning"],
                         "Неизвестная маркировка — выберите объект или общие расходы")

    def test_unrecognized_order_marker_is_visible_but_never_auto_assigned(self):
        fields = beijer_fixture()
        fields["order_proposals"] = [dict(fields["order_proposals"][0], project_code=None,
                                          original_marker="ROMAN")]
        fields["net_amount"] = "258.56"
        fields["vat_amount"] = "64.64"
        fields["rounding_amount"] = "0.00"
        result = propose_object_allocations(fields, BEIJER_OBJECTS)
        self.assertFalse(result["errors"])
        self.assertEqual(result["parts"][0]["marker"], "ROMAN")
        self.assertIsNone(result["parts"][0]["object_id"])
        self.assertEqual(result["parts"][0]["warning"],
                         "Маркировка «ROMAN» не распознана — выберите вручную")

    def test_unknown_and_duplicate_markers_never_select_random_object(self):
        for objects in (OBJECTS[:2], [*OBJECTS, {**OBJECTS[2], "id": 99}]):
            result = propose_object_allocations(fixture(), objects)
            self.assertIsNone(result["parts"][2]["object_id"])
            self.assertTrue(result["parts"][2]["warning"])

    def test_equivalent_markings_group_across_pages_and_select_unique_active_object(self):
        fields = fixture()
        fields["order_proposals"][2]["project_code"] = "10-35"
        fields["order_proposals"][2]["vat_items"][0]["page"] = 3
        result = propose_object_allocations(fields, OBJECTS)
        part = next(item for item in result["parts"] if item["marker"] == "P1035")
        self.assertEqual(part["pages"], [2, 3])
        self.assertEqual([option["id"] for option in part["object_options"]], [13])
        self.assertEqual(part["object_id"], 13)

    def test_markings_match_format_variants_only_in_labeled_context(self):
        variants = ("P1039", "P10-39", "1039", "10-39")
        self.assertTrue(all(object_marking_matches(value, "P1039") for value in variants))
        objects = [{"id": 7, "name": "HOTEL", "markning": "P1039", "is_active": 1}]
        for text in ("Project reference: P10-39", "Projekt 1039", "Order nr 10-39"):
            self.assertEqual([item["object_id"] for item in match_objects(text, objects)], [7])
        for text in ("Invoice number: 1039", "Article number: P1039", "Total: 1039"):
            self.assertEqual(match_objects(text, objects), [])
        for text in ("Reference: 10.39", "Order nr: 1039.00", "Project: 10/39"):
            self.assertEqual(match_objects(text, objects), [])

    def test_ambiguous_context_returns_all_candidates_without_assigning(self):
        fields = fixture()
        fields["order_proposals"] = [dict(fields["order_proposals"][0], project_code="P10-22")]
        fields["net_amount"] = "473.86"
        fields["vat_amount"] = "118.47"
        fields["rounding_amount"] = "0.00"
        result = propose_object_allocations(fields, [OBJECTS[0], {**OBJECTS[0], "id": 99}])
        self.assertIsNone(result["parts"][0]["object_id"])
        self.assertEqual({item["id"] for item in result["parts"][0]["object_options"]}, {5, 99})

    def test_service_categories_are_not_suggested_as_projects(self):
        objects = [*OBJECTS, {
            "id": 15, "name": "Custom category", "markning": "P1027",
            "service_type": "atf", "is_active": 1,
        }]
        part = propose_object_allocations(fixture(), objects)["parts"][2]
        self.assertEqual([item["id"] for item in part["object_options"]], [12])
        self.assertIsNone(part["object_id"])

    def test_unmarked_order_remains_separate_without_discarding_other_proposals(self):
        fields = fixture()
        fields["order_proposals"][1]["project_code"] = "."
        result = propose_object_allocations(fields, OBJECTS)
        self.assertFalse(result["errors"])
        part = next(part for part in result["parts"] if part["marker"] == "")
        self.assertEqual(part["orders"], ["1"])
        self.assertEqual(part["object_options"], [])
        self.assertIsNone(part["object_id"])

    def test_project_context_continues_between_pages_but_not_attachments(self):
        parsed = parse_project_order_allocations([
            {"page": 1, "source": "first.pdf", "attachment_index": 0,
             "text": "Order nr: A1\nProj.nr.: P 10-39"},
            {"page": 2, "source": "first.pdf", "attachment_index": 0,
             "text": "Summa order: 100,00\nMoms order: 25,00"},
            {"page": 1, "source": "second.pdf", "attachment_index": 1,
             "text": "Order nr: A1\nSumma order: 200,00\nMoms order: 50,00"},
        ])
        self.assertEqual(len(parsed["orders"]), 2)
        self.assertEqual(parsed["orders"][0]["project_code"], "P 10-39")
        self.assertEqual(parsed["orders"][0]["sources"][0]["page"], 2)
        self.assertIsNone(parsed["orders"][1]["project_code"])

    def test_ambiguous_or_incomplete_items_do_not_make_valid_proposal(self):
        for key, value in (("ambiguous", True), ("vat_amount", "1.00"), ("vat_items", [])):
            fields = fixture()
            fields["order_proposals"][1][key] = value
            self.assertTrue(propose_object_allocations(fields, OBJECTS)["errors"])


class ProposalIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(integration.IncomingIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(integration.IncomingIntegrationTests.tearDownClass.__func__)
    _add_company = integration.IncomingIntegrationTests._add_company
    setUp = integration.IncomingIntegrationTests.setUp
    stage = integration.IncomingIntegrationTests.stage
    request = integration.IncomingIntegrationTests.request
    sync = integration.IncomingIntegrationTests.sync

    def prepare(self):
        from test_mail_automation import pdf
        objects = []
        conn = self.server.get_db_connection()
        for obj in OBJECTS:
            cursor = conn.execute("""INSERT INTO meta_objects(company,name,markning,rate,transport_rate,is_active)
                VALUES ('Renatur',?,?,300,450,?)""", (obj["name"], obj["markning"], obj["is_active"]))
            objects.append({**obj, "id": cursor.lastrowid})
        conn.commit()
        conn.close()
        self.host._expense_objects = objects
        self.store.add_pdf(pdf("Isolated proposal document"), "proposal.pdf", {"fields": fixture()})
        document_id = self.store.documents()[-1]["id"]
        from offline_review_ui import OfflineReviewWindow
        window = OfflineReviewWindow(self.store, integrated=True)
        self.addCleanup(self.close_window, window)
        window.selector.setCurrentIndex(window.selector.findData(document_id))
        return window, document_id, objects

    def close_window(self, window):
        from PyQt6.QtWidgets import QMessageBox
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
            window.close()

    def prepare_beijer(self):
        from test_mail_automation import pdf
        objects = []
        conn = self.server.get_db_connection()
        for obj in BEIJER_OBJECTS:
            cursor = conn.execute("""INSERT INTO meta_objects(company,name,markning,rate,transport_rate,is_active)
                VALUES ('Renatur',?,?,300,450,?)""", (obj["name"], obj["markning"], obj["is_active"]))
            objects.append({**obj, "id": cursor.lastrowid})
        conn.commit()
        conn.close()
        self.host._expense_objects = objects
        self.store.add_pdf(pdf("Read-only Beijer #9 fixture"), "beijer-9.pdf",
                           {"fields": beijer_fixture()})
        document_id = self.store.documents()[-1]["id"]
        from offline_review_ui import OfflineReviewWindow
        window = OfflineReviewWindow(self.store, integrated=True)
        self.addCleanup(self.close_window, window)
        window.selector.setCurrentIndex(window.selector.findData(document_id))
        return window, document_id, objects

    def test_explicit_apply_save_reopen_confirm_sync_lost_ack_one_original(self):
        from PyQt6.QtWidgets import QMessageBox

        window, document_id, objects = self.prepare()
        self.assertEqual(window.allocations.rowCount(), 0)
        self.assertIsNone(self.store.card(document_id))
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            window.apply_proposal_button.click()
        self.assertEqual(window.allocations.rowCount(), 3)
        self.assertEqual([window.allocations.cellWidget(row, 0).currentData()
                          for row in range(window.allocations.rowCount())],
                         [objects[0]["id"], objects[1]["id"], "unselected"])
        window.show_archived_objects.setChecked(True)
        for row, obj in enumerate(objects):
            combo = window.allocations.cellWidget(row, 0)
            self.assertGreaterEqual(combo.findData(obj["id"]), 0)
            combo.setCurrentIndex(combo.findData(obj["id"]))
        self.assertIn("[Архив]", window.allocations.cellWidget(2, 0).currentText())
        window.checked.setChecked(True)
        self.assertIsNone(self.store.card(document_id))
        self.assertEqual(self.store.latest(document_id)[0], 0)
        self.assertEqual(window.inputs["gross_amount"].text(), "5286.00")
        self.assertEqual(window.inputs["net_amount"].text(), "4228.88")
        window.expense_button.click()
        self.assertIsNotNone(self.store.card(document_id))
        revision = self.store.latest(document_id)[0]
        self.mode = "lost_ack"
        self.assertEqual(self.sync()["synced"], 0)
        self.assertEqual(self.sync()["synced"], 1, self.last_sync)
        self.store.transfer(document_id, revision)
        self.assertEqual(self.sync()["synced"], 0)
        documents = self.server_client.get("/api/expense-documents",
            headers={"X-Sync-Key": "isolated-sync-test-key"}).get_json()["documents"]
        self.assertEqual(len(documents), 1)
        remote = documents[0]
        self.assertEqual(remote["net_amount"], "4228.88")
        self.assertEqual(remote["vat_amount"], "1057.24")
        self.assertEqual(remote["rounding_amount"], "-0.12")
        self.assertEqual(remote["gross_amount"], "5286.00")
        self.assertEqual(len(remote["allocations"]), 3)
        self.assertEqual(len(remote["attachments"]), 1)
        self.assertEqual({part["object_id"]: part["gross_amount"] for part in remote["allocations"]},
                         {obj["id"]: value for obj, value in zip(objects, ("592.32", "2411.02", "2282.66"))})
        response = self.server_client.put(f"/api/expense-documents/{remote['id']}", json={
            "document_type": "manual", "usage": "object_cost", "supplier_name": remote["supplier_name"],
            "document_date": remote["document_date"], "invoice_number": remote["invoice_number"],
            "currency_code": "SEK", "gross_amount": "5286.00", "vat_amount": "1057.24",
            "verification_status": "confirmed", "expected_version": remote["sync_version"],
            "document_comment": "Metadata-only review", "allocations": remote["allocations"],
        }, headers={"X-Sync-Key": "isolated-sync-test-key"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["document"]["net_amount"], "4228.88")
        self.assertEqual(response.get_json()["document"]["rounding_amount"], "-0.12")
        conn = self.server.get_db_connection()
        self.assertEqual(conn.execute("SELECT is_active FROM meta_objects WHERE id=?", (objects[2]["id"],)).fetchone()[0], 0)
        conn.close()

    def test_beijer_proposal_fills_form_idempotently_without_creating_expense(self):
        from PyQt6.QtWidgets import QMessageBox

        window, document_id, objects = self.prepare_beijer()
        before = self.store.latest(document_id)
        self.assertFalse(window.proposal["errors"], window.proposal["errors"])
        self.assertEqual(window.inputs["gross_amount"].text(), "5679.10")
        with patch.object(QMessageBox, "warning") as warning, \
                patch.object(QMessageBox, "question",
                             return_value=QMessageBox.StandardButton.No) as question:
            window.apply_proposal_button.click()
        warning.assert_not_called()
        question.assert_not_called()
        self.assertEqual(window.allocations.rowCount(), 3)
        self.assertEqual([window.allocations.cellWidget(row, 0).currentData()
                          for row in range(3)],
                         [objects[0]["id"], "unselected", objects[1]["id"]])
        self.assertEqual([window.allocations.item(row, 1).text() for row in range(3)],
                         ["323.19", "127.20", "5228.61"])
        self.assertEqual(window.allocations.cellWidget(1, 0).currentText(),
                         "Выберите объект или общие расходы")
        self.assertNotEqual(window.allocations.cellWidget(1, 0).styleSheet(), "")
        self.assertEqual(window.inputs["gross_amount"].text(), "5679.00")
        self.assertEqual(sum((Decimal(window.allocations.item(row, 1).text())
                              for row in range(3)), Decimal(0)), Decimal("5679.00"))
        self.assertIsNone(self.store.card(document_id))
        self.assertEqual(self.store.latest(document_id), before)

        with patch.object(QMessageBox, "warning") as warning, \
                patch.object(QMessageBox, "question",
                             return_value=QMessageBox.StandardButton.No) as question:
            window.apply_proposal_button.click()
        warning.assert_not_called()
        question.assert_not_called()
        self.assertEqual(window.allocations.rowCount(), 3)
        self.assertEqual(window.allocations.cellWidget(1, 0).currentData(), "unselected")
        self.assertIsNone(self.store.card(document_id))

        unknown = window.allocations.cellWidget(1, 0)
        unknown.setCurrentIndex(unknown.findData(objects[0]["id"]))
        with patch.object(QMessageBox, "question",
                          return_value=QMessageBox.StandardButton.No) as question:
            window.apply_proposal_button.click()
        question.assert_called_once()
        self.assertEqual(window.allocations.rowCount(), 3)
        self.assertEqual(unknown.currentData(), objects[0]["id"])
        self.assertIsNone(self.store.card(document_id))
        self.assertEqual(self.store.latest(document_id), before)

    def test_saved_manual_rows_survive_preview_and_declined_apply(self):
        from PyQt6.QtWidgets import QMessageBox
        window, document_id, objects = self.prepare()
        window.add_allocation({"object_id": objects[0]["id"], "gross_amount": "5286.00"})
        self.assertTrue(window.save_edits())
        before = self.store.latest(document_id)
        window.select_document()
        window.show_proposal()
        self.assertEqual(window.allocations.rowCount(), 1)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            window.apply_proposal_button.click()
        self.assertEqual(window.allocations.rowCount(), 1)
        self.assertEqual(self.store.latest(document_id), before)

    def test_saved_archived_assignment_survives_reopen_and_filter_changes(self):
        window, document_id, objects = self.prepare()
        archived = objects[2]
        window.add_allocation({"object_id": archived["id"], "gross_amount": "5286.00"})
        self.assertTrue(window.save_edits())
        window.select_document()
        combo = window.allocations.cellWidget(0, 0)
        self.assertEqual(combo.currentData(), archived["id"])
        self.assertIn("[Архив]", combo.currentText())
        window.show_archived_objects.setChecked(True)
        self.assertEqual(combo.currentData(), archived["id"])
        window.show_archived_objects.setChecked(False)
        self.assertEqual(combo.currentData(), archived["id"])
        self.assertIn("[Архив]", combo.currentText())
