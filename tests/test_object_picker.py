import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QApplication

from object_picker import ObjectPickerComboBox


class ObjectPickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.objects = [
            {"id": 11, "name": "HOTEL Hazeliusbacken", "markning": "P1039",
             "company": "Bygger AB", "is_active": 1},
            {"id": 12, "name": "HOTEL Hazeliusbacken", "markning": "P1040",
             "company": "Renatur AB", "is_active": 1},
            {"id": 13, "name": "Archived project", "markning": "10-39",
             "company": "Bygger AB", "is_active": 0},
            {"id": 14, "name": "Sjuk", "markning": "p1001",
             "company": "Bygger AB", "is_active": 1},
        ]
        self.combo = ObjectPickerComboBox()
        self.addCleanup(self.combo.close)

    def test_active_search_labels_and_company_destination_use_stable_ids(self):
        self.combo.set_object_choices(self.objects, include_company=True)
        self.assertEqual(self.combo.currentData(), "unselected")
        self.assertEqual(self.combo.itemData(1), "company")
        self.assertEqual(self.combo.findData(11), 2)
        self.assertEqual(self.combo.findData(12), 3)
        self.assertEqual(self.combo.findData(13), -1)
        self.assertEqual(self.combo.findData(14), -1)
        self.assertIn("P1039", self.combo.itemText(2))
        self.assertIn("Bygger AB", self.combo.itemText(2))
        self.assertIn("Renatur AB", self.combo.itemText(3))
        self.assertEqual(self.combo.completer().caseSensitivity(), Qt.CaseSensitivity.CaseInsensitive)
        self.assertEqual(self.combo.completer().filterMode(), Qt.MatchFlag.MatchContains)
        self.combo.completer().setCompletionPrefix("P1039")
        self.assertEqual(self.combo.completer().completionCount(), 1)

    def test_archived_selection_survives_filter_refresh_and_keeps_catalog_marking(self):
        self.combo.set_object_choices(self.objects, selected=13)
        self.assertEqual(self.combo.currentData(), 13)
        self.assertIn("10-39", self.combo.currentText())
        self.assertIn("[Архив]", self.combo.currentText())
        self.combo.set_object_choices(self.objects, selected=13, show_archived=True)
        self.assertEqual(self.combo.currentData(), 13)
        self.assertEqual(self.combo.findData(13), 3)
        self.combo.set_object_choices(self.objects, selected=13, show_archived=False)
        self.assertEqual(self.combo.currentData(), 13)
        self.assertIn("[Архив]", self.combo.currentText())

    def test_catalog_refresh_is_deferred_until_open_popup_closes(self):
        self.combo.set_object_choices(self.objects, selected=11)
        self.combo.show()
        self.combo.showPopup()
        self.application.processEvents()
        self.assertTrue(self.combo.view().isVisible())
        updated = [*self.objects, {
            "id": 15, "name": "New project", "markning": "P1050", "company": "Bygger AB",
            "is_active": 1,
        }]
        self.combo.set_object_choices(updated, selected=11)
        self.assertEqual(self.combo.currentData(), 11)
        self.assertEqual(self.combo.findData(15), -1)
        self.combo.hidePopup()
        self.assertEqual(self.combo.currentData(), 11)
        self.assertGreaterEqual(self.combo.findData(15), 0)

    def test_typed_search_does_not_save_previously_selected_id(self):
        self.combo.set_object_choices(self.objects, selected=11)
        self.combo.lineEdit().textEdited.emit("P1040")
        self.assertIsNone(self.combo.currentData())
        self.combo.setCurrentIndex(self.combo.findData(12))
        self.assertEqual(self.combo.currentData(), 12)

    def test_deferred_refresh_keeps_selection_made_while_popup_is_open(self):
        self.combo.set_object_choices(self.objects, selected=11)
        self.combo.show()
        self.combo.showPopup()
        self.application.processEvents()
        self.combo.set_object_choices([*self.objects, {
            "id": 15, "name": "New", "markning": "P1050", "is_active": 1,
        }], selected=11)
        self.combo.setCurrentIndex(self.combo.findData(12))
        self.combo.hidePopup()
        self.assertEqual(self.combo.currentData(), 12)

    def test_similar_marking_does_not_hide_real_construction_object(self):
        self.combo.set_object_choices([{
            "id": 99, "name": "Private site", "markning": "p1001", "is_active": 1,
        }])
        self.assertGreaterEqual(self.combo.findData(99), 0)

    def test_explicit_none_resets_selection_for_new_document(self):
        self.combo.set_object_choices(self.objects, selected=11)
        self.combo.set_object_choices(self.objects, selected=None)
        self.assertIsNone(self.combo.currentData())

    def test_closed_picker_ignores_mouse_wheel(self):
        self.combo.set_object_choices(self.objects, selected=11)
        event = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        self.application.sendEvent(self.combo, event)
        self.assertEqual(self.combo.currentData(), 11)

if __name__ == "__main__":
    unittest.main()
