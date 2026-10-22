import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from folder_mail import FOLDER, HOST, ADDRESS, FolderError, import_folder
from incoming_documents import IncomingStore
from test_mail_automation import eml, pdf, recognized


CalendarDate = date


class ImportDateType(type):
    def __instancecheck__(cls, value):
        return isinstance(value, CalendarDate)


class ImportTestDate(date, metaclass=ImportDateType):
    @classmethod
    def today(cls):
        return cls(2027, 1, 15)


class FolderIMAP:
    messages = {}
    commands = []
    epoch = 50
    fail = None
    dates = {}

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
            assert len(args) == 5 and args[0] is None and args[1] == "SINCE" and args[3] == "BEFORE"
            from datetime import datetime
            start = datetime.strptime(args[2], "%d-%b-%Y").date()
            end = datetime.strptime(args[4], "%d-%b-%Y").date()
            return "OK", [b" ".join(str(uid).encode() for uid in self.messages
                                    if start <= self.dates.get(uid, date.today()) < end)]
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
        for name in ("folder_mail.date", __name__ + ".date"):
            date_patch = patch(name, ImportTestDate)
            date_patch.start()
            self.addCleanup(date_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = IncomingStore(Path(self.temp.name) / "incoming", SimpleNamespace())
        self.addCleanup(self.store.close)
        FolderIMAP.messages = {uid: eml(pdf(f"Incoming PDF {uid}")) for uid in range(1, 8)}
        FolderIMAP.commands = []
        FolderIMAP.epoch = 50
        FolderIMAP.fail = None
        FolderIMAP.dates = {}

    def run_import(self):
        return import_folder(self.store, "isolated-only", client_factory=FolderIMAP, recognizer=recognized)

    def test_all_folder_messages_no_five_cap_and_replay_zero_new(self):
        self.assertEqual(self.run_import()["new_drafts"], 7)
        before = self.store.conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
        report = self.run_import()
        self.assertEqual(report["new_drafts"], 0)
        self.assertEqual(report["duplicates"], 0)
        self.assertEqual(report["skipped"], 7)
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
        self.assertEqual(report["skipped"], 3)

    def test_uidvalidity_change_is_explicit_and_completed_uid_never_refetched(self):
        self.run_import()
        FolderIMAP.epoch = 51
        with self.assertRaisesRegex(FolderError, "epoch_changed"):
            self.run_import()
        FolderIMAP.epoch = 50
        FolderIMAP.messages[1] = eml(pdf("Changed UID"))
        FolderIMAP.commands = []
        result = self.run_import()
        self.assertEqual(result["skipped"], 7)
        self.assertFalse(any(command[0] == "FETCH" for command in FolderIMAP.commands))

    def test_failed_recognition_keeps_original_for_manual_fields(self):
        result = import_folder(self.store, "isolated-only", client_factory=FolderIMAP,
                               recognizer=lambda paths: {"fields": {}, "state": "failed"})
        self.assertEqual(result["recognition_failed"], 7)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0], 0)
        retry = self.run_import()
        self.assertEqual(retry["new_drafts"], 0)
        self.assertEqual(retry["recognition_failed"], 0)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0], 7)
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

    def test_completed_messages_skip_all_fetch_and_ocr_but_new_uid_checks_attachment_hash(self):
        from unittest.mock import Mock
        self.run_import()
        FolderIMAP.commands = []
        recognizer = Mock(side_effect=AssertionError("OCR must not run"))
        result = import_folder(self.store, "isolated-only", client_factory=FolderIMAP, recognizer=recognizer)
        self.assertEqual(result["skipped"], 7)
        self.assertEqual(result["messages"], 0)
        self.assertEqual(result["duplicates"], 0)
        self.assertFalse(any(command[0] == "FETCH" for command in FolderIMAP.commands))
        recognizer.assert_not_called()
        FolderIMAP.messages[2000] = FolderIMAP.messages[1]
        result = import_folder(self.store, "isolated-only", client_factory=FolderIMAP, recognizer=recognizer)
        self.assertEqual(result["messages"], 1)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["new_drafts"], 0)
        recognizer.assert_not_called()

    def test_recent_14_days_inclusive_boundaries_and_explicit_archive(self):
        from folder_mail import recent_period, imap_date
        today = date.today()
        start, end = recent_period()
        self.assertEqual(start, today - timedelta(days=13))
        self.assertEqual(end, today)
        FolderIMAP.dates = {1: start - timedelta(days=1), 2: start, 3: today,
                            4: today + timedelta(days=1), 5: start - timedelta(days=2),
                            6: start - timedelta(days=3), 7: start - timedelta(days=4)}
        self.assertEqual(self.run_import()["new_drafts"], 2)
        self.assertIn(("SEARCH", None, "SINCE", imap_date(start), "BEFORE",
                       imap_date(end + timedelta(days=1))), FolderIMAP.commands)
        result = import_folder(self.store, "isolated-only", start=date(2026, 11, 1),
                               end=start - timedelta(days=1), client_factory=FolderIMAP, recognizer=recognized)
        self.assertEqual(result["new_drafts"], 4)
        self.assertEqual(len(self.store.documents()), 6)
        # Selecting a different period never filters or deletes existing drafts.
        self.assertEqual(self.run_import()["skipped"], 2)
        self.assertEqual(len(self.store.documents()), 6)

    def test_import_start_boundary_and_14_day_window(self):
        from folder_mail import recent_period
        self.assertEqual(recent_period(date(2026, 11, 1)), (date(2026, 11, 1), date(2026, 11, 1)))
        self.assertEqual(recent_period(date(2026, 11, 14)), (date(2026, 11, 1), date(2026, 11, 14)))
        self.assertEqual(recent_period(date(2026, 11, 15)), (date(2026, 11, 2), date(2026, 11, 15)))
        from unittest.mock import Mock
        factory = Mock(side_effect=AssertionError("No IMAP before approved start"))
        class BeforeStart(date):
            @classmethod
            def today(cls):
                return cls(2026, 10, 31)
        with patch("folder_mail.date", BeforeStart):
            with self.assertRaisesRegex(FolderError, "import_not_started"):
                import_folder(self.store, "isolated-only", client_factory=factory)
        factory.assert_not_called()
        FolderIMAP.dates = {uid: date(2026, 10, 31) for uid in FolderIMAP.messages}
        FolderIMAP.dates[2] = date(2026, 11, 1)
        report = import_folder(self.store, "isolated-only", start=date(2026, 10, 1),
                               end=date(2026, 11, 1), client_factory=FolderIMAP, recognizer=recognized)
        self.assertEqual(report["start"], "2026-11-01")
        self.assertEqual(report["new_drafts"], 1)
        with self.assertRaisesRegex(FolderError, "before_import_start"):
            import_folder(self.store, "isolated-only", start=date(2026, 10, 1),
                          end=date(2026, 10, 31), client_factory=factory)

    def test_invalid_period_never_connects(self):
        from unittest.mock import Mock
        factory = Mock(side_effect=AssertionError("No IMAP"))
        for start, end in ((date.today(), date.today() - timedelta(days=1)),
                           (date.today(), date.today() + timedelta(days=1)), (None, date.today())):
            with self.assertRaisesRegex(FolderError, "period"):
                import_folder(self.store, "isolated-only", start=start, end=end, client_factory=factory)
        factory.assert_not_called()

    def test_stop_after_first_pdf_of_multi_attachment_message_resumes_without_duplicate(self):
        from email.message import EmailMessage
        from threading import Event
        stop = Event()
        message = EmailMessage()
        message.set_content("Synthetic multi-attachment fixture")
        for index in (1, 2):
            message.add_attachment(pdf(f"Part {index}"), maintype="application",
                                   subtype="pdf", filename=f"part-{index}.pdf")
        FolderIMAP.messages = {1: message.as_bytes()}
        def recognize(paths):
            stop.set()
            return recognized(paths)
        result = import_folder(self.store, "isolated-only", client_factory=FolderIMAP,
                               recognizer=recognize, should_stop=stop.is_set)
        self.assertTrue(result["stopped"])
        self.assertEqual(len(self.store.documents()), 1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0], 0)
        history = self.store.history(1)
        result = self.run_import()
        self.assertEqual(result["new_drafts"], 1)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(len(self.store.documents()), 2)
        self.assertEqual(self.store.history(1), history)
        self.assertEqual(self.run_import()["skipped"], 1)

    def test_failed_pdf_in_multi_attachment_message_retries_only_failed_ocr(self):
        from email.message import EmailMessage
        message = EmailMessage()
        message.set_content("Synthetic fixture")
        for index in (1, 2):
            message.add_attachment(pdf(f"Part {index}"), maintype="application",
                                   subtype="pdf", filename=f"part-{index}.pdf")
        FolderIMAP.messages = {1: message.as_bytes()}
        calls = []
        def first_recognition(paths):
            calls.append(paths)
            return recognized(paths) if len(calls) == 1 else {"state": "failed", "fields": {}}
        report = import_folder(self.store, "isolated-only", client_factory=FolderIMAP, recognizer=first_recognition)
        self.assertEqual(report["recognition_failed"], 1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0], 0)
        calls.clear()
        def retry_recognition(paths):
            calls.append(paths)
            return recognized(paths)
        report = import_folder(self.store, "isolated-only", client_factory=FolderIMAP, recognizer=retry_recognition)
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["new_drafts"], 0)
        self.assertEqual(report["duplicates"], 2)
        self.assertEqual(self.run_import()["skipped"], 1)

    def test_stop_before_connect_and_progress_are_safe_and_do_not_include_credentials(self):
        from unittest.mock import Mock
        factory = Mock(side_effect=AssertionError("No IMAP"))
        reports = []
        result = import_folder(self.store, "isolated-only", client_factory=factory,
                               should_stop=lambda: True, progress=reports.append)
        self.assertTrue(result["stopped"])
        factory.assert_not_called()
        self.assertNotIn("isolated-only", json.dumps(reports))
        reports.clear()
        import_folder(self.store, "isolated-only", client_factory=FolderIMAP,
                      recognizer=recognized, progress=reports.append)
        self.assertEqual(reports[-1]["completed"], 7)
        self.assertEqual(reports[-1]["total"], 7)
        self.assertEqual(reports[-1]["phase"], "finished")
        self.assertIn("recognizing", [report["phase"] for report in reports])
