"""Installed-client acceptance using an explicitly supplied isolated fixture and localhost."""
import json
import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def verify_proposal(desktop, work):
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from incoming_documents import IncomingStore, open_incoming
    application = QApplication.instance() or QApplication([])
    application.setStyleSheet(desktop.DARK_THEME_QSS)
    desktop.init_db_once()
    host = desktop.QWidget()
    host._incoming_costs_supported = True
    host.refresh_incoming_capabilities = lambda: None
    host._schedule_expense_read = lambda key: None
    host._expense_objects = desktop.request_expense_api("GET", "/api/expense-objects?include_inactive=1")["objects"]
    host.incoming_delivery_pending = lambda client_id: None
    snapshot = []
    host.incoming_register_snapshot = lambda: snapshot
    staged = work / "staged"

    def stage(paths, client_id):
        import shutil
        folder = staged / client_id
        folder.mkdir(parents=True, exist_ok=True)
        results = []
        for source in paths:
            target = folder / Path(source).name
            shutil.copyfile(source, target)
            results.append(str(target))
        return results

    host._store_expense_originals = stage
    host.queue_expense_document_operation = lambda *args, **kwargs: desktop.SmartReportApp.queue_expense_document_operation(
        host, *args, **kwargs)
    store = IncomingStore(work / "incoming", host)
    fixture = work / "fixture"
    store.import_snapshot(fixture)
    store.close()
    window = open_incoming(host, work / "incoming")
    store = window.store
    try:
        document_id = store.documents()[0]["id"]
        window.selector.setCurrentIndex(window.selector.findData(document_id))
        window.expense_button.click()
        assert len(window.proposal["parts"]) == 3 and not window.proposal["errors"]
        assert "Объект неактивен" in window.proposal_label.text()
        assert window.allocations.rowCount() == 0
        window.apply_proposal_button.click()
        assert window.allocations.rowCount() == 3
        expected = {part["object_id"]: part["gross_amount"] for part in window.proposal["parts"]}
        assert sorted(expected.values()) == ["2282.66", "2411.02", "592.32"]
        assert window.inputs["gross_amount"].text() == "5286.00"
        assert window.inputs["net_amount"].text() == "4228.88"
        assert window.inputs["vat_amount"].text() == "1057.24"
        assert not store.card(document_id)
        window.checked.setChecked(True)
        assert window.save_edits()
        window.select_document()
        assert window.allocations.rowCount() == 3 and not store.card(document_id)
        window.show()
        application.processEvents()
        screenshots, geometry = [], []
        for size in ("maximized", "reduced"):
            if size == "maximized":
                window.showMaximized()
            else:
                window.showNormal()
                window.resize(980, 760)
            application.processEvents()
            window.splitter.setSizes([max(220, window.width() // 3), max(470, window.width() * 2 // 3)])
            application.processEvents()
            assert window.width() <= application.primaryScreen().availableGeometry().width()
            window.financial_panel.ensureWidgetVisible(window.inputs["supplier_name"], 0, 0)
            application.processEvents()
            assert window.financial_panel.horizontalScrollBar().maximum() == 0
            window.grab().save(str(work / f"incoming-{size}-fields.png"))
            window.financial_panel.ensureWidgetVisible(window.allocations, 0, 15)
            application.processEvents()
            table = window.allocations
            window.grab().save(str(work / f"incoming-{size}.png"))
            assert table.columnWidth(1) >= 135 and table.columnWidth(2) >= 84
            assert table.width() <= window.financial_panel.viewport().width()
            for row in range(3):
                combo, button = table.cellWidget(row, 0), table.cellWidget(row, 2)
                assert table.rowHeight(row) >= max(combo.sizeHint().height(), button.sizeHint().height())
                assert button.width() >= button.sizeHint().width(), (button.width(), button.sizeHint().width())
                assert table.fontMetrics().horizontalAdvance(table.item(row, 1).text()) + 16 <= table.columnWidth(1)
                assert combo.width() >= 110
            image = work / f"incoming-{size}.png"
            window.grab().save(str(image))
            screenshots.append(str(image))
            geometry.append({"mode": size, "width": window.width(), "height": window.height(),
                             "card_width": window.financial_panel.width(),
                             "columns": [table.columnWidth(i) for i in range(3)],
                             "row_height": table.rowHeight(0), "device_pixel_ratio": window.devicePixelRatioF()})
        # Reopening and previewing never replace saved manual rows.
        saved = store.latest(document_id)
        window.show_proposal()
        assert store.latest(document_id) == saved
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            window.apply_proposal_button.click()
        assert store.latest(document_id) == saved
        revision = saved[0]
        store.transfer(document_id, revision)
        result = desktop.SmartReportApp._sync_expense_document_outbox(host)
        assert result["synced"] == 1, result
        store.transfer(document_id, revision)
        assert desktop.SmartReportApp._sync_expense_document_outbox(host)["synced"] == 0
        remote = desktop.request_expense_api("GET", "/api/expense-documents")["documents"]
        client_id = store.card(document_id)["client_id"]
        matching = [document for document in remote if document["client_id"] == client_id]
        assert len(matching) == 1
        document = matching[0]
        assert document["net_amount"] == "4228.88" and document["vat_amount"] == "1057.24"
        assert document["gross_amount"] == "5286.00" and document["rounding_amount"] == "-0.12"
        assert len(document["attachments"]) == 1
        assert {part["object_id"]: part["gross_amount"] for part in document["allocations"]} == expected
        assert sum((Decimal(part["gross_amount"]) for part in document["allocations"]), Decimal(0)) == Decimal("5286.00")
        snapshot[:] = remote
        window.select_document()
        assert window.allocations.rowCount() == 3
        assert {window.allocations.cellWidget(i, 0).currentData(): window.allocations.item(i, 1).text()
                for i in range(3)} == expected
        assert not window.apply_proposal_button.isEnabled()
        assert not store.add_pdf(store.original(document_id).read_bytes(), "renamed.pdf", {"fields": {}})
        return {"proposal_apply_save_reopen_sync": True, "manual_rows_preserved": True,
                "inactive_object_preserved": True, "one_document_one_pdf": True,
                "total": "5286.00", "net": "4228.88", "vat": "1057.24",
                "geometry": geometry, "screenshots": screenshots,
                "qt_scale_factor": os.environ.get("QT_SCALE_FACTOR", "1")}
    finally:
        window.loaded_payload = window.payload()
        window.close()
        store.close()
        host.close()
