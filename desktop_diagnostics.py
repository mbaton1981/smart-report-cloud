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


def verify_approved_update(app_module, work_dir, out_path):
    """Inspect an existing approved profile without startup migrations or network."""
    from contextlib import closing
    import sqlite3
    from unittest.mock import patch
    from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QMessageBox
    from PyQt6.QtCore import QDate
    from folder_mail import IMPORT_START_DATE, import_folder, open_import_dialog, FolderError
    from incoming_documents import open_incoming
    database = Path(app_module.DB_FILE).resolve()
    profile = database.parent
    configuration = json.loads((profile / "working_profile.json").read_text(encoding="utf-8-sig"))
    if not configuration.get("approved_transition") or not database.is_file():
        raise RuntimeError("An existing approved profile is required.")
    work = Path(work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    application = QApplication.instance() or QApplication([])
    application.setStyleSheet(app_module.DARK_THEME_QSS)

    def connection():
        conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def state():
        with closing(connection()) as conn:
            return {
                name: sorted(map(tuple, conn.execute('SELECT * FROM "' + name.replace('"', '""') + '"')),
                             key=repr)
                for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }

    before = state()
    result = {"database": str(database), "frozen": bool(getattr(sys, "frozen", False)),
              "network_requests": 0, "import_start": IMPORT_START_DATE.isoformat()}
    with patch("requests.sessions.Session.request", side_effect=RuntimeError("Read-only verification: network forbidden")), \
            patch.object(app_module, "get_db_connection", connection), \
            patch.object(app_module, "REPORTS_DIR", str(work)), \
            patch.object(app_module.os, "startfile", create=True), \
            patch.object(QMessageBox, "information") as information, \
            patch.object(QMessageBox, "critical", side_effect=RuntimeError("PDF export failed")):
        window = app_module.SmartReportApp.__new__(app_module.SmartReportApp)
        QMainWindow.__init__(window)
        window.tab_invoice = QWidget()
        window.tab_dagbok = QWidget()
        window.setCentralWidget(window.tab_invoice)
        window.setStyleSheet(app_module.DARK_THEME_QSS)
        window.resize(1450, 1100)
        window.setup_invoice_tab()
        window.setup_dagbok_tab()
        window.setup_reporting_pages()
        window.inv_comp_cb.setCurrentText("Renatur")
        for field in (window.inv_date_from, window.inv_date_to):
            field.setDate(QDate(2026, 10, 21))
        for identifier, name in ((39, "tomteboda"), (84, "hotel")):
            window.inv_obj_cb.setCurrentIndex(window.inv_obj_cb.findData(identifier))
            report = window.generate_invoice()
            if report is None:
                raise RuntimeError("Existing Andrii report could not be generated")
            result[name] = {"totals": report["totals"], "payroll": report["payroll"]}
            window.export_invoice_to_pdf()
            window.show()
            application.processEvents()
            window.grab().save(str(work / (name + "-installed.png")))
            window.invoice_summary_table.scrollToBottom()
            application.processEvents()
            window.grab().save(str(work / (name + "-installed-costs.png")))
        window.close()
        if result["tomteboda"]["totals"]["hours"] != 4 or result["hotel"]["totals"]["hours"] != 8:
            raise RuntimeError("Actual report hours differ from approved scenario")
        host = QWidget()
        host._expense_objects = []
        host.incoming_delivery_pending = lambda _cid: None
        host.incoming_register_snapshot = lambda: []
        host.refresh_incoming_capabilities = lambda: None
        host._schedule_expense_read = lambda _key: None
        incoming_root = desktop_auth.app_data_dir() / "incoming-documents"
        existing = list(incoming_root.glob("*/review.sqlite3"))
        if len(existing) != 1:
            raise RuntimeError("Expected one existing incoming queue")
        with closing(sqlite3.connect(existing[0].as_uri() + "?mode=ro", uri=True)) as conn:
            result["remaining_documents"] = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            result["retained_mail_messages"] = conn.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0]
            result["excluded_pdf_hashes"] = conn.execute("SELECT COUNT(*) FROM mail_excluded_pdfs").fetchone()[0]
        if result["remaining_documents"] != 0 or result["excluded_pdf_hashes"] != 632:
            raise RuntimeError("Incoming cleanup or exact PDF exclusions differ")
        # Opening the already-updated queue is allowed; the main database stays query_only.
        incoming = open_incoming(host, existing[0].parent)
        try:
            incoming.show()
            application.processEvents()
            incoming.grab().save(str(work / "incoming-installed.png"))
            if app_module.datetime.now().date() < IMPORT_START_DATE:
                try:
                    import_folder(incoming.store, "unused-before-approved-start")
                except FolderError as error:
                    if str(error) != "import_not_started":
                        raise
                else:
                    raise RuntimeError("Pre-start mail import was not blocked")
                open_import_dialog(incoming)
                if "01.11.2026" not in information.call_args.args[2]:
                    raise RuntimeError("Missing visible import-start notice")
                result["pre_start_import_blocked"] = True
            result["november_replay"] = verify_excluded_pdf_replay(
                incoming.store, work.parent / "server-originals-for-restoration")
        finally:
            incoming.close()
    if state() != before:
        raise RuntimeError("Read-only acceptance changed protected database contents")
    result["main_database_unchanged"] = True
    result["balance_rows"] = len(before["balance"])
    if out_path:
        Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def verify_excluded_pdf_replay(store, originals):
    """Exercise the frozen importer on an in-memory ledger, never real IMAP."""
    from contextlib import closing
    from datetime import date
    from email.message import EmailMessage
    import sqlite3
    from unittest.mock import patch
    import folder_mail

    class CalendarDateType(type):
        def __instancecheck__(cls, value):
            return isinstance(value, date)

    class November(date, metaclass=CalendarDateType):
        @classmethod
        def today(cls):
            return cls(2026, 11, 14)

    pdf = originals / "b26a90289df34598b6b05c19b9568d1b.pdf"
    content = pdf.read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    if sha != "701648c0e26b4a85e16371966c95e1a0200bfdae8c8851bb0d87f83911d2c1f7":
        raise RuntimeError("Expected exact backed-up BEIJER PDF")
    message = EmailMessage()
    message["Date"] = "Sun, 01 Nov 2026 12:00:00 +0100"
    message.add_attachment(content, maintype="application", subtype="pdf", filename="replay.pdf")
    raw = message.as_bytes()
    epoch = store.conn.execute("SELECT epoch FROM mail_epoch WHERE folder='INBOX'").fetchone()[0]
    commands = []

    class FakeIMAP:
        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, *_args):
            return "OK", []

        def select(self, _folder, readonly=False):
            if not readonly:
                raise RuntimeError("IMAP must be read-only")
            return "OK", []

        def response(self, _name):
            return "UIDVALIDITY", [epoch.encode()]

        def uid(self, command, *args):
            commands.append(command)
            if command == "SEARCH":
                if args != (None, "SINCE", "01-Nov-2026", "BEFORE", "15-Nov-2026"):
                    raise RuntimeError("Wrong 14-day date boundary")
                return "OK", [b"999999"]
            if args[-1] == "(UID RFC822.SIZE)":
                return "OK", [f"999999 (UID 999999 RFC822.SIZE {len(raw)})".encode()]
            return "OK", [(b"999999 (UID 999999 BODY[])", raw)]

        def logout(self):
            return "BYE", []

    with closing(sqlite3.connect(":memory:")) as memory:
        memory.row_factory = sqlite3.Row
        store.conn.backup(memory)
        isolated = SimpleNamespace(conn=memory)
        before = memory.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0]
        with patch.object(folder_mail, "date", November):
            first = folder_mail.import_folder(isolated, "unused", client_factory=FakeIMAP)
            fetches = commands.count("FETCH")
            second = folder_mail.import_folder(isolated, "unused", client_factory=FakeIMAP)
        if (first["excluded"] != 1 or first["new_drafts"] != 0 or second["skipped"] != 1
                or commands.count("FETCH") != fetches
                or memory.execute("SELECT COUNT(*) FROM documents").fetchone()[0] != 0
                or memory.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] != before + 1):
            raise RuntimeError("Frozen importer replay protection failed")
    return {"actual_pdf_excluded": True, "new_november_uid": True, "replay_skipped": True,
            "real_mail_connections": 0, "disk_database_created": False}


def verify_reports_ui_readonly(app_module, work_dir, out_path):
    """Click the real report controls in the visible installed application."""
    from contextlib import closing
    import sqlite3
    from unittest.mock import patch
    from PyQt6.QtCore import QDate, QTimer, Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QMessageBox, QPushButton
    from pypdf import PdfReader

    database = Path(app_module.DB_FILE).resolve(strict=True)
    configuration = json.loads((database.parent / "working_profile.json").read_text(encoding="utf-8-sig"))
    if configuration.get("approved_transition") is not True:
        raise RuntimeError("Expected the approved existing profile")
    work = Path(work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)

    def connection():
        conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def state():
        with closing(connection()) as conn:
            return {
                name: sorted(map(tuple, conn.execute('SELECT * FROM "' + name.replace('"', '""') + '"')),
                             key=repr)
                for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }

    before = state()
    application = QApplication.instance() or QApplication([])
    application.setStyleSheet(app_module.DARK_THEME_QSS)
    failures = []
    messages = []
    result = {
        "database": str(database), "executable": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)), "visible_ui": True,
        "real_network_requests": 0, "new_disk_databases": 0,
    }
    resource = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    manifest = resource / "release-manifest.json"
    if manifest.exists():
        result["build"] = json.loads(manifest.read_text(encoding="utf-8-sig"))

    def exception_hook(kind, error, traceback):
        failures.append(f"{kind.__name__}: {error}")

    def dismiss_information():
        modal = application.activeModalWidget()
        if isinstance(modal, QMessageBox):
            messages.append({"title": modal.windowTitle(), "text": modal.text()})
            if modal.icon() != QMessageBox.Icon.Information:
                failures.append(modal.text())
            modal.accept()

    def click(widget):
        if not widget.isVisible() or not widget.isEnabled():
            raise RuntimeError("Report control is not visible and enabled")
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
        QTest.qWait(150)
        if failures:
            raise RuntimeError("; ".join(failures))

    def button(label):
        return next(widget for widget in window.tab_invoice.findChildren(QPushButton)
                    if label in widget.text())

    def capture(name):
        window.grab().save(str(work / (name + ".png")))
        screen = application.primaryScreen()
        if screen is not None:
            screen.grabWindow(0).save(str(work / (name + "-desktop.png")))

    with patch("requests.sessions.Session.request", side_effect=RuntimeError("UI acceptance: network forbidden")), \
            patch.object(app_module, "get_db_connection", connection), \
            patch.object(app_module, "init_db_once"), \
            patch.object(app_module, "REPORTS_DIR", str(work)), \
            patch.object(app_module.os, "startfile", create=True), \
            patch.object(sys, "excepthook", exception_hook):
        window = app_module.SmartReportApp()
        window.auto_sync_timer.stop()
        window.setWindowTitle(window.windowTitle() + " — проверка только чтения")
        window.statusBar().showMessage("Проверка установленного интерфейса: база только для чтения; сеть запрещена.")
        window.showMaximized()
        window.raise_()
        window.activateWindow()
        dismissal = QTimer(window)
        dismissal.timeout.connect(dismiss_information)
        dismissal.start(100)

        def exercise():
            try:
                bar = window.tabs.tabBar()
                QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(4).center())
                QTest.qWait(200)
                if window.tabs.currentWidget().widget() is not window.tab_invoice:
                    raise RuntimeError("The unified top-level tab did not open")
                if window.report_pages.count() != 2:
                    raise RuntimeError("Missing shared journal page")
                window.inv_comp_cb.setCurrentText("Renatur")
                for field in (window.inv_date_from, window.inv_date_to):
                    field.setDate(QDate(2026, 10, 21))
                for identifier, label, hours, known, final in (
                    (39, "tomteboda", 4, 2530, None),
                    (84, "hotel", 8, 4610, 4610),
                ):
                    window.inv_obj_cb.setCurrentIndex(window.inv_obj_cb.findData(identifier))
                    click(button("Просмотр сводки"))
                    report = window._invoice_report
                    if (report["totals"]["hours"] != hours
                            or report["totals"]["known_subtotal"] != known
                            or report["totals"]["invoice_total"] != final
                            or report["recorded_trips"] != 1):
                        raise RuntimeError("Existing real report differs: " + label)
                    if window.table_dagbok.rowCount() != len(report["journal_rows"]):
                        raise RuntimeError("Journal rows differ from shared report")
                    capture(label + "-summary")
                    window.invoice_summary_table.scrollToBottom()
                    QTest.qWait(100)
                    capture(label + "-totals")
                    click(button("Сформировать общий PDF"))
                    pdf = next(work.glob("Fakturaunderlag_*" + report["object"] + "*.pdf"))
                    reader = PdfReader(pdf)
                    first = reader.pages[0].extract_text()
                    journal = "\n".join(page.extract_text() for page in reader.pages[1:])
                    if ("Fakturaunderlag" not in first or "Arbetsjournal" not in journal
                            or "SEK" in journal or "Löneunderlag" in first + journal
                            or "1 229" in first + journal):
                        raise RuntimeError("Combined PDF sections are invalid")
                    if final is None and ("Preliminärt totalbelopp" not in first or "utan ej fastställda" not in first):
                        raise RuntimeError("Missing explicit preliminary total")
                    inner = window.report_pages.tabBar()
                    QTest.mouseClick(inner, Qt.MouseButton.LeftButton, pos=inner.tabRect(1).center())
                    QTest.qWait(100)
                    capture(label + "-journal")
                    click(button("PDF только журнала"))
                    journal_pdf = next(work.glob("Arbetsjournal_*" + report["object"] + "*.pdf"))
                    if "Fakturaunderlag" in PdfReader(journal_pdf).pages[0].extract_text():
                        raise RuntimeError("Journal-only export includes customer costing")
                    result[label] = {
                        "hours": hours, "totals": report["totals"],
                        "cost_lines": report["cost_lines"], "recorded_trips": report["recorded_trips"],
                        "pdf": str(pdf), "pages": len(reader.pages),
                        "journal_rows": len(report["journal_rows"]),
                    }
                    QTest.mouseClick(inner, Qt.MouseButton.LeftButton, pos=inner.tabRect(0).center())
                if failures:
                    raise RuntimeError("; ".join(failures))
            except Exception as error:
                failures.append(f"{type(error).__name__}: {error}")
            finally:
                window.close()
                application.quit()

        QTimer.singleShot(400, exercise)
        application.exec()
    if state() != before:
        raise RuntimeError("UI acceptance changed protected database contents")
    if failures:
        raise RuntimeError("; ".join(failures))
    result["main_database_unchanged"] = True
    result["balance_rows"] = len(before["balance"])
    result["button_clicks_verified"] = True
    result["export_messages"] = len(messages)
    if out_path:
        Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def run(app_module, action, work_dir, case="case", out_path=None):
    if action == "approved-update-readonly":
        return verify_approved_update(app_module, work_dir, out_path)
    if action == "reports-ui-readonly":
        return verify_reports_ui_readonly(app_module, work_dir, out_path)
    url = _require_local_server()
    import requests
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    result = {"action": action, "case": case, "frozen": bool(getattr(sys, "frozen", False)), "server": url}
    state_path = work / f"{case}_state.json"

    if action == "incoming-proposal":
        from proposal_acceptance import verify_proposal
        result.update(verify_proposal(app_module, work))
    elif action == "shift-edit":
        from shift_edit_acceptance import verify_shift_edit
        harness = SimpleNamespace(_manual_sync_requested=False)
        harness.apply_remote_shift = lambda cursor, shift: app_module.SmartReportApp.apply_remote_shift(
            harness, cursor, shift)
        harness._sync_expense_document_outbox = lambda: app_module.SmartReportApp._sync_expense_document_outbox(harness)
        result.update(verify_shift_edit(app_module, work,
                      sync=lambda: app_module.SmartReportApp.perform_sync_logic(harness)))
    elif action == "mail-release":
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
                assert window.gallery.isVisible()
                assert not window.gallery.preview._image.pixmap().isNull()
            window.selector.setCurrentIndex(window.selector.findData(2))
            window.expense_button.click()
            window.kind.setCurrentIndex(window.kind.findData("receipt"))
            window.object.setCurrentIndex(window.object.findData(1))
            window.checked.setChecked(True)
            assert window.inputs["gross_amount"].text() == "7506.00"
            assert window.save_edits()
            application.processEvents()
            assert window.gallery.isVisible()
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
            application.processEvents()
            assert window.gallery.isVisible()
            window.grab().save(str(work / "incoming.png"))
            window.selector.setCurrentIndex(window.selector.findData(4))
            assert not window.transfer_button.isEnabled()
            result.update({"incoming_imports_loaded": True, "five_pdfs_opened": True,
                           "receipt_edit_confirm_retry": True, "nordea_blocked": True,
                           "masked_password_loaded": True, "queued_count": len(queued)})
            tax_path = work / "tax-original.pdf"
            if tax_path.is_file():
                from invoice_processing import recognize_invoice
                tax = recognize_invoice([tax_path])
                assert tax["fields"]["object_cost_fields"]["gross_amount"] == "1208.00"
                store.add_pdf(tax_path.read_bytes(), tax_path.name, tax)
                document_id = store.conn.execute("SELECT id FROM documents WHERE sha256=?", (
                    hashlib.sha256(tax_path.read_bytes()).hexdigest(),)).fetchone()[0]
                window.reload_documents()
                window.selector.setCurrentIndex(window.selector.findData(document_id))
                assert window.inputs["gross_amount"].text() == "1208.00"
                window.inputs["vat_amount"].setText("not a valid financial field")
                window.ignore_button.click()
                window.later_button.click()
                assert not window.financial_panel.isVisible()
                assert store.card(document_id) is None and len(queued) == 1
                window.inputs["vat_amount"].clear()
                window.expense_button.click()
                application.processEvents()
                assert window.financial_panel.isVisible() and window.gallery.isVisible()
                assert window.inputs["supplier_name"].text() == "Transportstyrelsen"
                assert window.inputs["document_date"].text() == "2026-10-12"
                assert not window.inputs["vat_amount"].text()
                assert not window.inputs["invoice_number"].text()
                window.grab().save(str(work / "tax-autofill.png"))
                result.update(tax_amount_autofilled=True, tax_status_only_actions=True,
                              tax_expenses_created=0)
            from decimal import Decimal
            host._expense_objects.append({"id": 3, "name": "ISOLATED C"})
            window.selector.setCurrentIndex(window.selector.findData(1))
            window.expense_button.click()
            total = Decimal(window.inputs["gross_amount"].text())
            window.add_allocation({"object_id": 1, "gross_amount": "1.00"})
            assert window.save_edits() and store.card(1) is None
            assert store.issues(1, window.payload())
            window.add_allocation({"object_id": 2, "gross_amount": "1.00"})
            window.add_allocation({"object_id": 3, "gross_amount": str(total - Decimal(2))})
            window.checked.setChecked(True)
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                window.confirm_transfer()
            assert store.card(1) and len(queued) == 2
            parts = queued[-1][0][2]["allocations"]
            assert len(parts) == 3
            assert sum(Decimal(part["gross_amount"]) for part in parts) == total
            assert all("vat_amount" not in part for part in parts)
            window.grab().save(str(work / "three-object-allocation.png"))
            result.update(three_object_allocation=True, partial_draft_saved_without_expense=True,
                          no_per_object_vat_required=True)
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
        from PyQt6.QtWidgets import QApplication, QLineEdit
        from folder_mail import create_import_dialog, recent_period, import_folder, FolderError, IMPORT_START_DATE
        from incoming_documents import IncomingStore
        from offline_review_ui import OfflineReviewWindow
        application = QApplication.instance() or QApplication([])
        store = IncomingStore(work / "mail-smoke", SimpleNamespace(_expense_objects=[]))
        window = OfflineReviewWindow(store, integrated=True)
        start, end = recent_period()
        if end < IMPORT_START_DATE:
            try:
                import_folder(store, "unused-before-import-start")
            except FolderError as error:
                assert str(error) == "import_not_started"
            else:
                raise AssertionError("Import must be blocked before 01.11.2026")
            result.update(mail_import_not_started=True, import_start="2026-11-01", mail_connections=0)
        else:
            dialog = create_import_dialog(window, start, end)
            try:
                assert dialog.password.echoMode() == QLineEdit.EchoMode.Password
                assert "последние 14 дней" in dialog.notice.text() and "INBOX" in dialog.notice.text()
                assert not dialog.stop_button.isEnabled()
                dialog.show()
                application.processEvents()
                dialog.grab().save(str(work / "mail-period.png"))
                dialog.reject()
                result.update(mail_period_dialog=True, masked_password=True, stop_control=True, mail_connections=0)
            finally:
                dialog.close()
        window.close()
        store.close()
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
