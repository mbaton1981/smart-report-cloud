import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PyQt6.QtCore import QDate, QTimer
from PyQt6.QtWidgets import QApplication, QDateEdit, QDialog, QDialogButtonBox, QMessageBox

import folder_mail
from incoming_documents import IncomingStore
from offline_review_ui import OfflineReviewWindow
from test_folder_mail import FolderIMAP, ImportTestDate
from test_mail_automation import eml, pdf, recognized


class FolderMailUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        for name in ("folder_mail.date", "test_folder_mail.date", __name__ + ".date"):
            date_patch = patch(name, ImportTestDate)
            date_patch.start()
            self.addCleanup(date_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = SimpleNamespace(_expense_objects=[], incoming_delivery_pending=lambda client_id: None,
                                    incoming_register_snapshot=lambda: [])
        self.store = IncomingStore(Path(self.temp.name) / "incoming", self.host)
        self.addCleanup(self.store.close)
        self.window = OfflineReviewWindow(self.store, integrated=True)
        self.addCleanup(self.window.close)
        FolderIMAP.messages = {index: eml(pdf(f"UI fixture {index}")) for index in (1, 2)}
        FolderIMAP.commands = []
        FolderIMAP.dates = {}
        FolderIMAP.fail = None
        FolderIMAP.epoch = 50

    def finish(self, dialog):
        deadline = time.monotonic() + 8
        while dialog.worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(dialog.worker, "Worker must finish without forced termination")
        self.app.processEvents()

    def test_before_november_no_dialog_or_mail_connection(self):
        class BeforeStart(ImportTestDate):
            @classmethod
            def today(cls):
                return cls(2026, 10, 31)
        with patch.object(folder_mail, "date", BeforeStart), \
                patch.object(QMessageBox, "information") as information, \
                patch.object(folder_mail, "create_import_dialog", side_effect=AssertionError("No import")), \
                patch.object(folder_mail.imaplib, "IMAP4_SSL", side_effect=AssertionError("No connection")):
            folder_mail.open_import_dialog(self.window)
            folder_mail.open_import_dialog(self.window, archive=True)
            self.assertEqual(information.call_count, 2)
            self.assertIn("01.11.2026", information.call_args.args[2])

    def test_diagnostics_selftest_records_blocked_import_before_start(self):
        import json
        import sys
        import desktop_diagnostics
        class BeforeStart(ImportTestDate):
            @classmethod
            def today(cls):
                return cls(2026, 10, 31)
        app_module = SimpleNamespace(DB_FILE=str(Path(self.temp.name) / "isolated.db"),
                                     init_db_once=lambda: None)
        output = Path(self.temp.name) / "diagnostic.json"
        with patch.object(folder_mail, "date", BeforeStart), \
                patch.object(desktop_diagnostics, "_require_local_server", return_value="http://127.0.0.1:5999"), \
                patch("ob_calculator.load_ob_rules"), \
                patch.dict(sys.modules, {"rapidocr_onnxruntime": SimpleNamespace(RapidOCR=lambda: None)}), \
                patch.object(folder_mail, "create_import_dialog", side_effect=AssertionError("No password")):
            desktop_diagnostics.run(app_module, "selftest", Path(self.temp.name) / "diagnostic",
                                    out_path=output)
        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(result["mail_import_not_started"])
        self.assertEqual(result["import_start"], "2026-11-01")
        self.assertEqual(result["mail_connections"], 0)

    def test_masked_password_progress_stop_and_manual_retry(self):
        original = folder_mail.import_folder
        def slow_recognition(paths):
            time.sleep(0.05)
            return recognized(paths)
        def isolated_import(*args, **kwargs):
            return original(*args, **kwargs, client_factory=FolderIMAP, recognizer=slow_recognition)
        dialog = folder_mail.create_import_dialog(self.window, *folder_mail.recent_period())
        self.addCleanup(dialog.close)
        self.assertTrue(dialog.password.echoMode() == dialog.password.EchoMode.Password)
        self.assertIn("последние 14 дней", dialog.notice.text())
        self.assertIn("01.11.2026", dialog.notice.text())
        self.assertIn("INBOX", dialog.notice.text())
        stopping = [True]
        def stop_during_recognition(report):
            if stopping[0] and report["phase"] == "recognizing":
                stopping[0] = False
                dialog.stop_button.click()
                # Closing during the operation requests stop but cannot destroy the worker.
                self.assertFalse(dialog.close())
        dialog.progress_signal.updated.connect(stop_during_recognition)
        with patch.object(folder_mail, "import_folder", side_effect=isolated_import):
            dialog.show()
            dialog.password.setText("isolated-only")
            dialog.accept()
            dialog.accept()  # Concurrent starts are blocked.
            self.assertEqual(dialog.password.text(), "")
            self.finish(dialog)
            self.assertTrue(dialog.report["stopped"])
            self.assertIn("Остановлено безопасно", dialog.status.text())
            self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0], 0)
            self.assertEqual(self.window.selector.count(), 1)
            dialog.password.setText("isolated-only")
            dialog.accept()
            self.finish(dialog)
        self.assertFalse(dialog.report["stopped"])
        self.assertEqual(dialog.report["total"], 2)
        self.assertEqual(dialog.progress_bar.value(), 2)
        self.assertEqual(self.window.selector.count(), 2)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0], 2)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM register").fetchone()[0], 0)
        self.assertNotIn("isolated-only", dialog.status.text())

    def test_closing_parent_requests_stop_instead_of_destroying_active_worker(self):
        original = folder_mail.import_folder
        def slow_recognition(paths):
            time.sleep(0.08)
            return recognized(paths)
        def isolated_import(*args, **kwargs):
            return original(*args, **kwargs, client_factory=FolderIMAP, recognizer=slow_recognition)
        folder_mail.open_import_dialog(self.window)
        dialog = self.window._mail_import_dialog
        self.addCleanup(dialog.close)
        self.window.show()
        def close_parent(report):
            if report["phase"] == "recognizing":
                self.assertFalse(self.window.close())
                self.window.reject()
                self.assertTrue(self.window.isVisible())
                self.assertTrue(dialog.stop_event.is_set())
        dialog.progress_signal.updated.connect(close_parent)
        with patch.object(folder_mail, "import_folder", side_effect=isolated_import):
            dialog.password.setText("isolated-only")
            dialog.accept()
            self.finish(dialog)
        self.assertTrue(dialog.report["stopped"])
        self.assertTrue(self.window.isVisible())

    def test_archive_selection_requires_explicit_confirmation(self):
        end = date.today() - timedelta(days=50)
        start = end - timedelta(days=10)
        def choose(dialog):
            dialog.findChild(QDateEdit, "archive_start").setDate(QDate(start.year, start.month, start.day))
            dialog.findChild(QDateEdit, "archive_end").setDate(QDate(end.year, end.month, end.day))
            return QDialog.DialogCode.Accepted
        with patch.object(QDialog, "exec", choose), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as confirm:
            self.assertIsNone(folder_mail.archive_period(self.window))
            self.assertIn(str(start), confirm.call_args.args[2])
            self.assertIn(str(end), confirm.call_args.args[2])
        with patch.object(QDialog, "exec", choose), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.assertEqual(folder_mail.archive_period(self.window), (start, end))
        with patch.object(QDialog, "exec", return_value=QDialog.DialogCode.Rejected), \
                patch.object(QMessageBox, "question", side_effect=AssertionError("No confirmation on cancel")):
            self.assertIsNone(folder_mail.archive_period(self.window))

    def test_declined_archive_never_opens_password_or_connects(self):
        with patch.object(folder_mail, "archive_period", return_value=None), \
                patch.object(folder_mail, "create_import_dialog", side_effect=AssertionError("No import dialog")):
            folder_mail.open_import_dialog(self.window, archive=True)

    def test_import_dialog_is_nonmodal_and_duplicate_launch_reuses_it(self):
        folder_mail.open_import_dialog(self.window)
        dialog = self.window._mail_import_dialog
        self.addCleanup(dialog.close)
        self.app.processEvents()
        self.assertFalse(dialog.isModal())
        self.assertTrue(self.window.isEnabled())
        with patch.object(folder_mail, "create_import_dialog", side_effect=AssertionError("No duplicate")):
            folder_mail.open_import_dialog(self.window, archive=True)

    def test_invalid_archive_does_not_reach_confirmation_or_mail(self):
        def choose_invalid(dialog):
            today = date.today()
            current = QDate(today.year, today.month, today.day)
            dialog.findChild(QDateEdit, "archive_start").setDate(current)
            dialog.findChild(QDateEdit, "archive_end").setDate(current.addDays(-50))
            return QDialog.DialogCode.Accepted
        with patch.object(QDialog, "exec", choose_invalid), \
                patch.object(QMessageBox, "warning") as warning, \
                patch.object(QMessageBox, "question", side_effect=AssertionError("Invalid archive")):
            self.assertIsNone(folder_mail.archive_period(self.window))
            warning.assert_called_once()

    def test_real_archive_modal_decline_returns_without_import(self):
        observations = []
        def decline():
            prompt = self.app.activeModalWidget()
            observations.append(isinstance(prompt, QMessageBox))
            prompt.button(QMessageBox.StandardButton.No).click()
        def choose():
            selection = self.app.activeModalWidget()
            observations.append(selection.findChild(QDateEdit, "archive_start") is not None)
            observations.append(selection.findChild(QDateEdit, "archive_end") is not None)
            QTimer.singleShot(0, decline)
            selection.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        QTimer.singleShot(0, choose)
        self.assertIsNone(folder_mail.archive_period(self.window))
        self.assertEqual(observations, [True, True, True])

    def test_network_failure_leaves_partial_results_visible_and_retries_unfinished_uid(self):
        original = folder_mail.import_folder
        def isolated_import(*args, **kwargs):
            return original(*args, **kwargs, client_factory=FolderIMAP, recognizer=recognized)
        dialog = folder_mail.create_import_dialog(self.window, *folder_mail.recent_period(), archive=True)
        self.addCleanup(dialog.close)
        FolderIMAP.fail = 2
        with patch.object(folder_mail, "import_folder", side_effect=isolated_import):
            dialog.password.setText("isolated-only")
            dialog.accept()
            self.finish(dialog)
            self.assertFalse(dialog.successful)
            self.assertIn("Ошибка соединения", dialog.status.text())
            self.assertEqual(self.window.selector.count(), 1)
            self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0], 1)
            FolderIMAP.fail = None
            FolderIMAP.commands = []
            dialog.password.setText("isolated-only")
            dialog.accept()
            self.finish(dialog)
        self.assertTrue(dialog.successful)
        self.assertEqual(dialog.report["skipped"], 1)
        fetched = [int(command[1]) for command in FolderIMAP.commands if command[0] == "FETCH"]
        self.assertNotIn(1, fetched)
        self.assertEqual(self.window.selector.count(), 2)
