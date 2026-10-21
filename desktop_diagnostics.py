"""Диагностика установленного клиента: проверка входа, очереди и вложений только против локального тестового сервера."""
import hashlib
import json
import os
import sys
import uuid
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

    if action == "mail-release":
        from release_acceptance import verify_mail_release
        result.update(verify_mail_release(app_module, work))
    elif action == "incoming-selftest":
        from PyQt6.QtWidgets import QApplication, QLineEdit, QMessageBox
        from unittest.mock import patch
        from incoming_documents import IncomingStore
        from limited_mail_ui import PasswordDialog
        application = QApplication.instance() or QApplication([])
        app_module.init_db_once()
        queued = []
        host = SimpleNamespace(
            _expense_objects=[{"id": 1, "name": "ISOLATED A"}, {"id": 2, "name": "ISOLATED B"}],
            _incoming_costs_supported=True,
            incoming_register_snapshot=lambda: [],
            incoming_delivery_pending=lambda client_id: {"last_error": None},
            _store_expense_originals=lambda paths, client_id: paths,
            queue_expense_document_operation=lambda *args, **kwargs: queued.append((args, kwargs)),
        )
        store = IncomingStore(work / "incoming", host)
        store.import_snapshot(work / "snapshot")
        from offline_review_ui import OfflineReviewWindow
        window = OfflineReviewWindow(store, integrated=True)
        try:
            window.show()
            application.processEvents()
            assert window.selector.count() == 5
            for index in range(window.selector.count()):
                window.selector.setCurrentIndex(index)
                application.processEvents()
                assert window.original_available
            window.selector.setCurrentIndex(window.selector.findData(2))
            window.kind.setCurrentIndex(window.kind.findData("receipt"))
            window.object.setCurrentIndex(window.object.findData(1))
            window.checked.setChecked(True)
            assert window.inputs["gross_amount"].text() == "7506.00"
            assert window.save_edits()
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                window.confirm_transfer()
            card = store.card(2)
            assert card and len(queued) == 1
            store.transfer(2, card["revision"])
            assert len(queued) == 1
            assert queued[0][0][2]["usage"] == "object_cost"
            assert "settlement_status" not in queued[0][0][2]
            assert "amount_due" not in window.inputs
            prompt = PasswordDialog(lambda password: None, offline=False, parent=window)
            assert prompt.password.echoMode() == QLineEdit.EchoMode.Password
            prompt.reject()
            window.grab().save(str(work / "incoming.png"))
            window.selector.setCurrentIndex(window.selector.findData(4))
            assert not window.transfer_button.isEnabled()
            result.update({"incoming_imports_loaded": True, "five_pdfs_opened": True,
                           "receipt_edit_confirm_retry": True, "nordea_blocked": True,
                           "masked_password_loaded": True, "queued_count": len(queued)})
        finally:
            window.close()
            store.close()
    elif action == "selftest":
        from ob_calculator import load_ob_rules
        from rapidocr_onnxruntime import RapidOCR
        load_ob_rules()
        RapidOCR()
        app_module.init_db_once()
        result.update({"ob_rules_loaded": True, "ocr_models_loaded": True, "database": str(Path(app_module.DB_FILE).resolve()),
                       "cwd": os.getcwd()})
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
