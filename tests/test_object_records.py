import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

import desktop_auth
import info_mail
from object_records import ObjectRecordStore, export_backup_files


class FakeIMAP:
    def __init__(self, _host, _port, **_kwargs):
        self.calls = []
        self.message = EmailMessage()
        self.message["From"] = "Client <client@example.test>"
        self.message["To"] = "info@example.test"
        self.message["Subject"] = "Additional kitchen work"
        self.message["Date"] = "Wed, 12 Nov 2026 10:00:00 +0000"
        self.message["Message-ID"] = "<request-123@example.test>"
        self.message.set_content("Please add two shelves.")
        self.message.add_attachment(
            b"%PDF-1.4 test", maintype="application", subtype="pdf", filename="request.pdf",
        )
        self.message.add_attachment(
            b"word attachment", maintype="application",
            subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename="drawing.docx",
        )
        self.raw = self.message.as_bytes()

    def login(self, _user, _password):
        self.calls.append(("login",))
        return "OK", [b"authenticated"]

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"1"]

    def response(self, name):
        self.calls.append(("response", name))
        return name, [b"77"]

    def uid(self, command, *args):
        self.calls.append(("uid", command, args))
        if command == "SEARCH":
            return "OK", [b"9"]
        if command == "FETCH" and "HEADER" in args[1]:
            header = self.message.as_bytes().split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
            prefix = f"9 (UID 9 RFC822.SIZE {len(self.raw)} BODY[HEADER] {{{len(header)}}}".encode()
            return "OK", [(prefix, header), b")"]
        if command == "FETCH" and "BODY.PEEK[]" in args[1]:
            prefix = f"9 (BODY[] {{{len(self.raw)}}}".encode()
            return "OK", [(prefix, self.raw), b")"]
        raise AssertionError(f"Unexpected IMAP command: {command} {args}")

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b"logged out"]


class ObjectRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="smartreport-object-records-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ObjectRecordStore(self.root / "object_documents")
        self.one = "sync:object-one"
        self.two = "sync:object-two"

    def test_file_mail_and_agreement_survive_reopen_on_the_correct_object(self):
        source = self.root / "contract.pdf"
        source.write_bytes(b"%PDF-1.4 local contract")
        self.store.add_file(
            self.one, "Kitchen renovation", "Client AB", source, "Договор",
        )
        agreement_id = self.store.add_agreement(
            self.one, "Kitchen renovation", "Client AB", "2026-11-12",
            "Add two shelves at the customer's request.", True, source,
        )
        message_id, created, added = self.store.add_email(
            self.one, "Kitchen renovation", "Client AB",
            email_key="message-key-1", sender="client@example.test",
            subject="Additional work", email_date="2026-11-12T10:00:00+00:00",
            body="Please add two shelves.",
            attachments=[{"part": 1, "filename": "request.pdf", "content": b"%PDF-1.4 email"}],
        )
        self.assertTrue(created)
        self.assertEqual(added, 1)
        self.store.add_file(self.two, "Other object", "Other AB", source, "Фото")

        reopened = ObjectRecordStore(self.root / "object_documents")
        records = reopened.list_documents(self.one)
        agreements = reopened.list_agreements(self.one)
        self.assertEqual(len(records), 3)
        self.assertEqual({row["category"] for row in records}, {"Договор", "Переписка"})
        self.assertEqual(len(reopened.list_documents(self.two)), 1)
        self.assertEqual(len(agreements), 1)
        self.assertEqual(agreements[0]["id"], agreement_id)
        self.assertEqual(agreements[0]["text"], "Add two shelves at the customer's request.")
        self.assertEqual(agreements[0]["agreed"], 1)
        self.assertEqual(agreements[0]["original_name"], "contract.pdf")
        mail = next(row for row in records if row["email_part"] == -1)
        self.assertEqual(mail["id"], message_id)
        self.assertEqual(mail["email_body"], "Please add two shelves.")

    def test_offer_status_can_be_updated_without_replacing_its_file(self):
        source = self.root / "offer.pdf"
        source.write_bytes(b"%PDF-1.4 offer")
        document_id = self.store.add_file(
            self.one, "Site", "Client", source, "Оферта",
            offer_version=1, offer_status="Черновик",
        )
        self.store.update_offer_status(document_id, "Отправлена")
        offer = self.store.document(document_id)
        self.assertEqual((offer["offer_version"], offer["offer_status"]), (1, "Отправлена"))
        self.assertEqual(
            self.store.file_path(offer["relative_path"], offer["sha256"]).read_bytes(),
            b"%PDF-1.4 offer",
        )

    def test_readding_same_email_to_same_object_is_idempotent(self):
        attachment = {"part": 3, "filename": "request.pdf", "content": b"%PDF-1.4"}
        first = self.store.add_email(
            self.one, "Site", "Client", email_key="stable-message-id",
            sender="client@example.test", subject="Request", email_date="2026-11-12",
            body="Request text", attachments=[attachment],
        )
        second = self.store.add_email(
            self.one, "Site", "Client", email_key="stable-message-id",
            sender="client@example.test", subject="Request", email_date="2026-11-12",
            body="Request text", attachments=[attachment],
        )
        other_object = self.store.add_email(
            self.two, "Other site", "Other client", email_key="stable-message-id",
            sender="client@example.test", subject="Request", email_date="2026-11-12",
            body="Request text", attachments=[attachment],
        )
        self.assertEqual(first, (second[0], True, 1))
        self.assertEqual(second, (first[0], False, 0))
        self.assertTrue(other_object[1])
        self.assertEqual(len(self.store.list_documents(self.one)), 2)
        self.assertEqual(len(self.store.list_documents(self.two)), 2)

    def test_removing_email_attachment_unlinks_its_group_only_and_allows_readd(self):
        payload = b"%PDF-1.4 shared attachment"
        attachment = {"part": 1, "filename": "request.pdf", "content": payload}
        self.store.add_email(
            self.one, "Site", "Client", email_key="removable-message",
            sender="client@example.test", subject="Request", email_date="2026-11-12",
            body="Please add one shelf.", attachments=[attachment],
        )
        manual_source = self.root / "manual-copy.pdf"
        manual_source.write_bytes(payload)
        manual_document_id = self.store.add_file(
            self.one, "Site", "Client", manual_source, "Фото",
        )
        agreement_id = self.store.add_agreement(
            self.one, "Site", "Client", "2026-11-12", "Keep this agreement.",
            True, manual_source,
        )
        other_object = self.store.add_email(
            self.two, "Other site", "Other client", email_key="removable-message",
            sender="client@example.test", subject="Request", email_date="2026-11-12",
            body="Please add one shelf.", attachments=[attachment],
        )

        attachment_row = next(
            row for row in self.store.list_documents(self.one)
            if row["email_part"] == 1
        )
        self.assertEqual(self.store.remove_document(attachment_row["id"]), 2)

        reopened = ObjectRecordStore(self.root / "object_documents")
        one_documents = reopened.list_documents(self.one)
        self.assertEqual([row["id"] for row in one_documents], [manual_document_id])
        self.assertEqual(one_documents[0]["category"], "Фото")
        self.assertEqual(
            reopened.file_path(one_documents[0]["relative_path"], one_documents[0]["sha256"])
            .read_bytes(),
            payload,
        )
        self.assertEqual(len(reopened.list_agreements(self.one)), 1)
        self.assertEqual(reopened.list_agreements(self.one)[0]["id"], agreement_id)
        self.assertEqual(len(reopened.list_documents(self.two)), 2)
        self.assertTrue(other_object[1])

        readded = reopened.add_email(
            self.one, "Site", "Client", email_key="removable-message",
            sender="client@example.test", subject="Request", email_date="2026-11-12",
            body="Please add one shelf.", attachments=[attachment],
        )
        self.assertTrue(readded[1])
        self.assertEqual(readded[2], 1)
        self.assertEqual(len(reopened.list_documents(self.one)), 3)
        duplicate = reopened.add_email(
            self.one, "Site", "Client", email_key="removable-message",
            sender="client@example.test", subject="Request", email_date="2026-11-12",
            body="Please add one shelf.", attachments=[attachment],
        )
        self.assertFalse(duplicate[1])
        self.assertEqual(duplicate[2], 0)
        self.assertEqual(len(reopened.list_documents(self.one)), 3)

    def test_removing_a_file_keeps_other_links_to_the_same_stored_content(self):
        source = self.root / "shared-file.pdf"
        source.write_bytes(b"%PDF-1.4 shared")
        first_id = self.store.add_file(self.one, "Site", "Client", source, "Договор")
        second_id = self.store.add_file(self.one, "Site", "Client", source, "Фото")
        self.store.add_file(self.two, "Other site", "Other client", source, "Фото")

        self.assertEqual(self.store.remove_document(first_id), 1)

        one_documents = self.store.list_documents(self.one)
        self.assertEqual([row["id"] for row in one_documents], [second_id])
        self.assertEqual(len(self.store.list_documents(self.two)), 1)
        self.assertEqual(
            self.store.file_path(one_documents[0]["relative_path"], one_documents[0]["sha256"])
            .read_bytes(),
            b"%PDF-1.4 shared",
        )

    def test_info_mail_reads_headers_and_body_with_read_only_peek_commands(self):
        settings = info_mail.MailboxSettings(
            "info@example.test", "imap.example.test", 993, "SSL/TLS", "INBOX", "Sent",
        )
        clients = []

        def client_factory(*args, **kwargs):
            client = FakeIMAP(*args, **kwargs)
            clients.append(client)
            return client

        summaries = info_mail.list_messages(
            settings, "local-test-password", "INBOX",
            now=datetime(2026, 11, 12, tzinfo=timezone.utc), client_factory=client_factory,
        )
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].subject, "Additional kitchen work")
        content = info_mail.fetch_message(
            settings, "local-test-password", summaries[0], client_factory=client_factory,
        )
        self.assertIn("two shelves", content.body)
        self.assertEqual([item.filename for item in content.attachments], ["request.pdf", "drawing.docx"])
        self.assertEqual(content.key, hashlib.sha256(
            b"info@example.test\0<request-123@example.test>"
        ).hexdigest())
        for client in clients:
            self.assertTrue(all(call[2] is True for call in client.calls if call[0] == "select"))
            self.assertTrue(all(
                call[1] in ("SEARCH", "FETCH")
                for call in client.calls if call[0] == "uid"
            ))
            self.assertFalse(any(call[0] in ("store", "expunge", "move", "close")
                                 for call in client.calls))
            self.assertTrue(all(
                "PEEK" in str(call) for call in client.calls if call[0] == "uid" and call[1] == "FETCH"
            ))

    def test_sent_folder_and_connection_check_are_read_only(self):
        settings = info_mail.MailboxSettings(
            "info@example.test", "imap.example.test", 993, "SSL/TLS", "INBOX", "Sent Items",
        )
        client = FakeIMAP("imap.example.test", 993)
        info_mail.test_connection(settings, "local-test-password", client_factory=lambda *_args, **_kwargs: client)
        self.assertIn(("select", '"INBOX"', True), client.calls)
        self.assertIn(("select", '"Sent Items"', True), client.calls)
        sent_client = FakeIMAP("imap.example.test", 993)
        sent_messages = info_mail.list_messages(
            settings, "local-test-password", "Sent Items",
            now=datetime(2026, 11, 12, tzinfo=timezone.utc),
            client_factory=lambda *_args, **_kwargs: sent_client,
        )
        self.assertEqual(len(sent_messages), 1)
        self.assertIn(("select", '"Sent Items"', True), sent_client.calls)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI is platform-specific")
    def test_windows_dpapi_round_trips_mail_secret(self):
        secret = b"isolated-info-mail-password"
        protected = desktop_auth._protect(secret)
        self.assertTrue(protected)
        self.assertNotEqual(protected, secret)
        self.assertEqual(desktop_auth._unprotect(protected), secret)

    def test_mail_secret_is_dpapi_protected_and_backup_restores_object_files(self):
        settings = info_mail.MailboxSettings(
            "info@example.test", "imap.example.test", 993, "SSL/TLS", "INBOX", "Sent",
        )
        with patch.object(desktop_auth, "_protect", side_effect=lambda _raw: b"opaque-dpapi-blob"), \
                patch.object(desktop_auth, "_unprotect", return_value=b"mail-password-not-in-chat"):
            info_mail.save_settings(settings, "mail-password-not-in-chat", self.root / "profile")
            settings_file = self.root / "profile" / "info_mail_settings.json"
            self.assertNotIn(b"mail-password-not-in-chat", settings_file.read_bytes())
            loaded = info_mail.load_settings(self.root / "profile")
            self.assertEqual(loaded, (settings, "mail-password-not-in-chat"))

        source = self.root / "offer.docx"
        source.write_bytes(b"word file")
        self.store.add_file(
            self.one, "Site", "Client", source, "Оферта",
            offer_version=2, offer_status="Согласована",
        )
        backup = export_backup_files(self.store.root)
        self.assertIn("profile/object_documents/object_records.sqlite3", backup)
        self.assertTrue(any(name.endswith(".docx") for name in backup))

        import portable_recovery

        encrypted = portable_recovery.encrypt(backup, "test-recovery-password-1234")
        restored_files = portable_recovery.decrypt(encrypted, "test-recovery-password-1234")
        restored_root = self.root / "restored"
        portable_recovery.restore(restored_files, restored_root)
        restored = ObjectRecordStore(restored_root / "profile" / "object_documents")
        self.assertEqual(
            portable_recovery.verify_object_documents(restored_root),
            {"present": True, "integrity": True, "files": 1, "records": 1,
             "links": 1, "pdf_files": 0, "pdfs_readable": 0},
        )
        offer = restored.list_documents(self.one)[0]
        self.assertEqual((offer["offer_version"], offer["offer_status"]), (2, "Согласована"))
        path = restored.file_path(offer["relative_path"], offer["sha256"])
        self.assertEqual(path.read_bytes(), b"word file")


if __name__ == "__main__":
    unittest.main()
