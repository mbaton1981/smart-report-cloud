"""Local, profile-scoped documents and agreements for object cards."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


APPLICATION_ID = 0x534D4F42
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_EMAIL_BODY_CHARS = 500_000
DOCUMENT_CATEGORIES = ("Оферта", "Договор", "Чертёж", "Фото", "Переписка")
OFFER_STATUSES = ("Черновик", "Отправлена", "Согласована")
FILE_SUFFIXES = {
    ".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff",
    ".webp", ".gif", ".heic",
}


class ObjectRecordError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ObjectRecordStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.files_dir = self.root / "files"
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(exist_ok=True)
        self.database = self.root / "object_records.sqlite3"
        self._initialize()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._connection() as connection:
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            table_count = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
            if application_id not in (0, APPLICATION_ID) or (
                table_count and application_id != APPLICATION_ID
            ):
                raise ObjectRecordError("Каталог документов занят другой базой данных.")
            if application_id == APPLICATION_ID and connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0] != 1:
                raise ObjectRecordError("Неизвестная версия каталога документов.")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS objects (
                    object_key TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    company TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    object_key TEXT NOT NULL REFERENCES objects(object_key),
                    original_name TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(object_key, sha256)
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    object_key TEXT NOT NULL REFERENCES objects(object_key),
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    file_id TEXT REFERENCES files(id),
                    created_at TEXT NOT NULL,
                    offer_version INTEGER,
                    offer_status TEXT,
                    email_key TEXT,
                    email_part INTEGER,
                    email_sender TEXT,
                    email_subject TEXT,
                    email_date TEXT,
                    email_body TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS unique_object_email_part
                    ON documents(object_key, email_key, email_part)
                    WHERE email_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS agreements (
                    id TEXT PRIMARY KEY,
                    object_key TEXT NOT NULL REFERENCES objects(object_key),
                    agreement_date TEXT NOT NULL,
                    text TEXT NOT NULL,
                    agreed INTEGER NOT NULL CHECK(agreed IN (0,1)),
                    file_id TEXT REFERENCES files(id),
                    created_at TEXT NOT NULL
                );
            """)
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")

    @staticmethod
    def _validate_object(object_key: str, label: str, company: str):
        if not isinstance(object_key, str) or not object_key.strip() or len(object_key) > 250:
            raise ObjectRecordError("Не удалось определить устойчивый ID объекта.")
        if not isinstance(label, str) or not label.strip() or len(label) > 500:
            raise ObjectRecordError("У объекта отсутствует корректное название.")
        if not isinstance(company, str) or len(company) > 500:
            raise ObjectRecordError("Некорректное название заказчика.")

    def _ensure_object(self, connection, object_key: str, label: str, company: str):
        self._validate_object(object_key, label, company)
        connection.execute(
            """INSERT INTO objects(object_key,label,company) VALUES(?,?,?)
               ON CONFLICT(object_key) DO UPDATE SET label=excluded.label,company=excluded.company""",
            (object_key, label.strip(), company.strip()),
        )

    def _store_bytes(self, connection, object_key: str, filename: str, payload: bytes) -> str:
        if not isinstance(payload, bytes) or not payload:
            raise ObjectRecordError("Файл пуст или не удалось прочитать его содержимое.")
        if len(payload) > MAX_FILE_BYTES:
            raise ObjectRecordError("Размер файла превышает 100 МБ.")
        suffix = Path(filename).suffix.casefold()
        if suffix not in FILE_SUFFIXES:
            raise ObjectRecordError("Поддерживаются PDF, Word и файлы изображений.")
        digest = hashlib.sha256(payload).hexdigest()
        relative_path = Path("files") / f"{digest}{suffix}"
        destination = self.root / relative_path
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise ObjectRecordError("Сохранённый файл повреждён; новый файл не добавлен.")
        else:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.files_dir, delete=False) as output:
                    temporary = Path(output.name)
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(destination)
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
        row = connection.execute(
            "SELECT id FROM files WHERE object_key=? AND sha256=?",
            (object_key, digest),
        ).fetchone()
        if row:
            return row["id"]
        file_id = uuid.uuid4().hex
        connection.execute(
            """INSERT INTO files(id,object_key,original_name,relative_path,sha256,created_at)
               VALUES(?,?,?,?,?,?)""",
            (file_id, object_key, Path(filename).name, relative_path.as_posix(), digest, _now()),
        )
        return file_id

    def add_file(
        self,
        object_key: str,
        label: str,
        company: str,
        source: Path,
        category: str,
        *,
        offer_version: int | None = None,
        offer_status: str | None = None,
    ) -> str:
        if category not in DOCUMENT_CATEGORIES:
            raise ObjectRecordError("Выберите категорию документа.")
        if category == "Оферта":
            if type(offer_version) is not int or offer_version < 1:
                raise ObjectRecordError("Для оферты укажите номер версии.")
            if offer_status not in OFFER_STATUSES:
                raise ObjectRecordError("Выберите статус оферты.")
        elif offer_version is not None or offer_status is not None:
            raise ObjectRecordError("Версия и статус доступны только для оферты.")
        path = Path(source).resolve(strict=True)
        if not path.is_file():
            raise ObjectRecordError("Выберите файл.")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ObjectRecordError("Размер файла превышает 100 МБ.")
        payload = path.read_bytes()
        with self._connection() as connection:
            self._ensure_object(connection, object_key, label, company)
            file_id = self._store_bytes(connection, object_key, path.name, payload)
            document_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO documents
                   (id,object_key,category,title,file_id,created_at,offer_version,offer_status)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (document_id, object_key, category, path.name, file_id, _now(),
                 offer_version, offer_status),
            )
        return document_id

    def update_offer_status(self, document_id: str, status: str):
        if status not in OFFER_STATUSES:
            raise ObjectRecordError("Выберите допустимый статус оферты.")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT category FROM documents WHERE id=?", (document_id,),
            ).fetchone()
            if not row:
                raise ObjectRecordError("Оферта больше не найдена. Обновите список документов.")
            if row["category"] != "Оферта":
                raise ObjectRecordError("Статус можно изменить только у оферты.")
            connection.execute(
                "UPDATE documents SET offer_status=? WHERE id=?",
                (status, document_id),
            )

    def add_email(
        self,
        object_key: str,
        label: str,
        company: str,
        *,
        email_key: str,
        sender: str,
        subject: str,
        email_date: str,
        body: str,
        attachments: list[dict],
    ) -> tuple[str, bool, int]:
        if not isinstance(email_key, str) or not email_key or len(email_key) > 128:
            raise ObjectRecordError("У письма отсутствует устойчивый идентификатор.")
        if not isinstance(body, str) or len(body) > MAX_EMAIL_BODY_CHARS:
            raise ObjectRecordError("Текст письма превышает допустимый размер.")
        if len(attachments) > 100:
            raise ObjectRecordError("В письме слишком много вложений для ручного добавления.")
        with self._connection() as connection:
            self._ensure_object(connection, object_key, label, company)
            parent = connection.execute(
                """SELECT id FROM documents
                   WHERE object_key=? AND email_key=? AND email_part=-1""",
                (object_key, email_key),
            ).fetchone()
            created = parent is None
            if parent:
                message_id = parent["id"]
            else:
                message_id = uuid.uuid4().hex
                connection.execute(
                    """INSERT INTO documents
                       (id,object_key,category,title,created_at,email_key,email_part,
                        email_sender,email_subject,email_date,email_body)
                       VALUES(?,?,'Переписка',?,?,?,-1,?,?,?,?)""",
                    (message_id, object_key, subject.strip() or "(без темы)", _now(),
                     email_key, sender, subject, email_date, body),
                )
            added_attachments = 0
            for attachment in attachments:
                part = attachment.get("part")
                filename = attachment.get("filename")
                payload = attachment.get("content")
                if type(part) is not int or part < 0:
                    raise ObjectRecordError("Некорректный номер вложения.")
                if not isinstance(filename, str) or not isinstance(payload, bytes):
                    raise ObjectRecordError("Некорректные данные вложения.")
                existing = connection.execute(
                    """SELECT id FROM documents
                       WHERE object_key=? AND email_key=? AND email_part=?""",
                    (object_key, email_key, part),
                ).fetchone()
                if existing:
                    continue
                file_id = self._store_bytes(connection, object_key, filename, payload)
                connection.execute(
                    """INSERT INTO documents
                       (id,object_key,category,title,file_id,created_at,email_key,email_part,
                        email_sender,email_subject,email_date)
                       VALUES(?,?,'Переписка',?,?,?,?,?,?,?,?)""",
                    (uuid.uuid4().hex, object_key, filename, file_id, _now(),
                     email_key, part, sender, subject, email_date),
                )
                added_attachments += 1
        return message_id, created, added_attachments

    def add_agreement(
        self,
        object_key: str,
        label: str,
        company: str,
        agreement_date: str,
        text: str,
        agreed: bool,
        attachment: Path | None = None,
    ) -> str:
        try:
            datetime.strptime(agreement_date, "%Y-%m-%d")
        except (TypeError, ValueError) as error:
            raise ObjectRecordError("Укажите дату договорённости.") from error
        clean_text = text.strip() if isinstance(text, str) else ""
        if not clean_text or len(clean_text) > 5000:
            raise ObjectRecordError("Коротко опишите договорённость (до 5000 символов).")
        if type(agreed) is not bool:
            raise ObjectRecordError("Не удалось прочитать отметку согласования.")
        payload = None
        filename = None
        if attachment is not None:
            path = Path(attachment).resolve(strict=True)
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                raise ObjectRecordError("Вложение отсутствует или превышает 100 МБ.")
            payload, filename = path.read_bytes(), path.name
        with self._connection() as connection:
            self._ensure_object(connection, object_key, label, company)
            file_id = self._store_bytes(connection, object_key, filename, payload) if payload is not None else None
            agreement_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO agreements
                   (id,object_key,agreement_date,text,agreed,file_id,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (agreement_id, object_key, agreement_date, clean_text, int(agreed), file_id, _now()),
            )
        return agreement_id

    def list_documents(self, object_key: str) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT d.*,f.original_name,f.relative_path,f.sha256
                   FROM documents d LEFT JOIN files f ON f.id=d.file_id
                   WHERE d.object_key=? ORDER BY d.created_at DESC,d.id""",
                (object_key,),
            ).fetchall()
            return [dict(row) for row in rows]

    def remove_document(self, document_id: str) -> int:
        if not isinstance(document_id, str) or not document_id.strip():
            raise ObjectRecordError("Не удалось определить удаляемую привязку.")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT object_key,email_key FROM documents WHERE id=?",
                (document_id,),
            ).fetchone()
            if not row:
                raise ObjectRecordError("Запись больше не найдена. Обновите список документов.")
            if row["email_key"] is not None:
                cursor = connection.execute(
                    "DELETE FROM documents WHERE object_key=? AND email_key=?",
                    (row["object_key"], row["email_key"]),
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM documents WHERE id=?",
                    (document_id,),
                )
            return cursor.rowcount

    def list_agreements(self, object_key: str) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT a.*,f.original_name,f.relative_path,f.sha256
                   FROM agreements a LEFT JOIN files f ON f.id=a.file_id
                   WHERE a.object_key=? ORDER BY a.agreement_date DESC,a.created_at DESC""",
                (object_key,),
            ).fetchall()
            return [dict(row) for row in rows]

    def document(self, document_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT d.*,f.original_name,f.relative_path,f.sha256
                   FROM documents d LEFT JOIN files f ON f.id=d.file_id WHERE d.id=?""",
                (document_id,),
            ).fetchone()
            return dict(row) if row else None

    def agreement(self, agreement_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT a.*,f.original_name,f.relative_path,f.sha256
                   FROM agreements a LEFT JOIN files f ON f.id=a.file_id WHERE a.id=?""",
                (agreement_id,),
            ).fetchone()
            return dict(row) if row else None

    def file_path(self, relative_path: str, expected_hash: str | None = None) -> Path:
        candidate = (self.root / relative_path).resolve(strict=True)
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise ObjectRecordError("Путь к вложению выходит за пределы хранилища.") from error
        if not candidate.is_file():
            raise ObjectRecordError("Файл вложения не найден.")
        if expected_hash and hashlib.sha256(candidate.read_bytes()).hexdigest() != expected_hash:
            raise ObjectRecordError("Хеш вложения не совпадает; файл не открыт.")
        return candidate


def export_backup_files(root: Path) -> dict[str, bytes]:
    """Create a consistent local metadata snapshot and verified attachment copies."""
    root = Path(root).resolve()
    database = root / "object_records.sqlite3"
    if not database.is_file():
        return {}
    with tempfile.TemporaryDirectory(prefix="smartreport-object-backup-") as directory:
        snapshot_path = Path(directory) / "object_records.sqlite3"
        with closing(sqlite3.connect(database, timeout=30)) as source, \
                closing(sqlite3.connect(snapshot_path)) as snapshot:
            source.backup(snapshot)
            rows = snapshot.execute("SELECT relative_path,sha256 FROM files").fetchall()
        result = {
            "profile/object_documents/object_records.sqlite3": snapshot_path.read_bytes(),
        }
    for relative_path, expected_hash in rows:
        path = (root / relative_path).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ObjectRecordError("Путь резервной копии выходит за пределы хранилища.") from error
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected_hash:
            raise ObjectRecordError("Хеш вложения не совпадает; резервная копия остановлена.")
        archive_name = "profile/object_documents/" + Path(relative_path).as_posix()
        result[archive_name] = payload
    return result
