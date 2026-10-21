"""Isolated, read-only Loopia trial. Never imports the desktop or server."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import imaplib
import json
import os
import re
import sqlite3
import ssl
import sys
import tempfile
import uuid
import warnings
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

from expense_documents import decimal_text, non_invoice_document_type, sha256_file
from invoice_processing import recognize_invoice


ADDRESS = "faktura@byggerochrenoverar.se"
HOST = "mailcluster.loopia.se"
APPLICATION_ID = 0x534D4149
MAX_MESSAGE_BYTES = 30 * 1024 * 1024
SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
MIME_SUFFIXES = {
    "application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg",
    "image/tiff": ".tiff", "image/bmp": ".bmp",
}


class MailImportError(RuntimeError):
    pass


@dataclass
class ImportSummary:
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    since: str = ""
    until: str = ""
    search_candidates: int = 0
    messages_checked: int = 0
    messages_in_period: int = 0
    attachments_found: int = 0
    supported_attachments: int = 0
    documents_added: int = 0
    duplicates_skipped: int = 0
    errors: int = 0
    completed: bool = False
    stop_reason: str | None = None


def classify_mail_document(fields: dict) -> None:
    text = fields.get("raw_text") or ""
    detected_type = non_invoice_document_type(text)
    if detected_type == "tax_decision":
        fields["document_type"] = "tax_decision"
        fields.update(
            supplier_name=None, supplier_org_number=None, supplier_vat_number=None,
            invoice_number=None, document_date=None, due_date=None, payment_reference=None,
        )
        reason = "Решение о транспортном налоге, не фактура. Получатель не является поставщиком; НДС не подставляется."
    elif detected_type == "order_confirmation":
        fields["document_type"] = "order_confirmation"
        fields.update(
            supplier_name=None, supplier_org_number=None, supplier_vat_number=None,
            invoice_number=None, document_date=None, due_date=None, payment_reference=None,
        )
        reason = "Подтверждение заказа, не фактура. До учёта требуется отдельная проверка документа."
    elif re.search(r"\bpro[\s-]?forma(?:faktura|\s+invoice)?\b", text, re.IGNORECASE):
        fields["document_type"] = "proforma"
        reason = "Проформа: только проверка, передача в учёт отключена."
    elif (not any(fields.get(key) for key in ("invoice_number", "net_amount", "gross_amount", "amount_due"))
          and not re.search(r"faktura|invoice|kvitto|receipt|kontantnota", text, re.IGNORECASE)):
        fields["document_type"] = "unclassified"
        reason = "Нет подтверждённых признаков финансового документа; возможно, это изображение из подписи письма."
    else:
        return
    fields.setdefault("review_reasons", []).append(reason)


def mail_review_warnings(fields: dict) -> list[str]:
    """Safe, deterministic notices; never expose arbitrary OCR/mail error text."""
    labels = {
        "invoice": "Фактура", "receipt": "Чек", "credit_note": "Кредитная фактура",
        "transfer_receipt": "Подтверждение перевода", "proforma": "Проформа",
        "tax_decision": "Решение о транспортном налоге",
        "order_confirmation": "Подтверждение заказа", "unclassified": "Тип не установлен",
    }
    kind = fields.get("document_type")
    notices = [f"Тип документа: {labels.get(kind, 'Тип не установлен')}.",
               "Только тестовый черновик: расходы и долг не создаются, перенос в учёт отключён."]
    specific = {
        "receipt": "Чек: требуется проверить оплату, возмещение сотруднику и отсутствие повторного расхода. Итог чека не означает долг.",
        "tax_decision": "Налоговое решение — не фактура. Требуется выбор категории и распределения; НДС не подставляется.",
        "order_confirmation": "Подтверждение заказа — не фактура. Для учёта нужна сама фактура.",
        "proforma": "Проформа — не основание для автоматического начисления расхода или долга.",
        "credit_note": "Кредитная фактура: проверьте знак сумм и связь с исходной фактурой.",
        "transfer_receipt": "Подтверждение перевода не заменяет фактуру; требуется сверка платежа.",
    }
    if kind in specific:
        notices.append(specific[kind])
    elif kind != "invoice":
        notices.append("Финансовый тип не подтверждён; требуется ручная проверка.")
    if fields.get("vat_amount") in (None, "", "UNKNOWN"):
        notices.append("НДС неизвестен. Ноль или расчётная ставка автоматически не подставляются.")
    if fields.get("amount_due") in (None, "", "UNKNOWN"):
        notices.append("Долг / остаток к оплате не подтверждён; из итога документа не вычисляется.")
    return notices


def business_key(fields: dict) -> str | None:
    values = [
        str(fields.get("supplier_name") or "").strip().casefold(),
        str(fields.get("invoice_number") or "").strip().casefold(),
        fields.get("document_date"), decimal_text(fields.get("gross_amount")),
        str(fields.get("currency_code") or "UNKNOWN").upper(),
    ]
    if not all(values) or values[-1] == "UNKNOWN":
        return None
    return json.dumps(values, ensure_ascii=False)


class MailQueue:
    def __init__(self, directory: Path):
        self.root = directory.resolve()
        self.path = self.root / "mail_queue.sqlite3"
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() and any(self.root.iterdir()):
            raise MailImportError("Test queue requires a new empty directory.")
        exists = self.path.exists()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        if exists and (
            self.conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
            or self.conn.execute("PRAGMA user_version").fetchone()[0] != 1
        ):
            self.conn.close()
            raise MailImportError("Not a supported SmartReport mail trial database.")
        self.conn.execute("PRAGMA foreign_keys=ON")
        if not exists:
            with self.conn:
                self.conn.executescript("""
                    CREATE TABLE documents (
                        id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
                        original TEXT NOT NULL, state TEXT NOT NULL,
                        result_json TEXT NOT NULL, business_key TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX document_business_key ON documents(business_key);
                    CREATE TABLE sources (
                        mailbox TEXT NOT NULL, folder TEXT NOT NULL,
                        uidvalidity TEXT NOT NULL, uid TEXT NOT NULL,
                        part INTEGER NOT NULL, document_id INTEGER NOT NULL REFERENCES documents(id),
                        message_id TEXT, sender TEXT, subject TEXT, received_at TEXT NOT NULL,
                        filename TEXT, content_type TEXT,
                        PRIMARY KEY(mailbox, folder, uidvalidity, uid, part)
                    );
                    CREATE TABLE issues (
                        uidvalidity TEXT NOT NULL, uid TEXT NOT NULL, part INTEGER NOT NULL,
                        code TEXT NOT NULL, PRIMARY KEY(uidvalidity, uid, part)
                    );
                """)
                self.conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
                self.conn.execute("PRAGMA user_version=1")
        (self.root / "originals").mkdir(exist_ok=True)

    def close(self):
        self.conn.close()

    def issue(self, validity: str, uid: str, part: int, code: str):
        # Never persist exception text: an IMAP response may contain private data.
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO issues VALUES (?, ?, ?, ?)",
                (validity, uid, part, code),
            )

    def clear_issue(self, validity: str, uid: str, part: int):
        with self.conn:
            self.conn.execute(
                "DELETE FROM issues WHERE uidvalidity=? AND uid=? AND part=?",
                (validity, uid, part),
            )

    def accept(self, payload: bytes, suffix: str, source: dict, recognizer=recognize_invoice):
        if suffix not in SUFFIXES:
            raise MailImportError("Unsupported attachment extension.")
        digest = hashlib.sha256(payload).hexdigest()
        existing = self.conn.execute("SELECT original FROM documents WHERE sha256=?", (digest,)).fetchone()
        original = self.root / existing["original"] if existing else self.root / "originals" / f"{digest}{suffix}"
        if original.exists():
            if sha256_file(original) != digest:
                raise MailImportError("Stored original checksum mismatch.")
        else:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=original.parent, suffix=".part", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                if sha256_file(temporary) != digest:
                    raise MailImportError("Original copy checksum mismatch.")
                temporary.replace(original)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        with self.conn:
            inserted = self.conn.execute(
                """INSERT OR IGNORE INTO documents
                   (sha256, original, state, result_json, created_at) VALUES (?, ?, ?, ?, ?)""",
                (digest, str(original.relative_to(self.root)), "pending", "{}",
                 datetime.now(timezone.utc).isoformat()),
            )
            document = self.conn.execute("SELECT * FROM documents WHERE sha256=?", (digest,)).fetchone()
            identity = (ADDRESS, "INBOX", source["uidvalidity"], source["uid"], source["part"])
            previous = self.conn.execute(
                """SELECT document_id FROM sources
                   WHERE mailbox=? AND folder=? AND uidvalidity=? AND uid=? AND part=?""", identity,
            ).fetchone()
            if previous and previous["document_id"] != document["id"]:
                raise MailImportError("Message attachment identity changed.")
            self.conn.execute(
                "INSERT OR IGNORE INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*identity, document["id"], source["message_id"], source["sender"], source["subject"],
                 source["received_at"], source["filename"], source["content_type"]),
            )
        if document["state"] == "pending":
            self.recognize(document["id"], recognizer)
            failed = self.conn.execute(
                "SELECT state FROM documents WHERE id=?", (document["id"],)
            ).fetchone()["state"] == "failed"
        else:
            failed = False
        return inserted.rowcount == 1, failed

    def recognize(self, document_id: int, recognizer=recognize_invoice):
        document = self.conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        original = self.root / document["original"]
        try:
            if sha256_file(original) != document["sha256"]:
                raise MailImportError("Stored original checksum mismatch.")
            result = recognizer([original])
            fields = result["fields"]
            classify_mail_document(fields)
            state = "failed" if result["state"] == "failed" else "requires_review"
            encoded = json.dumps(result, ensure_ascii=False)
            key = business_key(fields)
        except Exception as error:
            state, key = "failed", None
            encoded = json.dumps({"error_code": type(error).__name__,
                                  "message": f"Recognition failed: {safe_error(error)}; original retained. Retry available."})
        with self.conn:
            self.conn.execute(
                "UPDATE documents SET state=?, result_json=?, business_key=? WHERE id=?",
                (state, encoded, key, document_id),
            )

    def retry_failed(self, recognizer=recognize_invoice):
        ids = self.conn.execute("SELECT id FROM documents WHERE state IN ('failed', 'pending')").fetchall()
        for row in ids:
            self.recognize(row["id"], recognizer)

    def review(self) -> dict:
        documents = []
        for row in self.conn.execute("SELECT * FROM documents ORDER BY id"):
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json"))
            item["sources"] = [dict(source) for source in self.conn.execute(
                "SELECT * FROM sources WHERE document_id=?", (row["id"],)
            )]
            item["possible_duplicates"] = [match["id"] for match in self.conn.execute(
                "SELECT id FROM documents WHERE business_key=? AND id<>?",
                (row["business_key"], row["id"]),
            )]
            documents.append(item)
        return {"documents": documents,
                "issues": [dict(row) for row in self.conn.execute("SELECT * FROM issues")]}


def require_ok(response, operation: str):
    status, data = response
    if status != "OK":
        raise MailImportError(f"IMAP rejected {operation}; no mailbox changes were requested.")
    return data


def fetch_metadata(client, uid: str):
    metadata = require_ok(client.uid("FETCH", uid, "(RFC822.SIZE INTERNALDATE)"), "metadata fetch")
    raw_metadata = b" ".join(item for item in metadata if isinstance(item, bytes))
    size = re.search(rb"RFC822.SIZE (\d+)", raw_metadata, re.IGNORECASE)
    stamp = re.search(rb'INTERNALDATE "([^"]+)"', raw_metadata, re.IGNORECASE)
    if not size or not stamp:
        raise MailImportError("Missing message size or server arrival date.")
    received = parsedate_to_datetime(stamp[1].decode("ascii"))
    if received.tzinfo is None:
        raise MailImportError("Server arrival date has no timezone.")
    return received, int(size[1])


def import_inbox(queue: MailQueue, password: str, *, now: datetime | None = None,
                 client_factory=imaplib.IMAP4_SSL, recognizer=recognize_invoice,
                 summary: ImportSummary | None = None) -> ImportSummary:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Import time must include a timezone.")
    summary = summary if summary is not None else ImportSummary()
    summary.since = (now - timedelta(days=7)).isoformat()
    summary.until = now.isoformat()
    try:
        _import_inbox(queue, password, now, client_factory, recognizer, summary)
        summary.completed = True
    except (Exception, KeyboardInterrupt) as error:
        summary.errors += 1
        summary.stop_reason = safe_error(error) if isinstance(error, Exception) else type(error).__name__
        raise
    finally:
        reports = queue.root / "runs"
        reports.mkdir(exist_ok=True)
        report_path = reports / f"{now.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex}.json"
        report_path.write_text(
            json.dumps({**asdict(summary), "finished_at": datetime.now(timezone.utc).isoformat()},
                       ensure_ascii=False, indent=2), encoding="utf-8",
        )
    return summary


def _import_inbox(queue, password, now, client_factory, recognizer, summary: ImportSummary):
    since = now - timedelta(days=7)
    client = client_factory(HOST, 993, ssl_context=ssl.create_default_context(), timeout=30)
    validity = ""
    try:
        try:
            require_ok(client.login(ADDRESS, password), "login")
        except imaplib.IMAP4.abort:
            raise
        except imaplib.IMAP4.error:
            raise MailImportError("Loopia login rejected; check the mailbox password.") from None
        require_ok(client.select("INBOX", readonly=True), "read-only INBOX selection")
        validity_data = client.response("UIDVALIDITY")[1]
        if not validity_data or not validity_data[0] or not validity_data[0].isdigit():
            raise MailImportError("Missing UIDVALIDITY; safe message identity is unavailable.")
        validity = validity_data[0].decode("ascii")
        # IMAP SINCE is day-granular. Fetch one extra day, then enforce exact UTC bounds.
        search_day = (since - timedelta(days=1)).date()
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        search_date = f"{search_day.day:02d}-{months[search_day.month - 1]}-{search_day.year}"
        data = require_ok(client.uid("SEARCH", None, "SINCE", search_date), "bounded search")
        uids = data[0].split() if data and data[0] else []
        summary.search_candidates = len(uids)
        for raw_uid in uids:
            uid = raw_uid.decode("ascii")
            try:
                summary.messages_checked += 1
                received, size = fetch_metadata(client, uid)
                if not since <= received <= now:
                    continue
                summary.messages_in_period += 1
                if size > MAX_MESSAGE_BYTES:
                    raise MailImportError("Message exceeds the 30 MiB trial limit.")
                data = require_ok(client.uid("FETCH", uid, "(BODY.PEEK[])"), "read-only message fetch")
                bodies = [item[1] for item in data if isinstance(item, tuple) and len(item) == 2]
                if len(bodies) != 1 or not isinstance(bodies[0], bytes):
                    raise MailImportError("Missing message body.")
                if len(bodies[0]) > MAX_MESSAGE_BYTES:
                    raise MailImportError("Message exceeds the 30 MiB trial limit.")
                message = BytesParser(policy=policy.default).parsebytes(bodies[0])
                for part_index, part in enumerate(message.walk()):
                    if part.is_multipart():
                        continue
                    filename = part.get_filename() or ""
                    is_attachment = bool(filename) or part.get_content_disposition() == "attachment"
                    if is_attachment:
                        summary.attachments_found += 1
                    suffix = Path(filename).suffix.casefold()
                    if suffix not in SUFFIXES:
                        suffix = MIME_SUFFIXES.get(part.get_content_type(), "")
                    if not suffix:
                        continue
                    if not is_attachment:
                        summary.attachments_found += 1
                    summary.supported_attachments += 1
                    try:
                        payload = part.get_payload(decode=True)
                        if not payload:
                            raise MailImportError("Empty attachment.")
                        created, recognition_failed = queue.accept(payload, suffix, {
                            "uidvalidity": validity, "uid": uid, "part": part_index,
                            "message_id": str(message.get("Message-ID", "")),
                            "sender": str(message.get("From", "")),
                            "subject": str(message.get("Subject", "")),
                            "received_at": received.isoformat(), "filename": filename,
                            "content_type": part.get_content_type(),
                        }, recognizer)
                        summary.documents_added += int(created)
                        summary.duplicates_skipped += int(not created)
                        summary.errors += int(recognition_failed)
                        queue.clear_issue(validity, uid, part_index)
                    except (imaplib.IMAP4.abort, OSError, sqlite3.Error):
                        raise
                    except Exception as error:
                        summary.errors += 1
                        queue.issue(validity, uid, part_index, safe_error(error))
                queue.clear_issue(validity, uid, -1)
            except (imaplib.IMAP4.abort, OSError, sqlite3.Error):
                raise
            except Exception as error:
                summary.errors += 1
                queue.issue(validity, uid, -1, safe_error(error))
    finally:
        # LOGOUT only; never CLOSE/EXPUNGE/STORE/MOVE.
        try:
            client.logout()
            queue.clear_issue(validity, "", -1)
        except (imaplib.IMAP4.error, OSError) as error:
            summary.errors += 1
            queue.issue(validity, "", -1, f"logout:{type(error).__name__}")


def safe_error(error: Exception) -> str:
    if isinstance(error, MailImportError):
        return str(error)
    if isinstance(error, ssl.SSLCertVerificationError):
        return "TLS certificate verification failed; insecure fallback is disabled"
    if isinstance(error, TimeoutError):
        return "Connection timed out"
    if isinstance(error, imaplib.IMAP4.abort):
        return "IMAP connection closed unexpectedly"
    if isinstance(error, imaplib.IMAP4.error):
        return "IMAP protocol request failed (private server response omitted)"
    return type(error).__name__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("fetch", "review", "retry"))
    default = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SmartReport" / "mail_faktura_trial"
    parser.add_argument("--data-dir", type=Path, default=default)
    args = parser.parse_args()
    queue = None
    summary = None
    try:
        queue = MailQueue(args.data_dir)
        print(f"Test queue: {queue.path}")
        if args.action == "fetch":
            if not sys.stdin.isatty():
                raise MailImportError("Run in a local interactive terminal for protected password input.")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass(f"Loopia password for {ADDRESS} (not saved): ")
            if not password:
                raise MailImportError("Password is empty.")
            summary = ImportSummary()
            try:
                import_inbox(queue, password, summary=summary)
            finally:
                del password
        elif args.action == "retry":
            queue.retry_failed()
        report = queue.review()
        if args.action == "review":
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"Documents: {len(report['documents'])}; issues: {len(report['issues'])}; "
                  f"failed: {sum(d['state'] == 'failed' for d in report['documents'])}")
        return 2 if report["issues"] or any(d["state"] == "failed" for d in report["documents"]) else 0
    except (MailImportError, imaplib.IMAP4.error, OSError, sqlite3.Error, getpass.GetPassWarning) as error:
        print(f"Import stopped: {safe_error(error)}. Originals and queue retained.", file=sys.stderr)
        return 1
    finally:
        if summary is not None:
            print(f"Run completed: {summary.completed}; messages checked: {summary.messages_checked}; "
                  f"in last 7 days: {summary.messages_in_period}; "
                  f"attachments found: {summary.attachments_found}; "
                  f"PDF/images: {summary.supported_attachments}; "
                  f"documents added: {summary.documents_added}; "
                  f"duplicates skipped: {summary.duplicates_skipped}; errors: {summary.errors}")
        if queue is not None:
            queue.close()


if __name__ == "__main__":
    raise SystemExit(main())
