from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import patch

import accountant_mail as mail
from info_mail import MailboxSettings


class AccountantMailTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        profile = patch.object(mail.desktop_auth, "app_data_dir", return_value=root)
        profile.start()
        self.addCleanup(profile.stop)
        transport = patch.object(mail.smtplib, "SMTP_SSL")
        self.transport = transport.start()
        self.addCleanup(transport.stop)
        self.path = root / "Faktureringsunderlag.pdf"
        self.path.write_bytes(b"%PDF-1.4 selected package")
        self.settings = MailboxSettings("info@example.org", mail.MAIL_HOST, 993, "SSL/TLS", "INBOX", "Sent")

    def send(self, recipient="accounts@example.org"):
        mail.send_package(self.settings, "test-password", recipient, "Faktureringsunderlag", "Hej!",
                          self.path, 41, "2026-10-16 – 2026-11-14")

    def records(self):
        with mail.archive() as db:
            return db.execute("SELECT * FROM mail").fetchall()

    def test_attachment_matches_archive_and_envelope(self):
        self.send()
        msg, = self.transport.return_value.send_message.call_args.args
        row, = self.records()
        self.assertEqual((row["project"], row["status"], row["recipient"]), (41, "sent", "accounts@example.org"))
        self.assertEqual(self.transport.return_value.send_message.call_args.kwargs,
                         {"from_addr": self.settings.email_address, "to_addrs": [row["recipient"]]})
        attachment, = msg.iter_attachments()
        self.assertEqual(attachment.get_filename(), self.path.name)
        self.assertEqual(attachment.get_payload(decode=True), row["pdf"])
        self.assertEqual(row["pdf"], self.path.read_bytes())

    def test_rejection_and_unknown_outcome_are_distinct_and_never_retried(self):
        smtp = self.transport.return_value
        smtp.send_message.side_effect = [smtplib.SMTPRecipientsRefused({}), smtplib.SMTPServerDisconnected()]
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                self.send()
        self.assertEqual([row["status"] for row in self.records()], ["failed", "unknown"])
        self.assertEqual(smtp.send_message.call_count, 2)

    def test_invalid_recipient_does_not_connect_or_archive(self):
        for text in ("", "invalid", "a@example.org, b@example.org", "a@example.org\r\nBcc: b@example.org"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.send(text)
        self.transport.assert_not_called()
        self.assertFalse(self.records())
