from __future__ import annotations

from collections import Counter

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox, QCompleter

from expense_documents import is_service_category


def _is_active(item: dict) -> bool:
    return str(item.get("is_active", 1)).strip().casefold() not in {"0", "false", "no"}


def object_label(item: dict, duplicate_names: set[str] | None = None) -> str:
    name = str(item.get("name") or "").strip()
    marking = str(item.get("markning") or "").strip() or "без маркировки"
    label = f"{name} ({marking})"
    if name.casefold() in (duplicate_names or set()):
        label += f" · {str(item.get('company') or 'фирма не указана').strip()}"
    if not _is_active(item):
        label += " [Архив]"
    return label


class ObjectPickerComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMaxVisibleItems(24)
        completer = QCompleter(self.model(), self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.setCompleter(completer)
        self._choice_signature = None
        self._pending_choices = None
        self.lineEdit().textEdited.connect(self._clear_typed_selection)

    def _clear_typed_selection(self, text):
        self.setCurrentIndex(-1)
        self.setEditText(text)

    def currentData(self, role=Qt.ItemDataRole.UserRole):
        if self.currentIndex() < 0 or self.currentText() != self.itemText(self.currentIndex()):
            return None
        return super().currentData(role)

    def wheelEvent(self, event):
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()

    def hidePopup(self):
        super().hidePopup()
        if self._pending_choices is not None:
            choices = self._pending_choices
            self._pending_choices = None
            self.set_object_choices(
                choices[0], selected=self.currentData(), show_archived=choices[2],
                placeholder=choices[3], include_company=choices[4],
            )

    def set_object_choices(
        self,
        objects: list[dict],
        *,
        selected=None,
        show_archived=False,
        placeholder="Выберите объект",
        include_company=False,
    ):
        active = [item for item in objects if _is_active(item) and not is_service_category(item)]
        archived = [item for item in objects if not _is_active(item) and not is_service_category(item)]
        visible = active + (archived if show_archived else [])
        if isinstance(selected, int) and not any(int(item["id"]) == selected for item in visible):
            selected_archived = [item for item in archived if int(item["id"]) == selected]
            visible.extend(selected_archived or [{
                "id": selected, "name": f"Объект #{selected}",
                "markning": "", "company": "", "is_active": 1,
            }])
        duplicate_names = {
            name for name, count in Counter(str(item.get("name") or "").strip().casefold()
                                            for item in active + archived).items()
            if count > 1
        }
        entries = [(placeholder, "unselected" if include_company else None)]
        if include_company:
            entries.append(("Общие расходы фирмы", "company"))
        entries.extend((object_label(item, duplicate_names), int(item["id"])) for item in visible)
        signature = tuple(entries)
        if signature == self._choice_signature:
            if self.currentIndex() < 0 and self.lineEdit().hasFocus():
                return
            index = self.findData(selected)
            if index >= 0 and index != self.currentIndex():
                self.setCurrentIndex(index)
            return
        if self.view().isVisible():
            self._pending_choices = (
                objects, selected, show_archived, placeholder, include_company,
            )
            return
        self.blockSignals(True)
        self.clear()
        for label, value in entries:
            self.addItem(label, value)
        self._choice_signature = signature
        index = self.findData(selected)
        self.setCurrentIndex(index if index >= 0 else 0)
        self.blockSignals(False)
