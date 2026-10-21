"""Isolated desktop profile: immutable intake snapshot, revisions, atomic test register."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from expense_documents import decimal_text, parse_invoice_money, required_document_fields
from invoice_processing import recognize_invoice
from mail_import import classify_mail_document


APPLICATION_ID = 0x53525452
EDITABLE = (
    "supplier_name", "invoice_number", "payment_reference", "document_date", "due_date",
    "currency_code", "net_amount", "vat_amount", "gross_amount", "rounding_amount", "amount_due",
    "confirmed_type", "object_id", "original_checked",
    "settlement_status",
)
MONEY = ("net_amount", "vat_amount", "gross_amount", "rounding_amount", "amount_due")


class ReviewError(ValueError):
    pass


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def current_classification(fields):
    fresh = dict(fields)
    if "review_reasons" in fresh:
        fresh["review_reasons"] = list(fresh["review_reasons"])
    classify_mail_document(fresh)
    return fresh


class OfflineReviewStore:
    def __init__(self, root: Path):
        if not Path(root).is_absolute():
            raise ReviewError("Укажите абсолютный путь отдельного тестового профиля.")
        self.root = Path(root).resolve()
        path = self.root / "review.sqlite3"
        if self.root.exists() and not path.exists():
            raise ReviewError("Нужен новый каталог или существующий тестовый профиль проверки.")
        if path.exists():
            probe = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            try:
                if probe.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                    raise ReviewError("Это не тестовый профиль проверки.")
            finally:
                probe.close()
        else:
            self.root.mkdir(parents=False)
        self.conn = sqlite3.connect(path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self.conn.execute("PRAGMA application_id").fetchone()[0] == 0:
            self.conn.executescript(f"""
                PRAGMA application_id={APPLICATION_ID};
                CREATE TABLE originals(
                    sha256 TEXT PRIMARY KEY, content BLOB NOT NULL, filename TEXT NOT NULL);
                CREATE TABLE documents(
                    id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE REFERENCES originals,
                    source_json TEXT NOT NULL, base_json TEXT NOT NULL);
                CREATE TABLE objects(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
                INSERT INTO objects VALUES (1,'ТЕСТ — объект A'),(2,'ТЕСТ — объект B');
                CREATE TABLE revisions(
                    document_id INTEGER NOT NULL REFERENCES documents, revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL, changes_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(document_id,revision));
                CREATE TABLE register(
                    id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL UNIQUE REFERENCES documents,
                    client_id TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL,
                    business_key TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL,
                    sha256 TEXT NOT NULL REFERENCES originals, debt_amount TEXT NOT NULL,
                    created_at TEXT NOT NULL);
            """)
            self.conn.commit()

    def close(self):
        self.conn.close()

    def import_snapshot(self, source: Path):
        source = Path(source).resolve()
        if self.root == source or self.root in source.parents or source in self.root.parents:
            raise ReviewError("Тестовый профиль и исходная очередь должны находиться отдельно.")
        export = json.loads((source / "result.json").read_text(encoding="utf-8"))
        if not 1 <= len(export["drafts"]) <= 5:
            raise ReviewError("Допускается до пяти сохранённых черновиков.")
        prepared = []
        for draft in export["drafts"]:
            filename = draft["original"]
            if filename != Path(filename).name or not filename.endswith(".pdf"):
                raise ReviewError("Недопустимый путь оригинала.")
            content = (source / "intake" / "originals" / filename).read_bytes()
            if digest(content) != draft["sha256"]:
                raise ReviewError("SHA-256 оригинала не совпадает.")
            fields = current_classification(draft["result"]["fields"])
            # Do not rerun OCR or replace prior recognition; refresh only the reference
            # on text-based invoices, retaining the new parser's coordinate evidence.
            if fields.get("document_type") == "invoice" and not fields.get("payment_reference"):
                parsed = recognize_invoice([source / "intake" / "originals" / filename])
                if parsed["errors"]:
                    raise ReviewError("Не удалось обновить OCR/reference; исходная очередь не изменена.")
                ref = parsed["fields"].get("payment_reference")
                if ref:
                    fields["payment_reference"] = ref
                    fields.setdefault("field_sources", {})["payment_reference"] = (
                        parsed["fields"].get("field_sources", {}).get("payment_reference", []))
            prepared.append((draft, fields, content))
        with self.conn:
            for draft, fields, content in prepared:
                old = self.conn.execute("SELECT * FROM documents WHERE id=?", (draft["id"],)).fetchone()
                if old:
                    if old["sha256"] != draft["sha256"] or old["source_json"] != packed(draft):
                        raise ReviewError("Исходный снимок изменился; автоматическая замена запрещена.")
                    continue
                self.conn.execute("INSERT OR IGNORE INTO originals VALUES (?,?,?)",
                                  (draft["sha256"], content, draft["original"]))
                self.conn.execute("INSERT INTO documents VALUES (?,?,?,?)",
                                  (draft["id"], draft["sha256"], packed(draft), packed(fields)))
                original_fields = draft["result"]["fields"]
                changes = {key: {"before": original_fields.get(key), "after": fields.get(key),
                                 "origin": "Актуальная классификация / исправленный парсер"}
                           for key in ("document_type", "payment_reference")
                           if original_fields.get(key) != fields.get(key)}
                initial = {**{key: fields.get(key) for key in EDITABLE},
                           "confirmed_type": None, "object_id": None, "original_checked": False}
                self.conn.execute("INSERT INTO revisions VALUES (?,?,?,?,?)",
                                  (draft["id"], 0, packed(initial), packed(changes),
                                   datetime.now(timezone.utc).isoformat()))

    def documents(self):
        return [dict(row) for row in self.conn.execute("SELECT id,sha256 FROM documents ORDER BY id")]

    def objects(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM objects ORDER BY id")]

    def base(self, document_id):
        row = self.conn.execute("SELECT base_json FROM documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            raise ReviewError("Черновик не найден.")
        return current_classification(json.loads(row[0]))

    def latest(self, document_id):
        row = self.conn.execute(
            "SELECT * FROM revisions WHERE document_id=? ORDER BY revision DESC LIMIT 1",
            (document_id,)).fetchone()
        if row:
            return row["revision"], json.loads(row["payload_json"])
        base = self.base(document_id)
        return 0, {**{key: base.get(key) for key in EDITABLE},
                   "confirmed_type": None, "object_id": None, "original_checked": False}

    def provenance(self, document_id, payload):
        base = self.base(document_id)
        provenance = {}
        for key in EDITABLE:
            value = payload.get(key)
            original_value = base.get(key)
            value = None if value in (None, "", "UNKNOWN") else value
            original_value = None if original_value in (None, "", "UNKNOWN") else original_value
            if value != original_value and key not in (
                    "confirmed_type", "object_id", "original_checked"):
                provenance[key] = "Исправлено вручную"
            elif any(str(source.get("kind", "")).startswith("derived")
                     for source in base.get("field_sources", {}).get(key, [])):
                provenance[key] = "Вычислено из указанных итогов"
            elif base.get(key) not in (None, "", "UNKNOWN"):
                provenance[key] = "Считано — требуется сверка"
                captions = {source["caption"] for source in
                            base.get("field_sources", {}).get(key, []) if source.get("caption")}
                if key in ("document_date", "due_date") and captions:
                    provenance[key] += ": " + "; ".join(sorted(captions))
            else:
                provenance[key] = "Не указано / не распознано"
        return provenance

    def save(self, document_id, payload, expected_revision):
        cleaned = self.clean_payload(payload)
        return self.save_revision(document_id, cleaned, expected_revision)

    def clean_payload(self, payload):
        if set(payload) - set(EDITABLE):
            raise ReviewError("Неизвестные поля исправления.")
        cleaned = {}
        for key in EDITABLE:
            value = payload.get(key)
            if key in MONEY and value not in (None, ""):
                try:
                    value = decimal_text(parse_invoice_money(value))
                except InvalidOperation:
                    raise ReviewError(f"Сумма слишком велика: {key}.") from None
                if value is None or not Decimal(value).is_finite():
                    raise ReviewError(f"Некорректная сумма: {key}.")
            elif key not in ("object_id", "original_checked"):
                value = str(value).strip() if value not in (None, "") else None
            cleaned[key] = value
        return cleaned

    def save_revision(self, document_id, cleaned, expected_revision):
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            revision, previous = self.latest(document_id)
            if revision != expected_revision:
                raise ReviewError("Черновик изменён в другом окне. Откройте его заново.")
            if self.card(document_id):
                raise ReviewError("Карточка уже перенесена; исходное подтверждение неизменно.")
            changes = {key: {"before": previous.get(key), "after": cleaned[key]}
                       for key in cleaned if previous.get(key) != cleaned[key]}
            self.conn.execute("INSERT INTO revisions VALUES (?,?,?,?,?)",
                              (document_id, revision + 1, packed(cleaned), packed(changes),
                               datetime.now(timezone.utc).isoformat()))
        return revision + 1

    def history(self, document_id):
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM revisions WHERE document_id=? ORDER BY revision", (document_id,))]

    def original(self, document_id):
        row = self.conn.execute(
            "SELECT originals.* FROM originals JOIN documents USING(sha256) WHERE documents.id=?",
            (document_id,)).fetchone()
        if row is None or digest(row["content"]) != row["sha256"]:
            raise ReviewError("Оригинал отсутствует или повреждён.")
        folder = self.root / "originals"
        folder.mkdir(exist_ok=True)
        path = folder / row["filename"]
        if path.exists():
            if digest(path.read_bytes()) != row["sha256"]:
                raise ReviewError("Локальная копия оригинала повреждена; перенос заблокирован.")
        else:
            with path.open("xb") as stream:
                stream.write(row["content"])
        return path

    def issues(self, document_id, payload):
        base = self.base(document_id)
        issues = []
        kind = base.get("document_type")
        if kind not in ("invoice", "receipt"):
            issues.append("Актуальная классификация запрещает перенос как новой фактуры/чека.")
        if payload.get("confirmed_type") != kind:
            issues.append("Явно подтвердите тип документа после сверки оригинала.")
        if payload.get("original_checked") is not True:
            issues.append("Подтвердите сверку оригинала.")
        if type(payload.get("object_id")) is not int or payload["object_id"] not in {
                row["id"] for row in self.objects()}:
            issues.append("Обязательно выберите тестовый объект.")
        for key in required_document_fields(kind):
            if payload.get(key) in (None, "", "UNKNOWN"):
                issues.append(f"Не заполнено обязательное поле: {key}.")
        for key in ("document_date", "due_date"):
            if payload.get(key):
                try:
                    parsed = date.fromisoformat(payload[key])
                    if parsed.isoformat() != payload[key]:
                        raise ValueError()
                except (TypeError, ValueError):
                    issues.append(f"Некорректная дата: {key}.")
        if payload.get("currency_code") not in ("SEK", "EUR", "NOK", "DKK", "USD", "GBP", "CHF", "PLN"):
            issues.append("Выберите поддерживаемую валюту.")
        amounts = {}
        for key in MONEY:
            raw = payload.get(key)
            amounts[key] = parse_invoice_money(raw) if raw not in (None, "") else None
            if raw not in (None, "") and (
                    amounts[key] is None or not amounts[key].is_finite()):
                issues.append(f"Некорректная сумма: {key}.")
        net, vat = amounts["net_amount"], amounts["vat_amount"]
        total = amounts["amount_due"] if kind == "invoice" else amounts["gross_amount"]
        if all(value is not None and value.is_finite() for value in (net, vat, total)):
            rounding = amounts["rounding_amount"]
            if net < 0 or vat < 0 or total <= 0:
                issues.append("Для фактуры/чека нужны неотрицательные суммы и положительный итог.")
            if net + vat + (rounding if rounding is not None else Decimal(0)) != total:
                issues.append("Нетто + НДС + указанное округление не совпадают с итогом.")
        if kind == "receipt" and payload.get("amount_due") not in (None, ""):
            issues.append("Чек не создаёт задолженность. Поле долга должно быть пустым.")
        return issues

    def card(self, document_id):
        row = self.conn.execute("SELECT * FROM register WHERE document_id=?", (document_id,)).fetchone()
        return dict(row) if row else None

    def transfer(self, document_id, expected_revision):
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            # Reclassify even persisted pre-fix drafts and repeated requests.
            revision, payload = self.latest(document_id)
            problems = self.issues(document_id, payload)
            if problems:
                raise ReviewError("\n".join(problems))
            if revision != expected_revision or revision == 0:
                raise ReviewError("Сохраните актуальную версию исправлений перед переносом.")
            original = self.original(document_id)
            sha = digest(original.read_bytes())
            previous = self.card(document_id)
            if previous:
                return previous
            supplier = re.sub(r"\W+", "", payload["supplier_name"].casefold())
            kind = payload["confirmed_type"]
            identity = ([supplier, "invoice", payload["invoice_number"].casefold()] if kind == "invoice"
                        else [supplier, "receipt", payload["document_date"],
                              payload["gross_amount"], payload["currency_code"]])
            key = digest(packed(identity).encode())
            if self.conn.execute("SELECT 1 FROM register WHERE business_key=?", (key,)).fetchone():
                raise ReviewError("Возможный повтор покупки с другим PDF. Нужна ручная сверка; перенос остановлен.")
            for row in self.conn.execute("SELECT payload_json FROM register"):
                other = json.loads(row[0])
                if (re.sub(r"\W+", "", other["supplier_name"].casefold()) == supplier
                        and other["document_date"] == payload["document_date"]
                        and other["currency_code"] == payload["currency_code"]
                        and (other["amount_due"] or other["gross_amount"]) ==
                        (payload["amount_due"] or payload["gross_amount"])):
                    raise ReviewError("Совпадают продавец, дата и итог покупки. Нужна ручная сверка документов.")
            self.conn.execute("INSERT INTO register(document_id,client_id,revision,business_key,"
                              "payload_json,sha256,debt_amount,created_at) VALUES (?,?,?,?,?,?,?,?)",
                              (document_id, uuid.uuid4().hex, revision, key, packed(payload), sha,
                               payload["amount_due"] if kind == "invoice" else "0.00",
                               datetime.now(timezone.utc).isoformat()))
            return self.card(document_id)
