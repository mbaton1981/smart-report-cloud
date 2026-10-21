import hashlib
import imaplib
import io
import json
import mimetypes
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock, patch

from mail_import import ADDRESS, HOST, MAX_MESSAGE_BYTES, MailImportError, MailQueue, import_inbox, main


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
FIELDS = {
    "supplier_name": "Example Supplier AB", "invoice_number": "INV-42",
    "document_date": "2026-10-18", "gross_amount": "125.00",
    "currency_code": "SEK", "raw_text": "Invoice",
}


def recognized(paths):
    return {"fields": dict(FIELDS), "state": "requires_review", "errors": [], "methods": ["test"]}


def message(*attachments, message_id="<example@local.test>"):
    mail = EmailMessage()
    mail["Message-ID"] = message_id
    mail["From"] = "supplier@local.test"
    mail["To"] = ADDRESS
    mail["Subject"] = "Invoice attachments"
    mail.set_content("Please review.")
    for filename, payload in attachments:
        kind = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        maintype, subtype = kind.split("/", 1)
        mail.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    return mail.as_bytes()


class FakeIMAP:
    def __init__(self, messages, dates=None):
        self.messages = messages
        self.dates = dates or {}
        self.calls = []
        self.validity = b"321"
        self.fail_body = set()

    def login(self, address, password):
        self.calls.append(("login", address))
        return "OK", [b"authenticated"]

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, name):
        return name, [self.validity]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [" ".join(self.messages).encode()]
        uid, query = args
        if query == "(RFC822.SIZE INTERNALDATE)":
            date = self.dates.get(uid, NOW - timedelta(hours=1))
            stamp = date.strftime("%d-%b-%Y %H:%M:%S %z")
            return "OK", [f'1 (UID {uid} RFC822.SIZE {len(self.messages[uid])} INTERNALDATE "{stamp}")'.encode()]
        if query == "(BODY.PEEK[])":
            if uid in self.fail_body:
                return "NO", [b"private server error text"]
            return "OK", [(b"BODY[]", self.messages[uid]), b")"]
        raise AssertionError(f"Unsafe or unexpected IMAP request: {command} {args}")

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", []


class MailImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.queue = MailQueue(Path(self.temp.name) / "isolated")

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def run_import(self, client, recognizer=recognized):
        factory = Mock(return_value=client)
        self.summary = import_inbox(self.queue, "not-a-real-password", now=NOW,
                                    client_factory=factory, recognizer=recognizer)
        args, kwargs = factory.call_args
        self.assertEqual(args, (HOST, 993))
        self.assertTrue(kwargs["ssl_context"].check_hostname)
        self.assertEqual(kwargs["ssl_context"].verify_mode, 2)
        return self.queue.review()

    def test_read_only_exact_window_and_account(self):
        data = message(("invoice.pdf", b"same original"))
        client = FakeIMAP(
            {"1": data, "2": data, "3": data, "4": data},
            {"1": NOW - timedelta(days=7, seconds=1),
             "2": NOW - timedelta(days=7),
             "3": NOW, "4": NOW + timedelta(seconds=1)},
        )
        result = self.run_import(client)
        self.assertIn(("login", ADDRESS), client.calls)
        self.assertIn(("select", "INBOX", True), client.calls)
        self.assertIn(("SEARCH", None, "SINCE", "12-Oct-2026"), client.calls)
        bodies = [call[1] for call in client.calls if call[0] == "FETCH" and call[2] == "(BODY.PEEK[])"]
        self.assertEqual(bodies, ["2", "3"])
        self.assertEqual(len(result["documents"]), 1)
        self.assertEqual(len(result["documents"][0]["sources"]), 2)
        self.assertFalse(result["issues"])
        self.assertEqual(client.calls[-1], ("logout",))
        self.assertEqual(self.summary.messages_checked, 4)
        self.assertEqual(self.summary.messages_in_period, 2)
        self.assertEqual(self.summary.attachments_found, 2)
        self.assertEqual(self.summary.documents_added, 1)
        self.assertEqual(self.summary.duplicates_skipped, 1)
        self.assertEqual(self.summary.errors, 0)

    def test_repeated_fetch_forward_and_reopen_keep_one_original(self):
        data = message(("invoice.pdf", b"same original"))
        forward = message(("renamed.PDF", b"same original"), message_id="<forward@local.test>")
        client = FakeIMAP({"1": data, "2": forward})
        recognizer = Mock(side_effect=recognized)
        self.run_import(client, recognizer)
        self.assertEqual(self.summary.documents_added, 1)
        self.assertEqual(self.summary.duplicates_skipped, 1)
        self.run_import(client, recognizer)
        self.assertEqual(self.summary.documents_added, 0)
        self.assertEqual(self.summary.duplicates_skipped, 2)
        self.assertEqual(self.summary.errors, 0)
        runs = [json.loads(path.read_text(encoding="utf-8"))
                for path in (self.queue.root / "runs").glob("*.json")]
        self.assertEqual(len(runs), 2)
        self.assertEqual(sorted(run["documents_added"] for run in runs), [0, 1])
        self.assertTrue(all(run["completed"] for run in runs))
        self.assertTrue(all(run["messages_checked"] == 2 for run in runs))
        self.assertTrue(all(run["attachments_found"] == 2 for run in runs))
        self.assertTrue(all(run["errors"] == 0 for run in runs))
        self.assertNotIn("not-a-real-password", json.dumps(runs))
        self.queue.close()
        self.queue = MailQueue(Path(self.temp.name) / "isolated")
        result = self.queue.review()
        self.assertEqual(len(result["documents"]), 1)
        document = result["documents"][0]
        self.assertEqual(len(document["sources"]), 2)
        self.assertEqual({s["message_id"] for s in document["sources"]},
                         {"<example@local.test>", "<forward@local.test>"})
        self.assertEqual({s["filename"] for s in document["sources"]}, {"invoice.pdf", "renamed.PDF"})
        self.assertEqual(recognizer.call_count, 1)
        original = self.queue.root / document["original"]
        self.assertEqual(original.read_bytes(), b"same original")
        self.assertEqual(document["sha256"], hashlib.sha256(original.read_bytes()).hexdigest())
        self.assertEqual(len(list(original.parent.iterdir())), 1)

    def test_different_files_same_fields_warn_both_without_merging(self):
        result = self.run_import(FakeIMAP({"1": message(
            ("invoice.pdf", b"version one"), ("copy.pdf", b"version two"))}))
        self.assertEqual(len(result["documents"]), 2)
        first, second = result["documents"]
        self.assertEqual(first["possible_duplicates"], [second["id"]])
        self.assertEqual(second["possible_duplicates"], [first["id"]])
        self.assertEqual(first["state"], "requires_review")
        self.assertEqual(second["state"], "requires_review")

    def test_attachment_failure_preserves_original_and_continues_then_retry(self):
        def fail_one(paths):
            if Path(paths[0]).read_bytes() == b"bad":
                raise ValueError("sensitive text must not reach error log")
            return recognized(paths)

        result = self.run_import(FakeIMAP({"1": message(
            ("bad.pdf", b"bad"), ("ok.pdf", b"good"))}), fail_one)
        self.assertEqual([d["state"] for d in result["documents"]], ["failed", "requires_review"])
        self.assertEqual(len(list((self.queue.root / "originals").iterdir())), 2)
        self.assertNotIn("sensitive", json.dumps(result))
        self.assertEqual(self.summary.documents_added, 2)
        self.assertEqual(self.summary.errors, 1)
        self.queue.retry_failed(recognized)
        self.assertTrue(all(d["state"] == "requires_review" for d in self.queue.review()["documents"]))

    def test_message_failure_is_visible_and_other_messages_continue(self):
        client = FakeIMAP({"1": message(("one.pdf", b"one")), "2": message(("two.pdf", b"two"))})
        client.fail_body.add("1")
        result = self.run_import(client)
        self.assertEqual(len(result["documents"]), 1)
        self.assertEqual(result["issues"][0]["uid"], "1")
        self.assertIn("read-only message fetch", result["issues"][0]["code"])
        self.assertNotIn("private server error", json.dumps(result))
        self.assertEqual(self.summary.messages_checked, 2)
        self.assertEqual(self.summary.documents_added, 1)
        self.assertEqual(self.summary.errors, 1)
        client.fail_body.clear()
        result = self.run_import(client)
        self.assertEqual(len(result["documents"]), 2)
        self.assertFalse(result["issues"])

    def test_empty_attachment_does_not_stop_other_parts(self):
        result = self.run_import(FakeIMAP({"1": message(("empty.pdf", b""), ("good.pdf", b"good"))}))
        self.assertEqual(len(result["documents"]), 1)
        self.assertEqual(len(result["issues"]), 1)
        self.assertGreater(result["issues"][0]["part"], 0)
        self.assertEqual(result["issues"][0]["code"], "Empty attachment.")
        self.assertEqual(self.summary.attachments_found, 2)
        self.assertEqual(self.summary.documents_added, 1)
        self.assertEqual(self.summary.errors, 1)

    def test_changed_original_is_reported_and_never_overwritten(self):
        client = FakeIMAP({"1": message(("one.pdf", b"one"))})
        result = self.run_import(client)
        original = self.queue.root / result["documents"][0]["original"]
        original.write_bytes(b"unexpected change")
        result = self.run_import(client)
        self.assertEqual(original.read_bytes(), b"unexpected change")
        self.assertIn("checksum mismatch", result["issues"][0]["code"])
        self.assertEqual(len(result["documents"]), 1)

    def test_missing_uidvalidity_fails_before_download(self):
        client = FakeIMAP({"1": message(("one.pdf", b"one"))})
        client.validity = None
        with self.assertRaisesRegex(MailImportError, "UIDVALIDITY"):
            self.run_import(client)
        self.assertFalse(any(call[0] == "FETCH" for call in client.calls))

    def test_oversized_message_is_not_downloaded(self):
        client = FakeIMAP({"1": b"x" * (MAX_MESSAGE_BYTES + 1)})
        result = self.run_import(client)
        self.assertFalse(result["documents"])
        self.assertIn("30 MiB", result["issues"][0]["code"])
        self.assertFalse(any(call[0] == "FETCH" and call[2] == "(BODY.PEEK[])" for call in client.calls))

    def test_uidvalidity_change_retains_sources_without_duplicate_document(self):
        client = FakeIMAP({"1": message(("one.pdf", b"one"))})
        self.run_import(client)
        client.validity = b"322"
        result = self.run_import(client)
        self.assertEqual(len(result["documents"]), 1)
        self.assertEqual({s["uidvalidity"] for s in result["documents"][0]["sources"]}, {"321", "322"})

    def test_failed_login_is_fatal_without_downloading_or_exposing_response(self):
        client = FakeIMAP({"1": message(("one.pdf", b"one"))})
        client.login = Mock(return_value=("NO", [b"private authentication response"]))
        with self.assertRaisesRegex(MailImportError, "IMAP rejected login"):
            self.run_import(client)
        self.assertEqual(client.calls, [("logout",)])
        self.assertFalse(self.queue.review()["documents"])
        runs = list((self.queue.root / "runs").glob("*.json"))
        self.assertEqual(len(runs), 1)
        report = json.loads(runs[0].read_text(encoding="utf-8"))
        self.assertFalse(report["completed"])
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["messages_checked"], 0)
        self.assertIn("login", report["stop_reason"])
        self.assertNotIn("private authentication", json.dumps(report))

    def test_summary_counts_unsupported_attachments_without_document_creation(self):
        client = FakeIMAP({"1": message(("invoice.pdf", b"invoice"), ("notes.txt", b"notes"))})
        result = self.run_import(client)
        self.assertEqual(self.summary.attachments_found, 2)
        self.assertEqual(self.summary.supported_attachments, 1)
        self.assertEqual(self.summary.documents_added, 1)
        self.assertEqual(self.summary.errors, 0)
        self.assertEqual(len(result["documents"]), 1)
    def test_imap_auth_exception_is_sanitized_and_not_retried(self):
        client = FakeIMAP({})
        client.login = Mock(side_effect=imaplib.IMAP4.error(b"private authentication response"))
        with self.assertRaisesRegex(MailImportError, "Loopia login rejected") as caught:
            self.run_import(client)
        self.assertNotIn("private", str(caught.exception))
        client.login.assert_called_once()
        self.assertEqual(client.calls, [("logout",)])

    def test_cli_refuses_noninteractive_password_before_network_access(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["mail_import.py", "fetch", "--data-dir", str(self.queue.root)]), \
                patch("mail_import.sys.stdin.isatty", return_value=False), \
                patch("mail_import.getpass.getpass") as prompt, \
                patch("mail_import.import_inbox") as fetch, \
                redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(main(), 1)
        prompt.assert_not_called()
        fetch.assert_not_called()
        self.assertIn("local interactive terminal", errors.getvalue())

    def test_refuse_existing_foreign_directory_and_database(self):
        foreign = Path(self.temp.name) / "foreign"
        foreign.mkdir()
        (foreign / "smart_report.db").write_bytes(b"do not touch")
        with self.assertRaises(MailImportError):
            MailQueue(foreign)
        self.assertEqual((foreign / "smart_report.db").read_bytes(), b"do not touch")
        with closing(sqlite3.connect(foreign / "mail_queue.sqlite3")) as connection:
            with connection:
                connection.execute("CREATE TABLE working_data (value TEXT)")
                connection.execute("INSERT INTO working_data VALUES ('retained')")
        with self.assertRaises(MailImportError):
            MailQueue(foreign)
        with closing(sqlite3.connect(foreign / "mail_queue.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT value FROM working_data").fetchone()[0], "retained")

    def test_proforma_only_changes_staging_classification(self):
        def proforma(paths):
            result = recognized(paths)
            result["fields"]["raw_text"] = "PROFORMAFAKTURA\nInvoice number 42"
            return result

        result = self.run_import(FakeIMAP({"1": message(("proforma.pdf", b"proforma"))}), proforma)
        document = result["documents"][0]
        self.assertEqual(document["result"]["fields"]["document_type"], "proforma")
        self.assertEqual(document["state"], "requires_review")
        tables = {row[0] for row in self.queue.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"documents", "sources", "issues"})


    def test_real_pdf_scan_receipt_credit_proforma_and_duplicate(self):
        from PIL import Image, ImageDraw, ImageFont
        from reportlab.pdfgen.canvas import Canvas

        source = Path(self.temp.name) / "fixtures"
        source.mkdir()
        base = [
            "Supplier: Example Supplier AB", "Invoice number: INV-42",
            "Invoice date: 2026-10-18", "Netto: 100.00",
            "Total VAT: 25.00", "Total amount: 125.00",
            "Amount due: 125.00 SEK",
        ]

        def pdf(name, title):
            path = source / name
            canvas = Canvas(str(path))
            text = canvas.beginText(60, 780)
            text.setFont("Helvetica", 14)
            lines = [title, *base]
            if title == "KREDITFAKTURA":
                lines = [line.replace("100.00", "-100.00").replace("125.00", "-125.00")
                         .replace("Total VAT: 25.00", "Total VAT: -25.00") for line in lines]
            for line in lines:
                text.textLine(line)
            canvas.drawText(text)
            canvas.save()
            return path.read_bytes()

        invoice = pdf("invoice.pdf", "INVOICE")
        receipt = pdf("receipt.pdf", "KVITTO")
        credit = pdf("credit.pdf", "KREDITFAKTURA")
        proforma = pdf("proforma.pdf", "PROFORMA INVOICE")
        image = Image.new("RGB", (1600, 950), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 40)
        for index, line in enumerate(["INVOICE", *base]):
            draw.text((70, 60 + index * 90), line, fill="black", font=font)
        scan = source / "scan.png"
        image.save(scan)
        client = FakeIMAP({
            "1": message(("invoice.pdf", invoice), ("scan.png", scan.read_bytes()),
                         ("receipt.pdf", receipt), ("credit.pdf", credit), ("proforma.pdf", proforma)),
            "2": message(("forward.pdf", invoice), message_id="<forward@local.test>"),
        })
        from invoice_processing import recognize_invoice
        report = self.run_import(client, recognize_invoice)
        self.assertFalse(report["issues"])
        self.assertEqual(len(report["documents"]), 5)
        documents = report["documents"]
        self.assertTrue(all(d["state"] == "requires_review" for d in documents), report)
        types = [d["result"]["fields"]["document_type"] for d in documents]
        self.assertEqual(types, ["invoice", "invoice", "receipt", "credit_note", "proforma"])
        self.assertEqual(documents[0]["result"]["methods"], ["текст PDF"])
        self.assertEqual(documents[3]["result"]["fields"]["amount_due"], "-125.00")
        self.assertEqual(documents[1]["result"]["methods"], ["локальный OCR"])
        self.assertEqual(documents[1]["result"]["fields"]["invoice_number"], "INV-42")
        scan_fields = documents[1]["result"]["fields"]
        self.assertEqual(scan_fields["net_amount"], "100.00")
        self.assertEqual(scan_fields["amount_due"], "125.00")
        self.assertIsNone(scan_fields["vat_amount"])
        self.assertIsNone(scan_fields["gross_amount"])
        self.assertTrue(any("vat_amount" in reason for reason in scan_fields["review_reasons"]))
        self.assertEqual(len(documents[0]["sources"]), 2)
        self.assertEqual(sum(len(d["sources"]) for d in documents), 6)
        self.assertEqual(self.queue.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
