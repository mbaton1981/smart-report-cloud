"""Draft-only storage and synthetic object catalog inside the mail test queue."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from expense_documents import due_date_issues, net_invoice_issues, parse_invoice_money, sha256_file
from mail_import import MailImportError, MailQueue


TEST_OBJECTS = (
    (101, "ТЕСТ — объект A", "P1022"),
    (102, "ТЕСТ — объект B", "P1034"),
    (103, "ТЕСТ — объект C", "P1035"),
    (104, "ТЕСТ — объект D", "P1024"),
    (105, "ТЕСТ — общий склад", "TEST-STOCK"),
    (106, "ТЕСТ — совпадение 1", "TEST-DUP"),
    (107, "ТЕСТ — совпадение 2", "TEST-DUP"),
)


class TrialStore:
    def __init__(self, root: Path, *, seed_test_objects=True):
        if not (root / "mail_queue.sqlite3").is_file():
            raise MailImportError("Тестовая почтовая очередь не найдена.")
        self.queue = MailQueue(root)
        self.conn = self.queue.conn
        self.root = self.queue.root
        try:
            with self.conn:
                self.conn.executescript("""
                    CREATE TABLE IF NOT EXISTS trial_objects (
                        id INTEGER PRIMARY KEY, name TEXT NOT NULL, markning TEXT NOT NULL,
                        company TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS trial_allocation_revisions (
                        document_id INTEGER NOT NULL REFERENCES documents(id),
                        revision INTEGER NOT NULL, original_sha256 TEXT NOT NULL,
                        created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                        PRIMARY KEY(document_id, revision)
                    );
                """)
                if seed_test_objects and not self.conn.execute("SELECT 1 FROM trial_objects LIMIT 1").fetchone():
                    self.conn.executemany(
                        "INSERT INTO trial_objects VALUES (?, ?, ?, 'ТЕСТОВАЯ ФИРМА — не рабочий каталог')",
                        TEST_OBJECTS,
                    )
        except Exception:
            self.close()
            raise

    def close(self):
        self.queue.close()

    def objects(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM trial_objects ORDER BY id")]

    def document(self, document_id: int):
        row = self.conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            raise MailImportError("Документ отсутствует в тестовой очереди.")
        original = (self.root / row["original"]).resolve()
        if (not original.is_relative_to((self.root / "originals").resolve())
                or not original.is_file() or sha256_file(original) != row["sha256"]):
            raise MailImportError("Не подтверждена целостность оригинала.")
        return dict(row), original

    def latest(self, document_id: int):
        row = self.conn.execute(
            """SELECT * FROM trial_allocation_revisions
               WHERE document_id=? ORDER BY revision DESC LIMIT 1""", (document_id,),
        ).fetchone()
        return {**dict(row), "payload": json.loads(row["payload_json"])} if row else None

    def save(self, document_id: int, payload: dict, expected_revision: int = 0):
        document, _ = self.document(document_id)
        payload = copy.deepcopy(payload)
        if payload.get("verification_status") != "draft":
            raise MailImportError("Допустим только тестовый черновик; подтверждение отключено.")
        rows = payload.get("allocations")
        if not isinstance(rows, list) or payload.get("allocation_basis") != "net":
            raise MailImportError("Ожидается существующая разноска по суммам без НДС.")
        for key in ("net_amount", "vat_amount", "rounding_amount", "amount_due"):
            if payload.get(key) not in (None, "") and parse_invoice_money(payload[key]) is None:
                raise MailImportError(f"Некорректный формат суммы: {key}.")
        if payload.get("due_date"):
            errors = due_date_issues({"due_date": payload["due_date"], "due_date_kind": "manual"})
            if errors:
                raise MailImportError(" ".join(errors))
        catalog = {item["id"]: item for item in self.objects()}
        for row in rows:
            if row.get("destination") not in {"unselected", "object", "company"}:
                raise MailImportError("Неизвестное назначение расходов.")
            if row.get("destination") == "object" and row.get("object_id") is not None:
                if row["object_id"] not in catalog:
                    raise MailImportError("Объект отсутствует в изолированном тестовом каталоге.")
            elif row.get("object_id") is not None:
                raise MailImportError("Для общих расходов/невыбранного назначения объект должен отсутствовать.")
            if row.get("net_amount") not in (None, "") and parse_invoice_money(row["net_amount"]) is None:
                raise MailImportError("Некорректная сумма строки распределения.")
        payload["order_proposals"] = copy.deepcopy(rows)
        payload["trial_validation_issues"] = net_invoice_issues(payload, rows)
        # INSERT-only revisions preserve both edits and incomplete drafts.
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            previous = self.latest(document_id)
            revision = previous["revision"] if previous else 0
            if revision != expected_revision:
                raise MailImportError("Черновик уже изменён в другом окне. Откройте его заново.")
            self.conn.execute(
                "INSERT INTO trial_allocation_revisions VALUES (?, ?, ?, ?, ?)",
                (document_id, revision + 1, document["sha256"],
                 datetime.now(timezone.utc).isoformat(), json.dumps(payload, ensure_ascii=False)),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return revision + 1
