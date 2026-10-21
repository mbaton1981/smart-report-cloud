"""Local test queue UI using the existing desktop invoice review form."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from desktop_ui import ReportTable
from PyQt6.QtWidgets import QAbstractItemView, QTableWidgetItem
from invoice_processing import recognize_invoice
from invoice_review import InvoiceReviewMixin
from mail_import import classify_mail_document, recognition_status
from mail_review import export_review
from mail_trial import TrialStore


class MailViewer(InvoiceReviewMixin, QWidget):
    def __init__(self, root: Path):
        super().__init__()
        self.store = TrialStore(root)
        self.setWindowTitle("SmartReport — ТЕСТОВАЯ почтовая очередь (без рабочего учёта)")
        self.resize(1150, 700)
        layout = QVBoxLayout(self)
        note = QLabel("Только тестовые объекты. Распределения сохраняются только в тестовой очереди. "
                      "Подтверждение, перенос в учёт и синхронизация отключены.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = ReportTable(0, 7)
        self.table.setHorizontalHeaderLabels([
            "№", "Поставщик", "Номер документа", "Дата", "Тестовый черновик",
            "Тип документа", "Результат распознавания",
        ])
        self.table.horizontalHeader().moveSection(5, 1)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.doubleClicked.connect(lambda _index: self.edit_selected())
        layout.addWidget(self.table)
        edit = QPushButton("Открыть оригинал и распределение")
        edit.clicked.connect(self.edit_selected)
        layout.addWidget(edit)
        reprocess = QPushButton("Повторно распознать и обновить результат")
        reprocess.clicked.connect(self.reprocess_selected)
        layout.addWidget(reprocess)
        report = QPushButton("Проверенные значения и история — открыть отчёт")
        report.clicked.connect(self.open_report)
        layout.addWidget(report)
        self.reload()

    def reload(self):
        path = self.store.root / "review" / "audit.json"
        audit = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"documents": []}
        self.audit = {row["id"]: row for row in audit["documents"]}
        self.documents = [dict(row) for row in self.store.conn.execute("SELECT * FROM documents ORDER BY id")]
        self.table.setRowCount(len(self.documents))
        for index, document in enumerate(self.documents):
            result = json.loads(document["result_json"])
            initial = result.get("fields", {})
            recognition = recognition_status(result, document["state"])
            reviewed = self.audit.get(document["id"], {})
            draft = self.store.latest(document["id"])
            fields = draft["payload"] if draft else (reviewed.get("checked") or reviewed.get("current") or initial)
            values = (document["id"], fields.get("supplier_name") or "Не определён",
                      fields.get("invoice_number") or "Не указан",
                      fields.get("document_date") or "Не указана",
                      f"Ревизия {draft['revision']}" if draft else "Не сохранён",
                      recognition["document_type"], recognition["label"])
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip("\n".join([recognition["label"], *initial.get("review_reasons", [])]))
                self.table.setItem(index, column, item)
        self.table.resizeColumnsToContents()

    def open_document(self, document_id: int):
        _, original = self.store.document(document_id)
        result = recognize_invoice([original])
        fields = result["fields"]
        classify_mail_document(fields)
        fields["local_attachment_paths"] = [str(original)]
        draft = self.store.latest(document_id)
        revision = draft["revision"] if draft else 0
        saved = draft["payload"] if draft else {}

        def save(payload):
            self.store.save(document_id, payload, expected_revision=revision)

        accepted = self._review_net_invoice(
            saved, [str(original)], fields, trial_save=save, trial_objects=self.store.objects(),
        )
        if accepted:
            self.update_report()
            self.reload()
        return accepted

    def edit_selected(self):
        index = self.table.currentRow()
        if not 0 <= index < len(self.documents):
            QMessageBox.information(self, "Выбор документа", "Выберите строку документа.")
            return
        try:
            self.open_document(self.documents[index]["id"])
        except Exception as error:
            QMessageBox.warning(self, "Тестовый просмотр", str(error))

    def reprocess_selected(self):
        index = self.table.currentRow()
        if not 0 <= index < len(self.documents):
            QMessageBox.information(self, "Выбор документа", "Выберите строку документа.")
            return
        document = self.documents[index]
        answer = QMessageBox.question(
            self,
            "Обновить результат распознавания",
            "Повторно обработать этот оригинал и заменить сохранённый результат распознавания? "
            "Черновики распределения и рабочий учёт не изменяются.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.document(int(document["id"]))
            self.store.queue.recognize(int(document["id"]))
            self.reload()
            QMessageBox.information(
                self, "Распознавание обновлено",
                f"Результат документа #{document['id']} обновлён из сохранённого оригинала. "
                "Это не подтверждает расход и не запускает синхронизацию.",
            )
        except Exception as error:
            QMessageBox.warning(self, "Повторное распознавание", str(error))

    def update_report(self):
        return export_review(self.store.root)

    def open_report(self):
        try:
            target = self.update_report()
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
                raise RuntimeError("Не удалось открыть отчёт в браузере.")
        except Exception as error:
            QMessageBox.warning(self, "Тестовый отчёт", str(error))

    def closeEvent(self, event):
        self.store.close()
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=(
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SmartReport" / "mail_faktura_trial"
    ))
    parser.add_argument("--document", type=int)
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    try:
        viewer = MailViewer(args.data_dir)
    except Exception as error:
        QMessageBox.critical(None, "Тестовая очередь недоступна", str(error))
        return 1
    viewer.show()
    if args.document is not None:
        matches = [index for index, row in enumerate(viewer.documents) if row["id"] == args.document]
        if matches:
            viewer.table.selectRow(matches[0])
            QTimer.singleShot(0, viewer.edit_selected)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
