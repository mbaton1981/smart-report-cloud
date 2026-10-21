import json
import imaplib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from flask import Flask
from werkzeug.serving import make_server

from limited_mail import quoted_folder, run_limited
from mail_intake import bounded_recognize, mailbox_key, register_mail_intake
from mail_worker import IntakeClient, WorkerError
from test_mail_automation import TOKEN, eml, pdf, recognized


FOLDER = "SmartReport-Test"
HOST = "imap.test.invalid"
USERNAME = "test@example.invalid"


class RestrictedIMAP:
    messages = {}
    commands = []
    epoch = 50
    fail_uid = None
    response_uid = None
    count_override = None
    search_override = None
    flags = {}

    def __init__(self, host, port, *, ssl_context, timeout):
        assert host == HOST and port == 993 and timeout == 30
        assert ssl_context.check_hostname
        self.commands.append(("CONNECT",))

    def login(self, username, password):
        assert username == USERNAME and password == "isolated-only"
        self.commands.append(("LOGIN",))
        return "OK", []

    def select(self, folder, readonly=False):
        assert folder == '"SmartReport-Test"' and readonly is True
        self.commands.append(("EXAMINE", folder))
        count = len(self.messages) if self.count_override is None else self.count_override
        return "OK", [str(count).encode()]

    def response(self, name):
        assert name == "UIDVALIDITY"
        self.commands.append(("RESPONSE", name))
        return name, [str(self.epoch).encode()]

    def uid(self, action, *args):
        assert action in ("SEARCH", "FETCH"), f"Forbidden IMAP command: {action}"
        self.commands.append((action, *args))
        if action == "SEARCH":
            assert args == (None, "ALL")
            result = b" ".join(str(uid).encode() for uid in self.messages)
            return "OK", [result if self.search_override is None else self.search_override]
        uid = int(args[0])
        assert uid in (11, 20, 33, 41, 58), f"Read outside frozen selection: {uid}"
        if uid == self.fail_uid:
            raise OSError("synthetic interruption")
        if uid not in self.messages:
            return "OK", []
        returned = self.response_uid or uid
        if args[1] == "(UID RFC822.SIZE)":
            return "OK", [f"1 (UID {returned} RFC822.SIZE {len(self.messages[uid])})".encode()]
        assert args[1] == "(UID BODY.PEEK[])"
        return "OK", [(f"1 (UID {returned} BODY[] {{{len(self.messages[uid])}}})".encode(),
                       self.messages[uid]), b")"]

    def logout(self):
        self.commands.append(("LOGOUT",))
        return "BYE", []


class LimitedMailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.selection = self.root / "selected-uids.json"
        RestrictedIMAP.messages = {11: eml(pdf()), 20: eml(pdf("Another copy"))}
        RestrictedIMAP.commands = []
        RestrictedIMAP.epoch = 50
        RestrictedIMAP.fail_uid = RestrictedIMAP.response_uid = None
        RestrictedIMAP.count_override = RestrictedIMAP.search_override = None
        RestrictedIMAP.flags = {11: (), 20: ("\\Seen",)}
        app = Flask(__name__)
        self.store = register_mail_intake(
            app, root=self.root / "intake", token=TOKEN,
            mailbox=mailbox_key(HOST, USERNAME, FOLDER), enabled=True, recognizer=recognized)
        self.server = make_server("127.0.0.1", 0, app, threaded=True)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.api = IntakeClient(f"http://127.0.0.1:{self.server.server_port}", TOKEN, allow_local=True)

    def shutdown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def run_limited(self, **kwargs):
        return run_limited(self.api, HOST, USERNAME, "isolated-only",
                           kwargs.pop("folder", FOLDER), self.selection,
                           client_factory=RestrictedIMAP, **kwargs)

    def fetch_uids(self):
        return {int(command[1]) for command in RestrictedIMAP.commands if command[0] == "FETCH"}

    def test_two_messages_pdf_draft_and_repeat_without_other_folder_or_flags(self):
        before = dict(RestrictedIMAP.flags)
        first = self.run_limited()
        counts = self.store.status()["counts"]
        self.assertEqual(first["uids"], [11, 20])
        self.assertEqual(counts, {"messages": 2, "drafts": 2, "attachments": 2})
        RestrictedIMAP.commands.clear()
        second = self.run_limited()
        self.assertEqual(second["uids"], first["uids"])
        self.assertEqual(self.store.status()["counts"], counts)
        self.assertEqual(self.fetch_uids(), {11, 20})
        self.assertFalse(any(command[0] == "SEARCH" for command in RestrictedIMAP.commands))
        self.assertEqual(RestrictedIMAP.flags, before)
        self.assertEqual({command[1] for command in RestrictedIMAP.commands if command[0] == "EXAMINE"},
                         {'"SmartReport-Test"'})
        self.assertEqual(self.store.status()["conflicts"], 2)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
            self.assertIsNone(db.execute(
                "SELECT name FROM sqlite_master WHERE name='expense_documents'").fetchone())

    def test_exactly_five_can_be_pinned(self):
        RestrictedIMAP.messages = {uid: eml(pdf()) for uid in (11, 20, 33, 41, 58)}
        result = self.run_limited()
        self.assertEqual(len(result["messages"]), 5)
        self.assertEqual(self.fetch_uids(), {11, 20, 33, 41, 58})
        self.assertEqual(self.store.status()["counts"]["drafts"], 1)

    def test_restricted_pdf_to_recognized_draft_and_reopen(self):
        self.store.recognizer = bounded_recognize
        RestrictedIMAP.messages = {11: eml(pdf())}
        self.run_limited()
        first = self.api.call("GET", "/drafts")["drafts"]
        self.assertEqual(len(first), 1)
        fields = first[0]["result"]["fields"]
        self.assertEqual(fields["supplier_name"], "Test Supplier AB")
        self.assertEqual(fields["invoice_number"], "3539")
        self.assertEqual(fields["amount_due"], "125.00")
        self.assertEqual(fields["payment_reference"], "003539")
        self.run_limited()
        second = self.api.call("GET", "/drafts")["drafts"]
        self.assertEqual(first, second)
        self.assertEqual(self.store.status()["counts"]["messages"], 1)

    def test_more_than_five_stops_before_search_or_body(self):
        RestrictedIMAP.count_override = 6
        with self.assertRaisesRegex(WorkerError, "one_to_five"):
            self.run_limited()
        self.assertFalse(self.selection.exists())
        self.assertFalse(any(command[0] in ("SEARCH", "FETCH") for command in RestrictedIMAP.commands))
        self.assertEqual(self.store.status()["counts"]["messages"], 0)

    def test_empty_folder_does_not_create_selection(self):
        RestrictedIMAP.messages = {}
        with self.assertRaisesRegex(WorkerError, "one_to_five"):
            self.run_limited()
        self.assertFalse(self.selection.exists())

    def test_new_mail_is_never_added_to_repeat(self):
        self.run_limited()
        RestrictedIMAP.messages[999] = eml(pdf("Not approved"))
        RestrictedIMAP.commands.clear()
        self.run_limited()
        self.assertEqual(self.fetch_uids(), {11, 20})
        self.assertEqual(self.store.status()["counts"]["messages"], 2)
        self.assertNotIn(999, json.loads(self.selection.read_text())["uids"])

    def test_epoch_change_stops_before_fetch_without_rescan_or_reset(self):
        self.run_limited()
        RestrictedIMAP.epoch += 1
        RestrictedIMAP.commands.clear()
        with self.assertRaisesRegex(WorkerError, "uidvalidity_changed"):
            self.run_limited()
        self.assertFalse(any(command[0] in ("SEARCH", "FETCH") for command in RestrictedIMAP.commands))
        self.assertEqual(self.store.status()["epoch"], "50")

    def test_interruption_resumes_frozen_set_without_duplicate(self):
        RestrictedIMAP.fail_uid = 20
        with self.assertRaises(OSError):
            self.run_limited()
        self.assertTrue(self.selection.exists())
        self.assertEqual(self.store.status()["counts"]["messages"], 1)
        RestrictedIMAP.fail_uid = None
        RestrictedIMAP.messages[999] = eml(pdf("Outside selection"))
        RestrictedIMAP.commands.clear()
        self.run_limited()
        self.assertEqual(self.fetch_uids(), {11, 20})
        self.assertEqual(self.store.status()["counts"]["messages"], 2)
        self.assertEqual(self.store.status()["pending_recognition"], 0)

    def test_lost_ack_after_receipt_commit_repeats_only_frozen_set(self):
        actual = self.api.call
        lost = [True]

        def call(method, path, **kwargs):
            result = actual(method, path, **kwargs)
            if path == "/messages" and lost[0]:
                lost[0] = False
                raise requests.ConnectionError("synthetic lost ACK")
            return result

        with patch.object(self.api, "call", side_effect=call):
            with self.assertRaises(requests.ConnectionError):
                self.run_limited()
        self.run_limited()
        self.assertEqual(self.store.status()["counts"]["messages"], 2)
        self.assertEqual(self.store.status()["counts"]["drafts"], 2)

    def test_missing_selected_message_stops_without_substitute(self):
        self.run_limited()
        del RestrictedIMAP.messages[20]
        RestrictedIMAP.messages[999] = eml(pdf("Replacement"))
        RestrictedIMAP.commands.clear()
        with self.assertRaisesRegex(WorkerError, "selected_message_missing"):
            self.run_limited()
        self.assertEqual(self.fetch_uids(), {11, 20})
        self.assertEqual(self.store.status()["counts"]["messages"], 2)

    def test_response_for_wrong_uid_stops_before_receipt(self):
        RestrictedIMAP.response_uid = 999
        with self.assertRaisesRegex(WorkerError, "fetch_uid_mismatch"):
            self.run_limited()
        self.assertEqual(self.store.status()["counts"]["messages"], 0)

    def test_corrupt_selection_is_not_regenerated(self):
        self.selection.write_text("{interrupted", encoding="utf-8")
        with self.assertRaisesRegex(WorkerError, "selection_file_invalid"):
            self.run_limited()
        self.assertEqual(RestrictedIMAP.commands, [])

    def test_folder_or_account_change_rejected_before_connect(self):
        self.run_limited()
        RestrictedIMAP.commands.clear()
        with self.assertRaisesRegex(WorkerError, "selection_identity_mismatch"):
            self.run_limited(folder="Other-Folder")
        self.assertEqual(RestrictedIMAP.commands, [])

    def test_inbox_control_characters_and_remote_api_rejected_before_connect(self):
        for folder in ("INBOX", "inbox", "", "x\r\nSELECT INBOX", "å"):
            with self.subTest(folder=folder):
                with self.assertRaises(WorkerError):
                    self.run_limited(folder=folder)
        original = self.api.url
        self.api.url = "https://remote.invalid/api/mail-intake"
        try:
            with self.assertRaisesRegex(WorkerError, "loopback"):
                self.run_limited()
        finally:
            self.api.url = original
        self.assertEqual(RestrictedIMAP.commands, [])
        self.assertEqual(quoted_folder('Folder "A"'), '"Folder \\"A\\""')

    def test_selection_is_saved_before_first_message_fetch(self):
        original = RestrictedIMAP.uid

        def check(instance, action, *args):
            if action == "FETCH":
                value = json.loads(self.selection.read_text())
                self.assertEqual(value["uids"], [11, 20])
                self.assertNotIn("isolated-only", self.selection.read_text())
            return original(instance, action, *args)

        with patch.object(RestrictedIMAP, "uid", check):
            self.run_limited()

    def test_inconsistent_search_stops_before_body(self):
        RestrictedIMAP.search_override = b"11 11"
        with self.assertRaisesRegex(WorkerError, "selection_uids"):
            self.run_limited()
        self.assertEqual(self.fetch_uids(), set())

    def test_python_imap_readonly_uses_examine_not_select(self):
        client = object.__new__(imaplib.IMAP4)

        def command(name, folder):
            self.assertEqual(name, "EXAMINE")
            self.assertEqual(folder, '"SmartReport-Test"')
            client.untagged_responses["EXISTS"] = [b"2"]
            return "OK", [b"READ-ONLY"]

        client._simple_command = Mock(side_effect=command)
        self.assertEqual(client.select('"SmartReport-Test"', readonly=True), ("OK", [b"2"]))
        self.assertTrue(client.is_readonly)

    def test_wrong_body_uid_is_not_saved(self):
        original = RestrictedIMAP.uid

        def wrong_body(instance, action, *args):
            result = original(instance, action, *args)
            if action == "FETCH" and args[-1] == "(UID BODY.PEEK[])":
                header, raw = result[1][0]
                return "OK", [(header.replace(b"UID 11", b"UID 999"), raw)]
            return result

        with patch.object(RestrictedIMAP, "uid", wrong_body):
            with self.assertRaisesRegex(WorkerError, "fetch_uid_mismatch"):
                self.run_limited()
        self.assertEqual(self.store.status()["counts"]["messages"], 0)

    def test_selection_cannot_be_recreated_over_existing_queue(self):
        self.run_limited()
        self.selection.unlink()
        RestrictedIMAP.commands.clear()
        with self.assertRaisesRegex(WorkerError, "requires_empty_intake"):
            self.run_limited()
        self.assertEqual(RestrictedIMAP.commands, [])

    def test_folder_grows_past_five_on_retry_stops_before_fetch(self):
        self.run_limited()
        RestrictedIMAP.count_override = 6
        RestrictedIMAP.commands.clear()
        with self.assertRaisesRegex(WorkerError, "one_to_five"):
            self.run_limited()
        self.assertEqual(self.fetch_uids(), set())
        self.assertFalse(any(command[0] == "SEARCH" for command in RestrictedIMAP.commands))


if __name__ == "__main__":
    unittest.main()
