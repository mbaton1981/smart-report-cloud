import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

import requests
from flask import Flask, session
from reportlab.pdfgen import canvas
from werkzeug.serving import make_server

from mail_intake import IntakeError, IntakeStore, MAX_MESSAGE, bounded_recognize, mailbox_key, register_mail_intake
from mail_worker import IntakeClient, WorkerError, run_once

TOKEN = "isolated-mail-intake-token-not-for-production-0123456789"
MAILBOX = mailbox_key("imap.test.invalid", "test@example.invalid")


def pdf(text="Invoice 3539"):
    output = io.BytesIO()
    page = canvas.Canvas(output, invariant=1)
    for index, line in enumerate((text, "Supplier: Test Supplier AB", "Fakturanummer: 3539",
                                  "Fakturadatum: 2026-10-21", "Netto: 100,00",
                                  "Moms: 25,00", "Att betala (SEK): 125,00", "OCR: 003539")):
        page.drawString(40, 780 - 20 * index, line)
    page.save()
    return output.getvalue()


def eml(content=None, name="invoice.pdf", subject="Isolated invoice"):
    message = EmailMessage()
    message["From"], message["To"] = "test-supplier@example.invalid", "test-inbox@example.invalid"
    message["Subject"], message["Message-ID"] = subject, "<synthetic@example.invalid>"
    message.set_content("Isolated mail fixture. Not a real mailbox.")
    if content is not None:
        maintype, subtype = ("image", "png") if name.endswith(".png") else ("application", "pdf")
        message.add_attachment(content, maintype=maintype, subtype=subtype, filename=name)
    return message.as_bytes()


def recognized(paths):
    return {"state": "requires_review", "fields": {
        "document_type": "invoice", "supplier_name": "Test Supplier AB", "invoice_number": "3539",
        "document_date": "2026-10-21", "net_amount": "100.00", "vat_amount": "25.00",
        "gross_amount": "125.00", "amount_due": "125.00", "currency_code": "SEK",
        "payment_reference": "003539", "rounding_amount": "", "raw_text": "Invoice 3539",
    }, "errors": []}


class FakeIMAP:
    epoch = 10
    messages = {}
    commands = []
    failing_uid = None

    def __init__(self, host, port, **kwargs):
        assert host == "imap.test.invalid" and port == 993

    def login(self, username, password):
        assert username == "test@example.invalid" and password == "isolated-password"
        return "OK", []

    def select(self, folder, readonly):
        assert folder == "INBOX" and readonly is True
        return "OK", []

    def response(self, name):
        value = self.epoch if name == "UIDVALIDITY" else max(self.messages, default=0) + 1
        return name, [str(value).encode()]

    def uid(self, action, *args):
        self.commands.append((action, args))
        if action == "SEARCH":
            start, end = map(int, args[-1].split(":"))
            return "OK", [b" ".join(str(uid).encode() for uid in sorted(self.messages) if start <= uid <= end)]
        uid = int(args[0])
        if uid == self.failing_uid:
            raise OSError("synthetic IMAP outage")
        if args[-1] == "(RFC822.SIZE)":
            return "OK", [f"1 (UID {uid} RFC822.SIZE {len(self.messages[uid])})".encode()]
        assert args[-1] == "(BODY.PEEK[])"
        return "OK", [(b"BODY[]", self.messages[uid]), b")"]

    def logout(self):
        return "BYE", []


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
        self.app.config["TESTING"] = True
        self.app.secret_key = "isolated-test-session-secret"
        self.store = register_mail_intake(self.app, root=self.root, token=TOKEN, mailbox=MAILBOX,
                                          enabled=True, recognizer=recognized,
                                          review_authorized=lambda: session.get("role") == "admin")
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer " + TOKEN}
        FakeIMAP.epoch, FakeIMAP.messages, FakeIMAP.commands, FakeIMAP.failing_uid = 10, {}, [], None

    def lease(self, epoch=10):
        lease = self.store.acquire(MAILBOX)
        self.store.control({**lease, "action": "epoch", "epoch": epoch})
        return lease

    def ingest(self, lease, uid, raw, epoch=10):
        result = self.store.ingest({**lease, "epoch": epoch, "uid": uid,
                                   "sha256": hashlib.sha256(raw).hexdigest()}, raw)
        for item in result["attachments"]:
            if item["outcome"] == "draft":
                self.store.recognize({**lease, "draft_id": item["draft_id"], "sha256": item["sha256"]})
        return result

    def start_http(self):
        server = make_server("127.0.0.1", 0, self.app, threaded=True)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        def cleanup():
            server.shutdown()
            thread.join()
            server.server_close()

        self.addCleanup(cleanup)
        return IntakeClient(f"http://127.0.0.1:{server.server_port}", TOKEN, allow_local=True)

    def run_worker(self, api, **kwargs):
        return run_once(api, "imap.test.invalid", "test@example.invalid", "isolated-password",
                        client_factory=FakeIMAP, **kwargs)

    def test_disabled_auth_limits_and_no_expense_permissions(self):
        app = Flask("disabled")
        self.assertIsNone(register_mail_intake(app, enabled=False))
        self.assertEqual(app.test_client().post("/api/mail-intake/lease").status_code, 404)
        for path in ("/lease", "/control", "/messages"):
            response = self.client.post("/api/mail-intake" + path)
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get("/api/mail-intake/status", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post("/api/expense-documents", headers=self.headers, json={}).status_code, 404)
        self.assertEqual(self.client.post("/api/mail-intake/drafts/1/confirm", headers=self.headers).status_code, 404)
        response = self.client.post("/api/mail-intake/messages", headers=self.headers,
                                    data=b"x", environ_overrides={"CONTENT_LENGTH": str(MAX_MESSAGE + 1)})
        self.assertEqual(response.status_code, 413)
        with self.assertRaises(WorkerError):
            IntakeClient("http://remote.invalid", TOKEN)
        with self.assertRaises(RuntimeError):
            register_mail_intake(Flask("invalid"), root=self.root, token="short", enabled=True)

    def test_receipt_history_exact_blob_and_different_pdf_conflicts(self):
        lease = self.lease()
        content = pdf()
        first = self.ingest(lease, 1, eml(content))
        self.ingest(lease, 2, eml(content, subject="Forwarded"))
        self.ingest(lease, 3, eml(pdf("Invoice 3539 different scan")))
        self.ingest(lease, 4, eml(None))
        self.ingest(lease, 5, eml(b"not a PDF", name="fake.pdf"))
        self.assertEqual(self.store.status()["counts"], {"messages": 5, "drafts": 2, "attachments": 4})
        self.assertEqual(self.store.status()["conflicts"], 2)
        listing = self.client.get("/api/mail-intake/drafts", headers=self.headers).get_json()["drafts"]
        self.assertEqual(len(listing[0]["sources"]), 2)
        self.assertTrue(all(item["requires_manual_review"] for item in listing))
        document_id = first["attachments"][0]["draft_id"]
        response = self.client.get(f"/api/mail-intake/drafts/{document_id}/original", headers=self.headers)
        self.assertEqual(hashlib.sha256(response.data).digest(), hashlib.sha256(content).digest())
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        response.close()
        with self.store.connection() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("expense_documents", tables)
        self.assertNotIn("expenses", tables)

    def test_lost_ack_retry_and_restart_keep_one_draft(self):
        lease = self.lease()
        raw = eml(pdf())
        result = self.ingest(lease, 1, raw)
        reopened = IntakeStore(self.root, recognized)
        data = {**lease, "epoch": 10, "uid": 1, "sha256": hashlib.sha256(raw).hexdigest()}
        self.assertEqual(reopened.ingest(data, raw), result)
        self.assertEqual(reopened.status()["counts"]["drafts"], 1)
        data["sha256"] = "0" * 64
        with self.assertRaisesRegex(IntakeError, "hash_mismatch"):
            reopened.ingest(data, raw)
        with self.assertRaisesRegex(IntakeError, "receipt_content_changed"):
            self.ingest(lease, 1, eml(pdf("different")))

    def test_parallel_lease_expiry_fencing_and_epoch_reset(self):
        now = [1000.0]
        self.store.clock = lambda: now[0]
        old = self.lease()
        self.ingest(old, 1, eml(pdf()))
        with self.assertRaisesRegex(IntakeError, "mailbox_busy"):
            self.store.acquire(MAILBOX)
        now[0] += 181
        new = self.lease(epoch=11)
        self.assertGreater(new["fence"], old["fence"])
        with self.assertRaisesRegex(IntakeError, "lease_lost"):
            self.ingest(old, 2, eml(pdf()))
        with self.assertRaisesRegex(IntakeError, "stale_epoch"):
            self.ingest(new, 2, eml(pdf()), epoch=10)
        self.assertEqual(self.store.status()["cursor"], 0)
        self.ingest(new, 1, eml(pdf()), epoch=11)
        self.assertEqual(self.store.status()["counts"]["messages"], 2)
        self.assertEqual(self.store.status()["counts"]["drafts"], 1)

    def test_simultaneous_workers_only_one_acquires_lease(self):
        ready = threading.Barrier(2)

        def acquire():
            ready.wait()
            try:
                self.store.acquire(MAILBOX)
                return 200
            except IntakeError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: acquire(), range(2)))
        self.assertEqual(sorted(results), [200, 423])

    def test_expired_recognition_cannot_write_and_renew_extends_lease(self):
        now = [1000.0]
        self.store.clock = lambda: now[0]
        lease = self.lease()
        raw = eml(pdf())
        draft = self.store.ingest({**lease, "epoch": 10, "uid": 1,
                                   "sha256": hashlib.sha256(raw).hexdigest()}, raw)["attachments"][0]
        now[0] += 170
        self.store.control({**lease, "action": "renew"})
        now[0] += 20
        self.store.control({**lease, "action": "renew"})

        def expire(paths):
            now[0] += 181
            return recognized(paths)

        self.store.recognizer = expire
        with self.assertRaisesRegex(IntakeError, "lease_lost"):
            self.store.recognize({**lease, "draft_id": draft["draft_id"], "sha256": draft["sha256"]})
        self.assertEqual(self.store.status()["pending_recognition"], 1)

    def test_oversized_message_stops_visibly_without_skipping_then_recovers(self):
        api = self.start_http()
        FakeIMAP.messages = {1: eml(pdf())}
        actual = FakeIMAP.uid

        def oversized(instance, action, *args):
            if action == "FETCH" and args[-1] == "(RFC822.SIZE)":
                return "OK", [f"1 (RFC822.SIZE {MAX_MESSAGE + 1})".encode()]
            return actual(instance, action, *args)

        with patch.object(FakeIMAP, "uid", oversized):
            with self.assertRaisesRegex(WorkerError, "message_too_large"):
                self.run_worker(api)
        self.assertEqual(self.store.status()["cursor"], 0)
        self.assertEqual(self.store.status()["last_error"], "message_too_large_manual_review_required")
        self.assertEqual(self.run_worker(api)["processed"], 1)

    def test_http_worker_catchup_gaps_batch_restart_and_uidvalidity(self):
        api = self.start_http()
        FakeIMAP.messages = {1: eml(pdf()), 4: eml(None), 9: eml(pdf(), subject="Resend")}
        first = self.run_worker(api, max_messages=1, window=5)
        self.assertGreater(first["backlog_uid_span"], 0)
        second = self.run_worker(api, window=5)
        self.assertEqual(second["processed"], 2)
        self.assertEqual(second["backlog_uid_span"], 0)
        self.assertEqual(self.run_worker(api)["processed"], 0)
        FakeIMAP.messages[22] = eml(pdf("Invoice 3539 another PDF"))
        self.assertEqual(self.run_worker(api, window=5)["processed"], 1)
        FakeIMAP.epoch, FakeIMAP.messages = 11, {1: eml(pdf())}
        self.assertEqual(self.run_worker(api)["processed"], 1)
        status = api.call("GET", "/status")
        self.assertEqual(status["counts"]["messages"], 5)
        self.assertEqual(status["counts"]["drafts"], 2)
        self.assertEqual(status["conflicts"], 2)
        self.assertIsNotNone(status["last_success"])
        self.assertTrue(all(command[0] in ("SEARCH", "FETCH") for command in FakeIMAP.commands))

    def test_http_worker_failure_recovers_without_skip(self):
        api = self.start_http()
        FakeIMAP.messages = {1: eml(pdf()), 2: eml(pdf("Other copy"))}
        FakeIMAP.failing_uid = 2
        with self.assertRaises(OSError):
            self.run_worker(api)
        status = api.call("GET", "/status")
        self.assertEqual(status["cursor"], 1)
        self.assertEqual(status["last_error"], "worker_failed")
        FakeIMAP.failing_uid = None
        self.assertEqual(self.run_worker(api)["processed"], 1)
        self.assertIsNone(api.call("GET", "/status")["last_error"])

    def test_http_lost_ack_after_commit_resume(self):
        api = self.start_http()
        FakeIMAP.messages = {1: eml(pdf())}
        actual_call = api.call
        lost = [True]

        def call(method, path, **kwargs):
            result = actual_call(method, path, **kwargs)
            if path == "/messages" and lost[0]:
                lost[0] = False
                raise requests.ConnectionError("synthetic lost response")
            return result

        with patch.object(api, "call", side_effect=call):
            with self.assertRaises(requests.ConnectionError):
                self.run_worker(api)
        self.assertEqual(self.run_worker(api)["processed"], 0)
        self.assertEqual(self.store.status()["counts"]["drafts"], 1)

    def test_real_pdf_recognizer_to_reopened_draft_and_failed_document(self):
        self.store.recognizer = bounded_recognize
        api = self.start_http()
        FakeIMAP.messages = {1: eml(pdf()), 2: eml(b"%PDF-1.4\nnot a readable document")}
        self.run_worker(api)
        reopened = IntakeStore(self.root)
        self.assertEqual(reopened.status()["counts"]["drafts"], 2)
        listing = self.client.get("/api/mail-intake/drafts", headers=self.headers).get_json()["drafts"]
        fields = listing[0]["result"]["fields"]
        self.assertIn("Invoice 3539", fields["raw_text"])
        self.assertEqual(fields["supplier_name"], "Test Supplier AB")
        self.assertEqual(fields["invoice_number"], "3539")
        self.assertEqual(fields["document_date"], "2026-10-21")
        self.assertEqual(fields["amount_due"], "125.00")
        self.assertEqual(fields["payment_reference"], "003539")
        self.assertIn(fields["rounding_amount"], (None, ""))
        self.assertEqual(listing[1]["recognition"]["outcome"], "failed")
        page = self.client.get("/api/mail-intake/review", headers=self.headers)
        self.assertEqual(page.status_code, 200)
        self.assertIn("003539", page.get_data(as_text=True))
        self.assertIn("125.00", page.get_data(as_text=True))

    def test_pending_recognition_resumes_after_committed_receipt(self):
        lease = self.lease()
        raw = eml(pdf())
        self.store.ingest({**lease, "epoch": 10, "uid": 1,
                           "sha256": hashlib.sha256(raw).hexdigest()}, raw)
        self.store.control({**lease, "action": "fail"})
        self.assertEqual(self.store.status()["pending_recognition"], 1)
        api = self.start_http()
        FakeIMAP.messages = {1: raw}
        result = self.run_worker(api)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["recognized"], 1)
        self.assertEqual(self.store.status()["pending_recognition"], 0)

    def test_admin_review_is_read_only_and_escapes_mail(self):
        lease = self.lease()
        self.ingest(lease, 1, eml(pdf(), subject="<script>alert(1)</script>"))
        self.assertEqual(self.client.get("/api/mail-intake/review").status_code, 401)
        with self.client.session_transaction() as browser:
            browser["role"] = "admin"
        page = self.client.get("/api/mail-intake/review")
        self.assertEqual(page.status_code, 200)
        self.assertIn("&lt;script&gt;", page.get_data(as_text=True))
        self.assertNotIn("<script>", page.get_data(as_text=True))
        response = self.client.get("/api/mail-intake/drafts/1/original?view=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("inline", response.headers["Content-Disposition"])
        response.close()
        self.assertEqual(self.client.post("/api/mail-intake/lease", json={"mailbox": MAILBOX}).status_code, 401)
        self.assertEqual(self.client.get("/api/mail-intake/status").status_code, 401)

    def test_mailbox_binding_malformed_control_and_corrupt_original(self):
        response = self.client.post("/api/mail-intake/lease", headers=self.headers, json={"mailbox": "wrong"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post("/api/mail-intake/control", headers=self.headers, json=[]).status_code, 400)
        with self.assertRaises(RuntimeError):
            IntakeStore(self.root, mailbox="f" * 64)
        self.ingest(self.lease(), 1, eml(pdf()))
        original = next((self.root / "originals").glob("*.pdf"))
        original.write_bytes(b"deliberately corrupted isolated fixture")
        self.assertEqual(self.client.get("/api/mail-intake/drafts/1/original", headers=self.headers).status_code, 500)

    def test_recognition_timeout_limits_and_no_automatic_overwrite(self):
        from mail_intake import recognize_file
        with patch("mail_intake.subprocess.run", side_effect=subprocess.TimeoutExpired("isolated", 90)):
            self.assertEqual(bounded_recognize([self.root / "not-used.pdf"])["state"], "failed")
        content = io.BytesIO()
        page = canvas.Canvas(content)
        for number in range(21):
            page.drawString(10, 10, str(number))
            page.showPage()
        page.save()
        path = self.root / "too-many-pages.pdf"
        path.write_bytes(content.getvalue())
        self.assertEqual(recognize_file(path)["errors"], ["page_limit_manual_review_required"])
        lease = self.lease()
        result = self.ingest(lease, 1, eml(pdf()))
        draft = result["attachments"][0]
        with patch.object(self.store, "recognizer", side_effect=AssertionError("must not overwrite")):
            self.store.recognize({**lease, "draft_id": draft["draft_id"], "sha256": draft["sha256"]})

    def test_monitor_reports_stale_and_failure_separately_from_recognition(self):
        api = self.start_http()
        environment = {**os.environ, "SMARTREPORT_MAIL_API_URL": api.url.removesuffix("/api/mail-intake"),
                       "SMARTREPORT_MAIL_INTAKE_TOKEN": TOKEN}

        def monitor(*args):
            result = subprocess.run(
                [sys.executable, "-m", "mail_worker", "--monitor", "--allow-local", *args],
                cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True, timeout=30)
            return result.returncode, json.loads(result.stdout)

        self.assertEqual(monitor()[0], 1)
        FakeIMAP.messages = {1: eml(pdf())}
        self.run_worker(api)
        code, result = monitor()
        self.assertEqual(code, 0)
        self.assertTrue(result["healthy"])
        self.assertEqual(result["recognition_attention"], 0)
        self.assertEqual(monitor("--max-age", "0")[0], 1)
        FakeIMAP.messages[2] = eml(pdf("Another PDF of invoice 3539"))
        self.run_worker(api)
        code, result = monitor()
        self.assertEqual(code, 1)
        self.assertTrue(result["operational_healthy"])
        self.assertTrue(result["review_attention"])
        self.assertFalse(result["healthy"])

    def test_noninvoice_classification_remains_separate(self):
        def order(paths):
            result = recognized(paths)
            result["fields"]["raw_text"] = "Orderbekräftelse\nOrdernummer SE-999"
            return result
        self.store.recognizer = order
        self.ingest(self.lease(), 1, eml(pdf("Orderbekraftelse")))
        item = self.client.get("/api/mail-intake/drafts", headers=self.headers).get_json()["drafts"][0]
        self.assertEqual(item["recognition"]["outcome"], "not_invoice")
        self.assertEqual(item["result"]["fields"]["document_type"], "order_confirmation")
        self.assertIsNone(item["result"]["fields"]["invoice_number"])

    def test_scanned_image_uses_local_ocr_through_worker(self):
        import pypdfium2 as pdfium
        source = pdfium.PdfDocument(pdf())
        try:
            page = source[0]
            try:
                bitmap = page.render(scale=2)
                try:
                    image = bitmap.to_pil()
                    output = io.BytesIO()
                    image.save(output, format="PNG")
                    image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            source.close()
        self.store.recognizer = bounded_recognize
        api = self.start_http()
        FakeIMAP.messages = {1: eml(output.getvalue(), name="scan.png")}
        result = self.run_worker(api)
        self.assertEqual(result["recognized"], 1)
        item = api.call("GET", "/drafts")["drafts"][0]
        self.assertEqual(item["result"]["state"], "requires_review", item["result"])
        self.assertIn("3539", item["result"]["fields"]["raw_text"])
        self.assertTrue(any("OCR" in method for method in item["result"]["methods"]))
        self.assertEqual(item["result"]["fields"]["amount_due"], "125.00")

    def test_actual_server_registration_default_off_and_draft_scope(self):
        script = """
import os, sqlite3, hashlib
import server
c = server.app.test_client()
headers = {'Authorization': 'Bearer ' + os.environ['SMARTREPORT_MAIL_INTAKE_TOKEN']}
r = c.get('/api/mail-intake/status', headers=headers)
assert r.status_code == int(os.environ['EXPECTED_STATUS']), r.status_code
if r.status_code == 200:
    assert c.post('/api/expense-documents', headers=headers, json={}).status_code in (401,403)
    lease = c.post('/api/mail-intake/lease', headers=headers,
                   json={'mailbox': os.environ['SMARTREPORT_MAILBOX_ID']}).get_json()
    assert c.post('/api/mail-intake/control', headers=headers,
                  json={**lease, 'action':'epoch','epoch':10}).status_code == 200
    raw = open(os.environ['TEST_EML'],'rb').read()
    response = c.post('/api/mail-intake/messages', query_string={**lease,'epoch':10,'uid':1},
                      headers={**headers,'X-Content-SHA256':hashlib.sha256(raw).hexdigest(),
                               'Content-Type':'message/rfc822'},data=raw)
    assert response.status_code == 200, response.get_json()
    draft = response.get_json()['attachments'][0]
    assert c.post('/api/mail-intake/recognize',headers=headers,
                  json={**lease,'draft_id':draft['draft_id'],'sha256':draft['sha256']}).status_code == 200
    assert c.get('/api/mail-intake/review',headers=headers).status_code == 200
db = sqlite3.connect(os.environ['DATABASE_PATH'])
assert db.execute('SELECT COUNT(*) FROM expense_documents').fetchone()[0] == 0
db.close()
"""
        fixture = self.root / "test.eml"
        fixture.write_bytes(eml(pdf()))
        for enabled, expected in (("0", "404"), ("1", "200")):
            env = {**os.environ, "DATABASE_PATH": str(self.root / f"server-{enabled}.db"),
                   "SMARTREPORT_DOCUMENTS_DIR": str(self.root / f"docs-{enabled}"),
                   "SECRET_KEY": "isolated-secret", "SYNC_API_KEY": "isolated-expense-secret",
                   "ADMIN_PASSWORD": "isolated-admin", "SMARTREPORT_MAIL_INTAKE_TOKEN": TOKEN,
                   "SMARTREPORT_MAIL_INTAKE_DIR": str(self.root / f"server-intake-{enabled}"),
                   "SMARTREPORT_MAILBOX_ID": MAILBOX,
                   "TEST_EML": str(fixture),
                   "SMARTREPORT_MAIL_INTAKE_ENABLED": enabled, "EXPECTED_STATUS": expected}
            result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                    env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
