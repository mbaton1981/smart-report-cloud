import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from folder_mail import FOLDER, HOST, ADDRESS, FolderError, import_folder
from incoming_documents import IncomingStore
from test_mail_automation import eml, pdf, recognized


class FolderIMAP:
    messages = {}
    commands = []
    epoch = 50
    fail = None

    def __init__(self, host, port, ssl_context, timeout):
        assert host == HOST and port == 993 and ssl_context.check_hostname

    def login(self, address, password):
        assert address == ADDRESS and password == "isolated-only"
        return "OK", []

    def select(self, folder, readonly):
        assert folder == '"' + FOLDER + '"' and readonly is True
        self.commands.append(("EXAMINE", folder))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, name):
        assert name == "UIDVALIDITY"
        return name, [str(self.epoch).encode()]

    def uid(self, action, *args):
        assert action in ("SEARCH", "FETCH")
        self.commands.append((action, *args))
        if action == "SEARCH":
            assert args == (None, "ALL")
            return "OK", [b" ".join(str(uid).encode() for uid in self.messages)]
        uid = int(args[0])
        if self.fail == uid:
            raise OSError("simulated connection loss")
        raw = self.messages[uid]
        if args[1] == "(UID RFC822.SIZE)":
            return "OK", [f"1 (UID {uid} RFC822.SIZE {len(raw)})".encode()]
        assert args[1] == "(UID BODY.PEEK[])"
        return "OK", [(f"1 (UID {uid} BODY[] {{{len(raw)}}})".encode(), raw), b")"]

    def logout(self):
        self.commands.append(("LOGOUT",))


class FolderMailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = IncomingStore(Path(self.temp.name) / "incoming", SimpleNamespace())
        self.addCleanup(self.store.close)
        FolderIMAP.messages = {uid: eml(pdf(f"Incoming PDF {uid}")) for uid in range(1, 8)}
        FolderIMAP.commands = []
        FolderIMAP.epoch = 50
        FolderIMAP.fail = None

    def run_import(self):
        return import_folder(self.store, "isolated-only", client_factory=FolderIMAP, recognizer=recognized)

    def test_all_folder_messages_no_five_cap_and_replay_zero_new(self):
        self.assertEqual(self.run_import()["new_drafts"], 7)
        before = self.store.conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
        report = self.run_import()
        self.assertEqual(report["new_drafts"], 0)
        self.assertEqual(report["duplicates"], 7)
        self.assertEqual(len(self.store.documents()), 7)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0], before)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM register").fetchone()[0], 0)
        for path in self.store.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"isolated-only", path.read_bytes())

    def test_interruption_resumes_without_duplicates(self):
        FolderIMAP.fail = 4
        with self.assertRaises(OSError):
            self.run_import()
        self.assertEqual(len(self.store.documents()), 3)
        FolderIMAP.fail = None
        report = self.run_import()
        self.assertEqual(report["new_drafts"], 4)
        self.assertEqual(report["duplicates"], 3)

    def test_uidvalidity_change_and_changed_message_are_explicit(self):
        self.run_import()
        FolderIMAP.epoch = 51
        with self.assertRaisesRegex(FolderError, "epoch_changed"):
            self.run_import()
        FolderIMAP.epoch = 50
        FolderIMAP.messages[1] = eml(pdf("Changed UID"))
        with self.assertRaisesRegex(FolderError, "message_changed"):
            self.run_import()

    def test_failed_recognition_keeps_original_for_manual_fields(self):
        result = import_folder(self.store, "isolated-only", client_factory=FolderIMAP,
                               recognizer=lambda paths: {"fields": {}, "state": "failed"})
        self.assertEqual(result["recognition_failed"], 7)
        for doc in self.store.documents():
            self.assertTrue(self.store.original(doc["id"]).is_file())

    def test_primary_inbox_cross_folder_duplicate_preserves_manual_review_and_status(self):
        from unittest.mock import patch
        self.assertEqual(FOLDER, "INBOX")
        with patch("folder_mail.FOLDER", "INBOX.SmartReport-Test"):
            # The same IMAP protocol mock accepts only the current folder.
            with patch(__name__ + ".FOLDER", "INBOX.SmartReport-Test"):
                self.assertEqual(self.run_import()["new_drafts"], 7)
        revision, fields = self.store.latest(1)
        fields["supplier_name"] = "Manually verified seller"
        self.store.save(1, fields, revision)
        self.store.set_action(1, "later", 0)
        history = self.store.history(1)
        originals = list(self.store.conn.execute("SELECT sha256,content FROM originals"))
        # UIDs and UIDVALIDITY are folder-scoped; PDF identity is global.
        FolderIMAP.messages = {uid + 10000: raw for uid, raw in FolderIMAP.messages.items()}
        FolderIMAP.epoch = 900
        report = self.run_import()
        self.assertEqual(report["new_drafts"], 0)
        self.assertEqual(report["duplicates"], 7)
        self.assertEqual(self.store.latest(1)[1]["supplier_name"], "Manually verified seller")
        self.assertEqual(self.store.history(1), history)
        self.assertEqual(self.store.action(1)[1], "later")
        self.assertEqual(list(self.store.conn.execute("SELECT sha256,content FROM originals")), originals)
        self.assertEqual(self.run_import()["new_drafts"], 0)
        self.assertIn(("EXAMINE", '"INBOX"'), FolderIMAP.commands)
        self.assertTrue(all(cmd[0] in {"EXAMINE", "SEARCH", "FETCH", "LOGOUT"}
                            for cmd in FolderIMAP.commands))

    def test_all_uids_without_cap_or_pdf_create_no_automatic_expenses(self):
        from email.message import EmailMessage
        message = EmailMessage()
        message.set_content("Message without a document")
        FolderIMAP.messages = {uid: eml(pdf(f"Invoice {uid}")) for uid in range(1001, 1102)}
        FolderIMAP.messages[50000] = message.as_bytes()
        report = self.run_import()
        self.assertEqual(report["messages"], 102)
        self.assertEqual(report["new_drafts"], 101)
        self.assertEqual(report["without_pdf"], 1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM register").fetchone()[0], 0)
        self.assertEqual(self.run_import()["new_drafts"], 0)

    def test_pending_document_with_manual_revision_is_not_replaced_on_repeat(self):
        from email import policy
        from email.parser import BytesParser
        message = BytesParser(policy=policy.default).parsebytes(FolderIMAP.messages[1])
        content = next(message.iter_attachments()).get_payload(decode=True)
        self.store.add_pdf(content, "pending.pdf", {"fields": {}, "state": "pending"})
        revision, payload = self.store.latest(1)
        payload["supplier_name"] = "Manual correction after interrupted OCR"
        self.store.save(1, payload, revision)
        self.assertEqual(self.run_import()["new_drafts"], 6)
        self.assertEqual(self.store.latest(1)[1]["supplier_name"], payload["supplier_name"])
