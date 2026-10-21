"""Durable draft-only mail intake. No dependency on the expense database."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file

from invoice_processing import recognize_invoice
from mail_import import classify_mail_document, recognition_status

MAX_MESSAGE = 20 * 1024 * 1024
LEASE_SECONDS = 180
MAX_PAGES = 20
MAX_PIXELS = 20_000_000
APPLICATION_ID = 0x534D494E
SCHEMA = """
CREATE TABLE IF NOT EXISTS configuration (id INTEGER PRIMARY KEY CHECK(id=1), mailbox TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mailbox (
 id INTEGER PRIMARY KEY CHECK(id=1), epoch TEXT, cursor INTEGER NOT NULL DEFAULT 0,
 fence INTEGER NOT NULL DEFAULT 0, owner TEXT, expires REAL NOT NULL DEFAULT 0,
 last_attempt REAL, last_success REAL, last_error TEXT, backlog INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO mailbox(id) VALUES(1);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY,
 epoch TEXT NOT NULL, uid INTEGER NOT NULL, sha256 TEXT NOT NULL,
 metadata TEXT NOT NULL, report TEXT NOT NULL, UNIQUE(epoch,uid));
CREATE TABLE IF NOT EXISTS drafts (
 id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, original TEXT NOT NULL,
 result TEXT NOT NULL, recognition TEXT NOT NULL, business_key TEXT,
 conflict INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS attachments (
 epoch TEXT NOT NULL, uid INTEGER NOT NULL, part INTEGER NOT NULL,
 filename TEXT NOT NULL, draft_id INTEGER, outcome TEXT NOT NULL,
 PRIMARY KEY(epoch,uid,part));
CREATE INDEX IF NOT EXISTS draft_business ON drafts(business_key);
"""


class IntakeError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def positive(value, name: str, allow_zero=False) -> int:
    if type(value) is not int or value < (0 if allow_zero else 1) or value > 4294967295:
        raise IntakeError(f"invalid_{name}", 400)
    return value


def identity(fields: dict) -> str | None:
    # A candidate match, never an authority to merge invoices.
    supplier = fields.get("supplier_org_number") or fields.get("supplier_name")
    number = fields.get("invoice_number")
    if not supplier or not number:
        return None
    return json.dumps([re.sub(r"\W", "", str(supplier).casefold()),
                       str(number).strip().casefold()], ensure_ascii=False)


def mailbox_key(host: str, username: str, folder="INBOX") -> str:
    return hashlib.sha256(json.dumps([host.strip().casefold(), username.strip().casefold(), folder]).encode()).hexdigest()


def failed_recognition(code):
    return {"fields": {}, "state": "failed", "errors": [code], "missing_fields": []}


def bounded_recognize(paths):
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--recognize", str(paths[0])],
            capture_output=True, timeout=90, check=False, encoding="utf-8")
    except subprocess.TimeoutExpired:
        return failed_recognition("recognition_timeout_manual_review_required")
    if result.returncode:
        return failed_recognition("recognition_process_failed_manual_review_required")
    return json.loads(result.stdout)


def recognize_file(path):
    if path.suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            return failed_recognition("encrypted_pdf")
        if len(reader.pages) > MAX_PAGES:
            return failed_recognition("page_limit_manual_review_required")
        if any(float(page.mediabox.width) * float(page.mediabox.height) * 4 > MAX_PIXELS
               for page in reader.pages):
            return failed_recognition("page_size_limit_manual_review_required")
    else:
        from PIL import Image
        with Image.open(path) as image:
            if image.width * image.height > MAX_PIXELS:
                return failed_recognition("image_size_limit_manual_review_required")
    return recognize_invoice([path])


class IntakeStore:
    def __init__(self, root: Path, recognizer=bounded_recognize, clock=time.time, mailbox=None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "originals").mkdir(exist_ok=True)
        self.recognizer, self.clock = recognizer, clock
        with self.connection() as db:
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            has_tables = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            if (has_tables and app_id != APPLICATION_ID) or app_id not in (0, APPLICATION_ID):
                raise RuntimeError("Refusing to initialize an unrelated mail database")
            if has_tables and db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise RuntimeError("Unsupported mail intake schema version")
            db.executescript(SCHEMA)
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            db.execute("PRAGMA user_version=1")
            configured = db.execute("SELECT mailbox FROM configuration WHERE id=1").fetchone()
            if configured and mailbox and configured["mailbox"] != mailbox:
                raise RuntimeError("Mail intake directory belongs to a different mailbox")
            if mailbox:
                db.execute("INSERT OR IGNORE INTO configuration VALUES(1,?)", (mailbox,))
            self.mailbox = configured["mailbox"] if configured else mailbox

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.root / "intake.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def guard(self, db, owner, fence):
        row = db.execute("SELECT * FROM mailbox WHERE id=1").fetchone()
        if row["owner"] != owner or row["fence"] != fence or row["expires"] <= self.clock():
            raise IntakeError("lease_lost")
        return row

    def acquire(self, mailbox=None):
        if self.mailbox and mailbox != self.mailbox:
            raise IntakeError("mailbox_mismatch", 403)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM mailbox WHERE id=1").fetchone()
            if row["expires"] > self.clock():
                raise IntakeError("mailbox_busy", 423)
            owner, fence = uuid.uuid4().hex, row["fence"] + 1
            db.execute("""UPDATE mailbox SET owner=?, fence=?, expires=?, last_attempt=?,
                          last_error=NULL WHERE id=1""",
                       (owner, fence, self.clock() + LEASE_SECONDS, self.clock()))
            return {"owner": owner, "fence": fence}

    def control(self, data):
        if not isinstance(data, dict):
            raise IntakeError("invalid_control", 400)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.guard(db, data.get("owner"), data.get("fence"))
            action = data.get("action")
            if action == "epoch":
                epoch = str(positive(data.get("epoch"), "epoch"))
                if epoch != row["epoch"]:
                    db.execute("UPDATE mailbox SET epoch=?,cursor=0 WHERE id=1", (epoch,))
            elif action == "advance":
                cursor = positive(data.get("cursor"), "cursor", True)
                if str(data.get("epoch")) != row["epoch"] or cursor < row["cursor"]:
                    raise IntakeError("stale_checkpoint")
                db.execute("UPDATE mailbox SET cursor=? WHERE id=1", (cursor,))
            elif action == "finish":
                backlog = positive(data.get("backlog"), "backlog", True)
                db.execute("""UPDATE mailbox SET last_success=?,backlog=?,expires=0,owner=NULL
                              WHERE id=1""", (self.clock(), backlog))
            elif action == "fail":
                # Do not accept raw IMAP/HTTP exception text containing credentials.
                code = data.get("code", "worker_failed")
                if code not in ("worker_failed", "message_too_large_manual_review_required",
                                "lease_renewal_failed"):
                    code = "worker_failed"
                db.execute("""UPDATE mailbox SET last_error=?,expires=0,owner=NULL
                              WHERE id=1""", (code,))
            elif action != "renew":
                raise IntakeError("invalid_action", 400)
            if action not in ("finish", "fail"):
                db.execute("UPDATE mailbox SET expires=? WHERE id=1",
                           (self.clock() + LEASE_SECONDS,))
            result = dict(db.execute("SELECT * FROM mailbox WHERE id=1").fetchone())
            return {"epoch": result["epoch"], "cursor": result["cursor"]}

    def save_blob(self, payload, suffix):
        digest = hashlib.sha256(payload).hexdigest()
        path = self.root / "originals" / (digest + suffix)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise IntakeError("original_corrupt", 500)
        else:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
                    temporary = Path(output.name)
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(path)
                if os.name != "nt":
                    directory = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        return digest, path

    def ingest(self, data, raw):
        if not raw or len(raw) > MAX_MESSAGE:
            raise IntakeError("invalid_message_size", 413)
        epoch = str(positive(data.get("epoch"), "epoch"))
        uid = positive(data.get("uid"), "uid")
        digest = hashlib.sha256(raw).hexdigest()
        if data.get("sha256") != digest:
            raise IntakeError("hash_mismatch", 400)
        with self.connection() as db:
            row = self.guard(db, data.get("owner"), data.get("fence"))
            if row["epoch"] != epoch:
                raise IntakeError("stale_epoch")
            previous = db.execute("SELECT * FROM messages WHERE epoch=? AND uid=?", (epoch, uid)).fetchone()
            if previous:
                if previous["sha256"] != digest:
                    raise IntakeError("receipt_content_changed")
                return json.loads(previous["report"])
            if uid <= row["cursor"]:
                raise IntakeError("behind_checkpoint")
        message = BytesParser(policy=policy.default).parsebytes(raw)
        if not message.get("From"):
            raise IntakeError("missing_message_sender", 400)
        prepared, reports = [], []
        for index, part in enumerate(message.walk()):
            if part.is_multipart():
                continue
            name = part.get_filename() or ""
            if not name and part.get_content_disposition() != "attachment":
                continue
            payload = part.get_payload(decode=True) or b""
            mime = part.get_content_type()
            suffix = Path(name).suffix.casefold() or {
                "application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg",
                "image/tiff": ".tiff", "image/bmp": ".bmp",
            }.get(mime, "")
            signatures = {
                ".pdf": ("application/pdf", payload.startswith(b"%PDF-")),
                ".png": ("image/png", payload.startswith(b"\x89PNG\r\n\x1a\n")),
                ".jpg": ("image/jpeg", payload.startswith(b"\xff\xd8\xff")),
                ".jpeg": ("image/jpeg", payload.startswith(b"\xff\xd8\xff")),
                ".tif": ("image/tiff", payload.startswith((b"II*\0", b"MM\0*"))),
                ".tiff": ("image/tiff", payload.startswith((b"II*\0", b"MM\0*"))),
                ".bmp": ("image/bmp", payload.startswith(b"BM")),
            }
            valid = suffix in signatures and signatures[suffix][1] and mime in (
                signatures[suffix][0], "application/octet-stream")
            report = {"part": index, "filename": name[:255],
                      "outcome": "invalid_attachment" if suffix in signatures else "unsupported"}
            if valid:
                blob, original = self.save_blob(payload, suffix)
                with self.connection() as db:
                    existing = db.execute("SELECT id FROM drafts WHERE sha256=?", (blob,)).fetchone()
                result = None if existing else {
                    "fields": {}, "state": "pending", "errors": [], "missing_fields": []}
                prepared.append((index, name[:255], blob, original.name, result))
                report.update(outcome="draft", sha256=blob)
            reports.append(report)
        report = {"receipt": "saved", "attachments": reports}
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.guard(db, data.get("owner"), data.get("fence"))
            if row["epoch"] != epoch:
                raise IntakeError("stale_epoch")
            for index, name, blob, original, result in prepared:
                if result is not None:
                    status = {"outcome": "pending", "label": "Ожидает распознавания",
                              "document_type": "Тип не установлен", "missing_fields": []}
                    key = None
                    db.execute("""INSERT OR IGNORE INTO drafts
                        (sha256,original,result,recognition,business_key,created) VALUES(?,?,?,?,?,?)""",
                               (blob, original, json.dumps(result, ensure_ascii=False),
                                json.dumps(status, ensure_ascii=False), key, self.clock()))
                draft = db.execute("SELECT id FROM drafts WHERE sha256=?", (blob,)).fetchone()[0]
                for item in reports:
                    if item["part"] == index:
                        item["draft_id"] = draft
                db.execute("INSERT INTO attachments VALUES(?,?,?,?,?,?)",
                           (epoch, uid, index, name, draft, "draft"))
            for item in reports:
                if item["outcome"] != "draft":
                    db.execute("INSERT INTO attachments VALUES(?,?,?,?,?,?)",
                               (epoch, uid, item["part"], item["filename"], None, item["outcome"]))
            metadata = {key: str(message.get(header, ""))[:1000] for key, header in
                        (("sender", "From"), ("subject", "Subject"), ("message_id", "Message-ID"))}
            db.execute("INSERT INTO messages(epoch,uid,sha256,metadata,report) VALUES(?,?,?,?,?)",
                       (epoch, uid, digest, json.dumps(metadata, ensure_ascii=False), json.dumps(report)))
            db.execute("UPDATE mailbox SET cursor=? WHERE id=1", (uid,))
        return report

    def recognize(self, data):
        if not isinstance(data, dict):
            raise IntakeError("invalid_recognition_request", 400)
        draft_id = positive(data.get("draft_id"), "draft_id")
        with self.connection() as db:
            self.guard(db, data.get("owner"), data.get("fence"))
            draft = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if not draft:
                raise IntakeError("not_found", 404)
            if draft["sha256"] != data.get("sha256"):
                raise IntakeError("hash_mismatch", 400)
            if json.loads(draft["result"])["state"] != "pending":
                return {"recognition": json.loads(draft["recognition"])}
        path = self.root / "originals" / draft["original"]
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != draft["sha256"]:
            raise IntakeError("original_corrupt", 500)
        result = self.recognizer([path])
        classify_mail_document(result["fields"])
        status, key = recognition_status(result, result["state"]), identity(result["fields"])
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(db, data.get("owner"), data.get("fence"))
            db.execute("UPDATE drafts SET result=?,recognition=?,business_key=? WHERE id=?",
                       (json.dumps(result, ensure_ascii=False), json.dumps(status, ensure_ascii=False), key, draft_id))
            if key and db.execute("SELECT COUNT(*) FROM drafts WHERE business_key=?", (key,)).fetchone()[0] > 1:
                db.execute("UPDATE drafts SET conflict=1 WHERE business_key=?", (key,))
        return {"recognition": status}

    def status(self):
        with self.connection() as db:
            row = dict(db.execute("SELECT * FROM mailbox WHERE id=1").fetchone())
            counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("messages", "drafts", "attachments")}
            failed = db.execute("""SELECT COUNT(*) FROM drafts
                WHERE json_extract(recognition,'$.outcome') IN ('failed','incomplete','unclassified')""").fetchone()[0]
            return {key: row[key] for key in
                    ("epoch", "cursor", "last_attempt", "last_success", "last_error", "backlog")} | {
                        "counts": counts, "recognition_attention": failed,
                        "pending_recognition": db.execute(
                            "SELECT COUNT(*) FROM drafts WHERE json_extract(result,'$.state')='pending'").fetchone()[0],
                        "invalid_attachments": db.execute(
                            "SELECT COUNT(*) FROM attachments WHERE outcome='invalid_attachment'").fetchone()[0],
                        "conflicts": db.execute("SELECT COUNT(*) FROM drafts WHERE conflict=1").fetchone()[0]}


def register_mail_intake(app, *, root=None, token=None, mailbox=None, enabled=None,
                         recognizer=bounded_recognize, review_authorized=None):
    enabled = os.environ.get("SMARTREPORT_MAIL_INTAKE_ENABLED") == "1" if enabled is None else enabled
    if not enabled:
        return None
    root = root or os.environ.get("SMARTREPORT_MAIL_INTAKE_DIR")
    token = token or os.environ.get("SMARTREPORT_MAIL_INTAKE_TOKEN")
    mailbox = mailbox or os.environ.get("SMARTREPORT_MAILBOX_ID")
    if not root or not Path(root).is_absolute() or not token or len(token) < 32:
        raise RuntimeError("Mail intake requires an absolute isolated directory and a dedicated >=32 character token")
    if token == os.environ.get("SYNC_API_KEY"):
        raise RuntimeError("Mail intake must not reuse the expense API key")
    if not mailbox or not re.fullmatch(r"[0-9a-f]{64}", mailbox):
        raise RuntimeError("Mail intake requires the expected mailbox identity hash")
    store = IntakeStore(Path(root), recognizer, mailbox=mailbox)
    blueprint = Blueprint("mail_intake", __name__, url_prefix="/api/mail-intake")

    @blueprint.before_request
    def authorize():
        provided = request.headers.get("Authorization", "")
        review_read = (request.method == "GET" and request.endpoint in
                       ("mail_intake.review", "mail_intake.original"))
        if review_read and review_authorized is not None and review_authorized():
            return None
        if not hmac.compare_digest(provided.encode(), f"Bearer {token}".encode()):
            return jsonify(error="unauthorized"), 401
        if request.content_length and request.content_length > MAX_MESSAGE:
            return jsonify(error="message_too_large"), 413

    @blueprint.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @blueprint.errorhandler(IntakeError)
    def invalid(error):
        return jsonify(error=error.code), error.status

    @blueprint.post("/lease")
    def lease():
        data = request.get_json()
        if not isinstance(data, dict):
            raise IntakeError("invalid_lease", 400)
        return jsonify(store.acquire(data.get("mailbox")))

    @blueprint.post("/control")
    def control():
        return jsonify(store.control(request.get_json()))

    @blueprint.post("/messages")
    def ingest():
        if request.mimetype != "message/rfc822":
            raise IntakeError("message_rfc822_required", 415)
        raw = request.stream.read(MAX_MESSAGE + 1)
        if not raw or len(raw) > MAX_MESSAGE:
            raise IntakeError("invalid_message_size", 413)
        data = {}
        for key in ("uid", "epoch", "fence"):
            try:
                data[key] = int(request.args[key])
            except (KeyError, ValueError):
                raise IntakeError(f"invalid_{key}", 400) from None
        data.update(owner=request.args.get("owner"), sha256=request.headers.get("X-Content-SHA256"))
        return jsonify(store.ingest(data, raw))

    @blueprint.get("/status")
    def status():
        return jsonify(store.status())

    @blueprint.get("/messages")
    def messages():
        try:
            after = positive(int(request.args.get("after", "0")), "after", True)
        except ValueError:
            raise IntakeError("invalid_after", 400) from None
        with store.connection() as db:
            items = [dict(row) for row in db.execute(
                "SELECT * FROM messages WHERE id>? ORDER BY id LIMIT 50", (after,))]
        for item in items:
            item["metadata"], item["report"] = json.loads(item["metadata"]), json.loads(item["report"])
        return jsonify(messages=items)

    @blueprint.get("/pending")
    def pending():
        with store.connection() as db:
            items = [dict(row) for row in db.execute(
                """SELECT id,sha256 FROM drafts WHERE json_extract(result,'$.state')='pending'
                   ORDER BY id LIMIT 10""")]
        return jsonify(drafts=items)

    @blueprint.post("/recognize")
    def recognize():
        return jsonify(store.recognize(request.get_json()))

    @blueprint.get("/drafts")
    def drafts():
        try:
            after = positive(int(request.args.get("after", "0")), "after", True)
        except ValueError:
            raise IntakeError("invalid_after", 400) from None
        with store.connection() as db:
            items = []
            for row in db.execute("SELECT * FROM drafts WHERE id>? ORDER BY id LIMIT 50", (after,)):
                item = dict(row)
                item["result"] = json.loads(item["result"])
                item["recognition"] = json.loads(item["recognition"])
                item["requires_manual_review"] = True
                item["sources"] = [dict(source) for source in db.execute(
                    """SELECT a.epoch,a.uid,a.part,a.filename,m.metadata FROM attachments a
                       JOIN messages m USING(epoch,uid) WHERE a.draft_id=?""", (row["id"],))]
                items.append(item)
        return jsonify(drafts=items)

    @blueprint.get("/review")
    def review():
        try:
            after = positive(int(request.args.get("after", "0")), "after", True)
        except ValueError:
            raise IntakeError("invalid_after", 400) from None
        with store.connection() as db:
            received = [dict(row) for row in db.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 50")]
            for item in received:
                item["metadata"], item["report"] = json.loads(item["metadata"]), json.loads(item["report"])
            items = []
            for row in db.execute("SELECT * FROM drafts WHERE id>? ORDER BY id LIMIT 50", (after,)):
                item = dict(row)
                item["result"], item["recognition"] = json.loads(row["result"]), json.loads(row["recognition"])
                item["sources"] = [
                    dict(source) | {"metadata": json.loads(source["metadata"])}
                    for source in db.execute(
                        """SELECT a.epoch,a.uid,a.filename,m.metadata FROM attachments a
                           JOIN messages m USING(epoch,uid) WHERE draft_id=?""", (row["id"],))]
                items.append(item)
        return render_template("mail_drafts.html", drafts=items, received=received, status=store.status())

    @blueprint.get("/drafts/<int:draft_id>/original")
    def original(draft_id):
        with store.connection() as db:
            row = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if not row:
            raise IntakeError("not_found", 404)
        path = store.root / "originals" / row["original"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise IntakeError("original_corrupt", 500)
        return send_file(path, as_attachment=request.args.get("view") != "1", download_name=row["original"])

    app.register_blueprint(blueprint)
    return store


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--recognize":
        raise SystemExit("Only isolated recognition subprocess mode is supported")
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(recognize_file(Path(sys.argv[2])), ensure_ascii=False))
