import contextlib
import imaplib
import io
import logging
import os
import secrets
import ssl
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import requests
from flask import Flask
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit
from werkzeug.serving import make_server

from limited_mail import run_limited
from limited_mail_ui import ERROR_MESSAGES, PasswordDialog, offline_operation
from mail_intake import mailbox_key, register_mail_intake
from mail_worker import IntakeClient, WorkerError
from test_limited_mail import FOLDER, HOST, USERNAME, RestrictedIMAP
from test_mail_automation import TOKEN, eml, pdf, recognized


class PasswordDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["isolated-password-tests"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for format_ in (QSettings.Format.NativeFormat, QSettings.Format.IniFormat):
            for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
                QSettings.setPath(format_, scope, str(self.root / "settings"))
        self.secret = "synthetic-" + secrets.token_hex(24)
        self.stdout, self.stderr, self.logs = io.StringIO(), io.StringIO(), io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(self.stdout))
        stack.enter_context(contextlib.redirect_stderr(self.stderr))
        handler = logging.StreamHandler(self.logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        self.dialogs = []
        self.addCleanup(self.close_dialogs)

    def close_dialogs(self):
        for dialog in self.dialogs:
            if dialog.worker is not None:
                dialog.worker.wait(5000)
                self.app.processEvents()
            dialog.reject()
            dialog.deleteLater()
        self.app.processEvents()

    def dialog(self, operation=offline_operation):
        dialog = PasswordDialog(operation, offline=True)
        self.dialogs.append(dialog)
        dialog.show()
        self.app.processEvents()
        return dialog

    def finish(self, dialog):
        deadline = time.monotonic() + 10
        while dialog.worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(10)
        self.assertIsNone(dialog.worker, "Password operation did not finish")

    def assert_no_secret(self, dialog):
        self.assertEqual(dialog.password.text(), "")
        self.assertFalse(dialog.password.isUndoAvailable())
        self.assertFalse(dialog.password.isRedoAvailable())
        widgets = [dialog, *dialog.findChildren(QLineEdit)]
        self.assertNotIn(self.secret, str([widget.property("text") for widget in widgets]))
        self.assertNotIn(self.secret, dialog.status.text())
        for stream in (self.stdout, self.stderr, self.logs):
            self.assertNotIn(self.secret, stream.getvalue())
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(self.secret.encode(), path.read_bytes(), str(path))
        for value in os.environ.values():
            self.assertNotIn(self.secret, value)

    def test_masked_input_success_clears_undo_does_not_write_or_connect(self):
        called = []

        def operation(password):
            called.append(password == self.secret)

        with patch("socket.socket.connect", side_effect=AssertionError("Network prohibited")), \
                patch("socket.socket.connect_ex", side_effect=AssertionError("Network prohibited")), \
                patch("socket.create_connection", side_effect=AssertionError("Network prohibited")), \
                patch("builtins.open", side_effect=AssertionError("File write/read prohibited")), \
                patch("os.open", side_effect=AssertionError("Filesystem prohibited")), \
                patch.object(QSettings, "setValue", side_effect=AssertionError("Settings prohibited")):
            dialog = self.dialog(operation)
            self.assertEqual(dialog.password.echoMode(), QLineEdit.EchoMode.Password)
            self.assertEqual(dialog.password.contextMenuPolicy(), Qt.ContextMenuPolicy.NoContextMenu)
            self.assertNotIn(self.secret, dialog.password.displayText())
            QTest.keyClicks(dialog.password, self.secret)
            self.assertNotEqual(dialog.password.displayText(), self.secret)
            self.assertTrue(dialog.password.displayText())
            QTest.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Ok),
                             Qt.MouseButton.LeftButton)
            self.finish(dialog)
        self.assertEqual(called, [True])
        self.assertEqual(dialog.status.text(), "Проверка завершена. Поле пароля очищено.")
        self.assert_no_secret(dialog)
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_cancel_escape_and_window_close_never_invoke_operation(self):
        for action in ("cancel", "escape", "close"):
            with self.subTest(action=action):
                called = []
                dialog = self.dialog(lambda password: called.append(True))
                QTest.keyClicks(dialog.password, self.secret)
                if action == "cancel":
                    QTest.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel),
                                     Qt.MouseButton.LeftButton)
                elif action == "escape":
                    QTest.keyClick(dialog, Qt.Key.Key_Escape)
                else:
                    dialog.close()
                self.app.processEvents()
                self.assertEqual(called, [])
                self.assert_no_secret(dialog)

    def test_empty_input_not_sent(self):
        called = []
        dialog = self.dialog(lambda password: called.append(True))
        dialog.accept()
        self.assertEqual(called, [])
        self.assertIn("Введите пароль", dialog.status.text())
        self.assertIsNone(dialog.worker)

    def test_all_error_text_including_credential_is_suppressed(self):
        for exception_type, code in (
            (imaplib.IMAP4.error, "auth"),
            (ssl.SSLCertVerificationError, "tls"),
            (requests.ConnectionError, "network"),
            (OSError, "network"),
            (WorkerError, "restricted"),
            (RuntimeError, "internal"),
        ):
            with self.subTest(code=code):
                def operation(password):
                    raise exception_type("provider echoed password: " + password + "\n<script>secret</script>")

                dialog = self.dialog(operation)
                QTest.keyClicks(dialog.password, self.secret)
                dialog.accept()
                self.finish(dialog)
                self.assertEqual(dialog.status.text(), ERROR_MESSAGES[code])
                self.assert_no_secret(dialog)
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_no_result_or_exception_object_escapes_worker(self):
        outcomes = []
        dialog = self.dialog()
        dialog.password.setText(self.secret)
        dialog.accept()
        worker = dialog.worker
        worker.outcome.connect(outcomes.append)
        with patch.object(worker, "deleteLater"):
            self.finish(dialog)
            self.assertEqual(outcomes, ["success"])
            self.assertEqual(worker._password, "")
            self.assertFalse(hasattr(worker, "_operation"))
        worker.deleteLater()
        self.assert_no_secret(dialog)

    def test_no_duplicate_submission_or_close_while_running(self):
        entered, release = threading.Event(), threading.Event()
        called = []

        def operation(password):
            called.append(password == self.secret)
            entered.set()
            release.wait(5)

        dialog = self.dialog(operation)
        dialog.password.setText(self.secret)
        dialog.accept()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(dialog.password.text(), "")
            self.assertFalse(dialog.buttons.isEnabled())
            dialog.accept()
            dialog.close()
            self.assertTrue(dialog.isVisible())
            self.assertEqual(called, [True])
        finally:
            release.set()
            self.finish(dialog)
        self.assert_no_secret(dialog)

    def test_password_does_not_persist_between_submissions(self):
        called = []
        dialog = self.dialog(lambda password: called.append(password == self.secret))
        dialog.password.setText(self.secret)
        dialog.accept()
        self.finish(dialog)
        dialog.accept()
        self.assertEqual(called, [True])
        self.assertIn("Введите пароль", dialog.status.text())
        self.assert_no_secret(dialog)

    def test_prompt_to_limited_runner_with_fake_imap_only(self):
        root = self.root / "trial"
        root.mkdir()
        app = Flask("isolated-password-intake")
        store = register_mail_intake(
            app, root=root / "intake", token=TOKEN,
            mailbox=mailbox_key(HOST, USERNAME, FOLDER), enabled=True, recognizer=recognized)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        api = IntakeClient(f"http://127.0.0.1:{server.server_port}", TOKEN, allow_local=True)
        RestrictedIMAP.messages = {11: eml(pdf())}
        RestrictedIMAP.commands = []
        RestrictedIMAP.epoch = 50
        RestrictedIMAP.count_override = RestrictedIMAP.search_override = None
        RestrictedIMAP.fail_uid = RestrictedIMAP.response_uid = None
        secret = self.secret

        class PasswordIMAP(RestrictedIMAP):
            def login(instance, username, password):
                if username != USERNAME or password != secret:
                    raise imaplib.IMAP4.error("synthetic rejected: " + password)
                return "OK", []

        def operation(password):
            run_limited(api, HOST, USERNAME, password, FOLDER,
                        root / "selection.json", client_factory=PasswordIMAP)

        try:
            with patch("imaplib.IMAP4_SSL", side_effect=AssertionError("Real IMAP prohibited")):
                dialog = self.dialog(operation)
                dialog.password.setText(self.secret)
                dialog.accept()
                self.finish(dialog)
                self.assertIn("Проверка завершена", dialog.status.text())
                self.assertEqual(store.status()["counts"]["drafts"], 1)
                dialog.password.setText(self.secret)
                dialog.accept()
                self.finish(dialog)
                self.assertEqual(store.status()["counts"]["drafts"], 1)
                self.assertEqual(store.status()["counts"]["messages"], 1)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        self.assert_no_secret(dialog)
