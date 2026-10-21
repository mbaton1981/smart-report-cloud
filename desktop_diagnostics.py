"""Диагностика установленного клиента: проверка входа, очереди и вложений только против локального тестового сервера."""
import hashlib
import json
import os
import sys
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import desktop_auth


def _require_local_server():
    url = desktop_auth.server_url()
    target = urlsplit(url)
    if target.scheme != "http" or target.hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit("Диагностика разрешена только для локального тестового сервера.")
    return url


def run(app_module, action, work_dir, case="case", out_path=None):
    url = _require_local_server()
    import requests
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    result = {"action": action, "case": case, "frozen": bool(getattr(sys, "frozen", False)), "server": url}
    state_path = work / f"{case}_state.json"

    if action in ("local-create", "local-reopen"):
        from unittest.mock import patch
        from PyQt6.QtWidgets import QApplication, QMessageBox
        assert app_module.ISOLATED_CANDIDATE, "Persistence check requires an isolated candidate."
        application = QApplication.instance() or QApplication([])
        app_module.init_db_once()
        marker = "ISOLATED-CANDIDATE-PERSISTENCE-CHECK"
        with patch.object(requests.sessions.Session, "request",
                          side_effect=AssertionError("Network forbidden in local persistence check")):
            window = app_module.SmartReportApp()
            try:
                window.show()
                application.processEvents()
                assert not window.auto_sync_timer.isActive()
                if action == "local-create":
                    with closing(app_module.get_db_connection()) as connection:
                        assert connection.execute("SELECT count(*) FROM shifts").fetchone()[0] == 0
                        selected = next(row for row in connection.execute(
                            "SELECT name,company,markning FROM objects WHERE is_active=1 ORDER BY id"
                        ) if not any(app_module.matches_special_object(
                            row["name"], row["markning"], reference
                        ) for reference in app_module.SPECIAL_OBJECTS.values()))
                    window.record_type_cb.setCurrentIndex(
                        window.record_type_cb.findData(app_module.RECORD_TYPE_WORK))
                    window.comp_cb.setCurrentText(selected["company"])
                    index = window.obj_cb.findData(selected["name"])
                    if index < 0:
                        index = window.obj_cb.findText(selected["name"])
                    assert index >= 0, "Object not available in input form."
                    window.obj_cb.setCurrentIndex(index)
                    assert window.emp_cb.count() > 0
                    window.date_input.setText("2026-10-20")
                    window.hours_input.setText("1.25")
                    window.comment_input.setText(marker)
                    with patch.object(QMessageBox, "information"), patch.object(
                        QMessageBox, "warning", side_effect=AssertionError("Save form rejected input")
                    ):
                        window.save_shift()
                window.load_shifts_history()
                with closing(app_module.get_db_connection()) as connection:
                    saved = connection.execute("SELECT * FROM shifts WHERE comment=?", (marker,)).fetchall()
                    assert len(saved) == 1 and saved[0]["hours"] == 1.25
                    assert connection.execute("SELECT count(*) FROM shifts").fetchone()[0] == 1
                assert window.table_shifts.rowCount() == 1
                assert any(window.table_shifts.item(0, column) is not None
                           and marker in window.table_shifts.item(0, column).text()
                           for column in range(window.table_shifts.columnCount()))
                result.update(saved_via_form=True, visible_in_history=True, hours=1.25,
                              network_disabled=True, auto_sync_disabled=True)
            finally:
                window.close()
    elif action in ("selftest", "baseline"):
        from ob_calculator import load_ob_rules
        from rapidocr_onnxruntime import RapidOCR
        load_ob_rules()
        RapidOCR()
        app_module.init_db_once()
        result.update({"ob_rules_loaded": True, "ocr_models_loaded": True, "database": str(Path(app_module.DB_FILE).resolve()),
                       "cwd": os.getcwd()})
        if action == "baseline":
            from unittest.mock import patch
            from PyQt6.QtWidgets import QApplication
            from document_preview import DocumentGalleryDialog
            from invoice_review import InvoiceReviewMixin
            application = QApplication.instance() or QApplication([])
            tables = ("companies", "employees", "objects", "expense_suppliers",
                      "object_name_history", "employee_sync_state")
            operations = ("shifts", "ob_segments", "balance", "company_expenses",
                          "supplier_documents", "supplier_document_allocations",
                          "supplier_document_payments", "supplier_document_vat_lines",
                          "shift_sync_outbox", "expense_document_outbox")
            with closing(app_module.get_db_connection()) as connection:
                before = {name: [tuple(row) for row in connection.execute(
                    f'SELECT * FROM "{name}" ORDER BY rowid')] for name in tables}
                for name in operations:
                    assert connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] == 0, name
            with patch.object(requests.sessions.Session, "request",
                              side_effect=AssertionError("Network forbidden in baseline UI check")):
                window = app_module.SmartReportApp()
                try:
                    window.auto_sync_timer.stop()
                    window.show()
                    application.processEvents()
                    assert isinstance(window, InvoiceReviewMixin)
                    assert app_module.DocumentGalleryDialog is DocumentGalleryDialog
                    result["tabs"] = [window.tabs.tabText(i) for i in range(window.tabs.count())]
                    assert len(result["tabs"]) == 7
                    window.load_salary_table_template()
                    window.load_balance_table()
                    window.load_shifts_history()
                    result["ui_created"] = True
                    result["network_blocked_during_ui_check"] = True
                finally:
                    window.close()
            with closing(app_module.get_db_connection()) as connection:
                for name, old_rows in before.items():
                    assert [tuple(row) for row in connection.execute(
                        f'SELECT * FROM "{name}" ORDER BY rowid')] == old_rows, name
                for name in operations:
                    assert connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] == 0, name
                result["integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
                assert result["integrity"] == "ok"
                assert not connection.execute("PRAGMA foreign_key_check").fetchall()
                result["catalog_counts"] = {name: len(value) for name, value in before.items()}
            result["catalogs_unchanged_by_ui"] = True
            result["operations_remain_empty"] = True
    elif action == "login":
        token = desktop_auth.login(url, os.environ["SMARTREPORT_E2E_USER"], os.environ["SMARTREPORT_E2E_PASS"])
        desktop_auth.set_token(token)
        result["token_saved"] = bool(desktop_auth.load_saved_token())
    else:
        saved = desktop_auth.load_saved_token()
        assert saved, "Нет сохранённого токена: сначала выполните вход."
        desktop_auth.set_token(saved, persist=False)
        assert not os.environ.get("SYNC_API_KEY"), "Диагностика должна работать без общего ключа."
        assert desktop_auth.check_token(url, saved) in (True, None)
        app_module.CLOUD_URL = url
        app_module.init_db_once()
        harness = SimpleNamespace()

        def queue_count():
            connection = app_module.get_db_connection()
            try:
                return connection.execute("SELECT COUNT(*) FROM expense_document_outbox").fetchone()[0]
            finally:
                connection.close()

        if action == "sync":
            harness._manual_sync_requested = True
            harness.apply_remote_shift = lambda cursor, shift: app_module.SmartReportApp.apply_remote_shift(
                harness, cursor, shift
            )
            harness._sync_expense_document_outbox = lambda: app_module.SmartReportApp._sync_expense_document_outbox(
                harness
            )
            result["exchange"] = app_module.SmartReportApp.perform_sync_logic(harness)
            connection = app_module.get_db_connection()
            try:
                result["queues"] = {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "shift_sync_outbox", "object_sync_outbox", "employee_sync_outbox",
                        "employee_restore_outbox", "expense_document_outbox",
                    )
                }
            finally:
                connection.close()
        elif action in ("send", "duplicate"):
            from reportlab.pdfgen import canvas
            source = work / f"{case}.pdf"
            if action == "send":
                pdf = canvas.Canvas(str(source))
                pdf.drawString(40, 700, "Installed client attachment " + case)
                pdf.save()
            elif not source.exists():
                raise AssertionError("Нет исходного PDF.")
            identifier = str(uuid.uuid4())
            state = {"client_id": identifier, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "source": str(source)}
            if action == "send":
                state_path.write_text(json.dumps(state), encoding="utf-8")
            payload = {"client_id": identifier, "supplier_name": "Installed client test", "invoice_number": "inst-" + case,
                       "document_date": "2026-10-17", "currency_code": "SEK", "gross_amount": "125.00",
                       "vat_amount": "25.00", "verification_status": "confirmed",
                       "allocations": [{"object_id": None, "gross_amount": "125.00", "vat_amount": "25.00"}]}
            copy = work / f"{case}_{action}_copy.pdf"
            copy.write_bytes(source.read_bytes())
            app_module.SmartReportApp.queue_expense_document_operation(harness, identifier, "upsert", payload, attachment_paths=[str(copy)])
            result["exchange"] = app_module.SmartReportApp._sync_expense_document_outbox(harness)
            result["queue_after"] = queue_count()
        elif action == "retry":
            result["queue_before"] = queue_count()
            result["exchange"] = app_module.SmartReportApp._sync_expense_document_outbox(harness)
            result["queue_after"] = queue_count()
        elif action == "receive":
            state = json.loads(state_path.read_text(encoding="utf-8"))
            response = requests.get(url + "/api/expense-documents", headers=app_module.sync_headers(), timeout=30)
            response.raise_for_status()
            documents = [row for row in response.json()["documents"] if row["client_id"] == state["client_id"]]
            result["server_documents_total"] = len(response.json()["documents"])
            result["matching_documents"] = len(documents)
            receiving = SimpleNamespace(_expense_temp_directory=SimpleNamespace(name=str(work)))
            paths = app_module.SmartReportApp._expense_document_original_paths(receiving, documents[0])
            result["downloaded_files"] = len(paths)
            result["download_hash_matches"] = hashlib.sha256(Path(paths[0]).read_bytes()).hexdigest() == state["sha256"]
        elif action == "storage":
            status = requests.get(url + "/api/admin/storage-status?verify_hashes=1", headers=app_module.sync_headers(), timeout=60)
            status.raise_for_status()
            result["storage"] = status.json()
        elif action == "backup":
            import server_backup
            target, manifest = server_backup.pull_backup(url, saved, work / "backup")
            result.update({"backup_files": len(manifest["files"]), "backup_problems": manifest["problems"],
                           "snapshot_bytes": (target / "server_snapshot.db").stat().st_size})
        else:
            raise SystemExit(f"Неизвестное действие: {action}")
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
    return result
