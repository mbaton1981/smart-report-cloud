"""Password-protected recovery bundles; no Windows profile or DPAPI required."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import getpass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import stat
import sys
import tempfile
import zipfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"SRBACK01"
MAX_BYTES = 512 * 1024 * 1024
PROFILE_DATABASE = "profile/smart_report.db"
PROFILE_INCOMING = "profile/incoming-documents"
PROFILE_MANIFEST = "profile/local-backup-manifest.json"
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
CREDENTIAL_COLUMN = re.compile(r"password|passwd|token|secret|credential", re.IGNORECASE)


class RecoveryError(ValueError):
    pass


def derive(password, salt):
    if len(password) < 16:
        raise RecoveryError("Use a recovery password of at least 16 characters.")
    return Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(password.encode("utf-8"))


def encrypt(files, password):
    if "manifest.json" in files:
        raise RecoveryError("Reserved manifest filename.")
    manifest = {
        "format": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            safe_name(name)
            archive.writestr(name, content)
        archive.writestr("manifest.json", json.dumps(manifest).encode())
    if len(stream.getvalue()) > MAX_BYTES or sum(map(len, files.values())) > MAX_BYTES:
        raise RecoveryError("Bundle exceeds the 512 MiB limit; nothing has been published.")
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    header = MAGIC + salt + nonce
    return header + AESGCM(derive(password, salt)).encrypt(nonce, stream.getvalue(), header)


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or path.is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise RecoveryError("Unsafe archive path.")
    return path


def decrypt(payload, password):
    if len(payload) > MAX_BYTES + 52 or len(payload) < 52 or payload[:8] != MAGIC:
        raise RecoveryError("Unsupported or incomplete recovery bundle.")
    header, salt, nonce = payload[:36], payload[8:24], payload[24:36]
    try:
        plain = AESGCM(derive(password, salt)).decrypt(nonce, payload[36:], header)
    except InvalidTag:
        raise RecoveryError("Incorrect password or damaged bundle; no files restored.") from None
    try:
        with zipfile.ZipFile(io.BytesIO(plain)) as archive:
            infos = archive.infolist()
            if sum(info.file_size for info in infos) > MAX_BYTES:
                raise RecoveryError("Expanded bundle exceeds the safety limit.")
            if len({info.filename for info in infos}) != len(infos):
                raise RecoveryError("Duplicate archive entries.")
            for info in infos:
                safe_name(info.filename)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise RecoveryError("Archive links are not permitted.")
            files = {info.filename: archive.read(info) for info in infos}
        manifest = json.loads(files.pop("manifest.json"))
        if manifest["format"] != 1 or manifest["files"] != {
            name: hashlib.sha256(content).hexdigest() for name, content in files.items()
        }:
            raise RecoveryError("Recovery manifest mismatch.")
        return files
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as error:
        raise RecoveryError("Invalid recovery archive.") from error


def publish(folder, files, password):
    """Never replace or prune earlier copies, including when a new backup fails."""
    encrypted = encrypt(files, password)
    if decrypt(encrypted, password) != files:
        raise RecoveryError("Encrypted backup verification failed.")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"SmartReport-{stamp}-{secrets.token_hex(6)}.srbackup"
    try:
        with path.open("xb") as output:
            output.write(encrypted)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        # An incomplete new file is not a usable version; old versions are untouched.
        raise RecoveryError("Cannot finish writing the new backup. Check storage and retry.") from None
    if decrypt(path.read_bytes(), password) != files:
        raise RecoveryError("Stored backup verification failed; retain earlier versions.")
    return path


def restore(files, destination):
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, content in files.items():
        target = destination.joinpath(*safe_name(name).parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open("xb") as output:
            if os.name != "nt":
                os.chmod(target, 0o600)
            output.write(content)


def table_hashes(conn):
    return {
        name: hashlib.sha256(repr(sorted(map(tuple, conn.execute(
            'SELECT * FROM "' + name.replace('"', '""') + '"')), key=repr)).encode()).hexdigest()
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'"
        )
    }


def _sqlite_summary(connection, *, reject_credentials=False):
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RecoveryError("A local SQLite snapshot failed its integrity check.")
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    counts = {}
    credential_columns = []
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        counts[table] = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        if reject_credentials:
            credential_columns.extend(
                f"{table}.{row[1]}" for row in connection.execute(f"PRAGMA table_info({quoted})")
                if CREDENTIAL_COLUMN.search(row[1])
            )
    if credential_columns:
        raise RecoveryError("A local SQLite schema contains credential fields; no backup was created.")
    foreign_key_errors = len(list(connection.execute("PRAGMA foreign_key_check")))
    if foreign_key_errors:
        raise RecoveryError("A local SQLite snapshot has broken record links.")
    return {"integrity": True, "table_counts": counts, "foreign_key_errors": foreign_key_errors}


def _snapshot_sqlite(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RecoveryError("A required local SQLite database is missing or unsafe.")
    with tempfile.TemporaryDirectory(prefix="smartreport-profile-db-") as directory:
        snapshot = Path(directory) / "snapshot.sqlite3"
        source_uri = path.resolve(strict=True).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source:
            source.execute("PRAGMA busy_timeout=30000")
            with closing(sqlite3.connect(snapshot, timeout=30)) as destination:
                source.backup(destination, pages=256, sleep=0.02)
                destination.commit()
                summary = _sqlite_summary(destination, reject_credentials=True)
        return snapshot.read_bytes(), summary


def _stable_file_bytes(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RecoveryError("A local incoming file is missing or unsafe.")
    before = path.stat()
    content = path.read_bytes()
    after = path.stat()
    if (len(content) != before.st_size or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns):
        raise RecoveryError("A local incoming file changed while it was being backed up.")
    return content


def _incoming_files(root):
    root = Path(root)
    if root.is_symlink():
        raise RecoveryError("The local incoming-documents path is unsafe.")
    if not root.exists():
        return []
    if not root.is_dir():
        raise RecoveryError("The local incoming-documents path is unsafe.")
    paths = []

    def walk_error(error):
        raise error

    for current, directories, filenames in os.walk(root, onerror=walk_error, followlinks=False):
        current_path = Path(current)
        for name in directories:
            if (current_path / name).is_symlink():
                raise RecoveryError("Links are not allowed in local incoming-documents.")
        for name in filenames:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise RecoveryError("Only regular files are allowed in local incoming-documents.")
            paths.append(path)
    paths.sort(key=lambda path: path.relative_to(root).as_posix().casefold())
    databases = {path for path in paths if path.suffix.casefold() in SQLITE_SUFFIXES}
    filtered = []
    for path in paths:
        sidecar = next((suffix for suffix in SQLITE_SIDECARS if path.name.casefold().endswith(suffix)), None)
        if sidecar:
            database = Path(str(path)[:-len(sidecar)])
            if database not in databases:
                raise RecoveryError("An SQLite sidecar has no matching database.")
            continue
        filtered.append(path)
    return filtered


def _pdf_page_count(content, label):
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as error:
        raise RecoveryError("PDF verification requires the pypdf dependency.") from error

    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise RecoveryError(f"An encrypted PDF cannot be verified: {label}")
        pages = len(reader.pages)
        if pages < 1:
            raise RecoveryError(f"A PDF has no readable pages: {label}")
        return pages
    except (PdfReadError, OSError, ValueError) as error:
        raise RecoveryError(f"A PDF cannot be opened: {label}") from error


def _review_relationships(database, pdf_entries):
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not ({"documents", "originals"} <= tables):
            return None
        columns = {
            table: {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            for table in ("documents", "originals")
        }
        if not ({"sha256"} <= columns["documents"]
                and {"sha256", "content", "filename"} <= columns["originals"]):
            raise RecoveryError("The local review database has an unsupported attachment schema.")
        rows = connection.execute("""
            SELECT d.sha256, o.sha256, o.content
            FROM documents d LEFT JOIN originals o ON o.sha256=d.sha256
        """).fetchall()
        originals = connection.execute("SELECT sha256,filename,content FROM originals").fetchall()
        revisions = connection.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] \
            if "revisions" in tables else 0
    if any(content is None for _, _, content in rows) or any(row[2] is None for row in originals):
        raise RecoveryError("An incoming draft is missing its original PDF bytes.")
    linked = sum(1 for document_sha, original_sha, content in rows
                 if original_sha == document_sha and content is not None)
    blob_matches = sum(
        1 for document_sha, original_sha, content in rows
        if original_sha == document_sha and content is not None
        and hashlib.sha256(content).hexdigest() == document_sha
    )
    original_pdf_hashes = {
        row[0] for row in originals if str(row[1]).casefold().endswith(".pdf")
    }
    disk_pdf_hashes = {hashlib.sha256(content).hexdigest() for _, content in pdf_entries}
    pdf_pages = {}
    for path, content in pdf_entries:
        pdf_hash = hashlib.sha256(content).hexdigest()
        if pdf_hash not in pdf_pages:
            pdf_pages[pdf_hash] = _pdf_page_count(content, path)
    for original_sha, filename, content in originals:
        if str(filename).casefold().endswith(".pdf"):
            digest = hashlib.sha256(content).hexdigest()
            if digest != original_sha:
                continue
            if digest not in pdf_pages:
                pdf_pages[digest] = _pdf_page_count(content, filename)
    return {
        "documents": len(rows),
        "originals": len(originals),
        "revisions": revisions,
        "linked_documents": linked,
        "blob_hash_matches": blob_matches,
        "pdf_files": len(pdf_entries),
        "pdf_files_linked": sum(
            1 for _, content in pdf_entries if hashlib.sha256(content).hexdigest() in original_pdf_hashes
        ),
        "missing_pdf_originals": len(original_pdf_hashes - disk_pdf_hashes),
        "unlinked_pdf_files": sum(
            1 for _, content in pdf_entries if hashlib.sha256(content).hexdigest() not in original_pdf_hashes
        ),
        "pdfs_readable": len(pdf_pages),
        "pdf_pages": sum(pdf_pages.values()),
    }


def snapshot_local_profile(profile_root):
    """Capture only the desktop database and incoming-document tree, never auth files."""
    root = Path(profile_root).resolve(strict=True)
    profile_directory = root / "profile"
    if profile_directory.is_symlink():
        raise RecoveryError("The local profile path is unsafe.")
    database = root / "smart_report.db"
    database_bytes, database_summary = _snapshot_sqlite(database)
    files = {PROFILE_DATABASE: database_bytes}
    databases = {PROFILE_DATABASE: database_summary}

    incoming_root = root / "profile" / "incoming-documents"
    incoming_paths = _incoming_files(incoming_root)
    database_paths = {
        path for path in incoming_paths if path.suffix.casefold() in SQLITE_SUFFIXES
    }
    for path in incoming_paths:
        relative = PurePosixPath(PROFILE_INCOMING) / path.relative_to(incoming_root).as_posix()
        name = relative.as_posix()
        if path in database_paths:
            content, summary = _snapshot_sqlite(path)
            databases[name] = summary
        else:
            content = _stable_file_bytes(path)
        files[name] = content

    pdf_entries = [
        (name, content) for name, content in files.items()
        if name.startswith(PROFILE_INCOMING + "/") and name.casefold().endswith(".pdf")
    ]
    review = {}
    for name in database_paths:
        archive_name = (PurePosixPath(PROFILE_INCOMING)
                        / name.relative_to(incoming_root).as_posix()).as_posix()
        if not {"documents", "originals"} <= set(databases[archive_name]["table_counts"]):
            continue
        with tempfile.TemporaryDirectory(prefix="smartreport-review-check-") as directory:
            staged = Path(directory) / "review.sqlite3"
            staged.write_bytes(files[archive_name])
            review[archive_name] = _review_relationships(staged, pdf_entries)
    file_manifest = {
        name: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for name, content in sorted(files.items())
    }
    manifest = {
        "format": 1,
        "files": file_manifest,
        "databases": databases,
        "incoming_reviews": review,
    }
    files[PROFILE_MANIFEST] = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return files


def _tracked_profile_files(directory):
    profile = Path(directory) / "profile"
    candidates = []
    database = profile / "smart_report.db"
    if database.exists():
        candidates.append(database)
    incoming = profile / "incoming-documents"
    candidates.extend(_incoming_files(incoming))
    result = {}
    for path in candidates:
        if path.name.casefold().endswith(SQLITE_SIDECARS):
            continue
        result[path.relative_to(Path(directory)).as_posix()] = path
    return result


def verify_local_profile(directory):
    directory = Path(directory)
    manifest_path = directory / PROFILE_MANIFEST
    if not manifest_path.is_file():
        return {"present": False, "integrity": False, "files": 0}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
            raise RecoveryError("Unsupported local-profile backup manifest.")
        expected = manifest["files"]
        actual = _tracked_profile_files(directory)
        if set(actual) != set(expected):
            raise RecoveryError("Restored local-profile file inventory does not match the backup.")
        for name, metadata in expected.items():
            safe_name(name)
            if not name.startswith("profile/") or name == PROFILE_MANIFEST:
                raise RecoveryError("Unsafe path in local-profile backup manifest.")
            content = actual[name].read_bytes()
            if (len(content) != metadata.get("size")
                    or hashlib.sha256(content).hexdigest() != metadata.get("sha256")):
                raise RecoveryError("A restored local-profile file failed its SHA-256 check.")
        database_summaries = {}
        for name, expected_summary in manifest.get("databases", {}).items():
            safe_name(name)
            if name not in actual:
                raise RecoveryError("A local-profile SQLite database is missing after restore.")
            with closing(sqlite3.connect(actual[name].resolve().as_uri() + "?mode=ro", uri=True,
                                          timeout=30)) as connection:
                summary = _sqlite_summary(connection, reject_credentials=True)
            if summary != expected_summary:
                raise RecoveryError("Local-profile SQLite counts or links changed after restore.")
            database_summaries[name] = summary["table_counts"]
        expected_databases = {
            PROFILE_DATABASE,
            *(name for name in actual
              if name.startswith(PROFILE_INCOMING + "/")
              and Path(name).suffix.casefold() in SQLITE_SUFFIXES),
        }
        if set(manifest.get("databases", {})) != expected_databases:
            raise RecoveryError("Local-profile database inventory does not match the backup.")

        pdf_entries = [
            (name, path.read_bytes()) for name, path in actual.items()
            if name.startswith(PROFILE_INCOMING + "/") and name.casefold().endswith(".pdf")
        ]
        pdf_page_counts = {}
        for name, content in pdf_entries:
            digest = hashlib.sha256(content).hexdigest()
            if digest not in pdf_page_counts:
                pdf_page_counts[digest] = _pdf_page_count(content, name)
        review_results = {}
        for name in database_summaries:
            if name.startswith(PROFILE_INCOMING + "/"):
                result = _review_relationships(actual[name], pdf_entries)
                if result is not None:
                    review_results[name] = result
        if review_results != manifest.get("incoming_reviews", {}):
            raise RecoveryError("Incoming draft/original links changed after restore.")
        return {
            "present": True,
            "integrity": True,
            "files": len(actual),
            "file_hashes_match": True,
            "sha256_files_checked": len(actual),
            "database_count": len(database_summaries),
            "database_table_counts": database_summaries,
            "incoming_reviews": review_results,
            "incoming_pdf_files": len(pdf_entries),
            "incoming_pdfs_readable": len(pdf_page_counts),
            "incoming_pdf_pages": sum(pdf_page_counts.values()),
            "links_consistent": all(
                item["linked_documents"] == item["documents"]
                and item["blob_hash_matches"] == item["documents"]
                and item["missing_pdf_originals"] == 0
                and item["unlinked_pdf_files"] == 0
                for item in review_results.values()
            ),
        }
    except (OSError, sqlite3.Error, KeyError, TypeError, json.JSONDecodeError) as error:
        raise RecoveryError("Restored local-profile verification failed.") from error


def verify_server_documents(directory):
    directory = Path(directory)
    database = directory / "backup" / "server_snapshot.db"
    manifest_path = directory / "backup" / "manifest.json"
    if not manifest_path.is_file():
        return {"present": False, "verified": False, "files": 0}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "complete":
            raise RecoveryError("The server-document backup manifest is incomplete.")
        expected_snapshot_hash = manifest.get("snapshot_sha256")
        if (not isinstance(expected_snapshot_hash, str)
                or hashlib.sha256(database.read_bytes()).hexdigest() != expected_snapshot_hash):
            raise RecoveryError("The restored server snapshot failed its SHA-256 check.")
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            rows = connection.execute("""
                SELECT document_id, id, original_name, file_sha256 FROM expense_document_attachments
                UNION ALL
                SELECT d.id, NULL, d.original_name, d.file_sha256 FROM expense_documents d
                WHERE d.stored_name IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM expense_document_attachments a WHERE a.document_id=d.id
                )
                ORDER BY 1, 2
            """).fetchall()
        expected = {}
        for document_id, attachment_id, original_name, sha in rows:
            filename = Path(str(original_name or "document").replace("\\", "/")).name
            name = f"{document_id}_{attachment_id or 0}_{filename}"
            expected[name] = sha
        document_root = directory / "backup" / "documents"
        actual = {path.name: path for path in document_root.iterdir() if path.is_file()}
        listed = {
            item["file"]: item for item in manifest.get("files", [])
            if isinstance(item, dict) and isinstance(item.get("file"), str)
        }
        if (len(expected) != manifest.get("expected_originals")
                or set(actual) != set(expected) or set(listed) != set(expected)):
            raise RecoveryError("Restored server-document inventory or record links do not match.")
        pdf_files = 0
        readable_pdfs = 0
        for name, path in actual.items():
            safe_name(name)
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            record = listed[name]
            if (digest != expected[name] or digest != record.get("sha256")
                    or record.get("document_id") not in {None, int(name.split("_", 1)[0])}
                    or record.get("matches_server_record") is not True):
                raise RecoveryError("A restored server original failed its linked SHA-256 check.")
            if path.suffix.casefold() == ".pdf":
                _pdf_page_count(content, name)
                pdf_files += 1
                readable_pdfs += 1
        return {
            "present": True, "verified": True, "files": len(actual),
            "sha256_matches": len(actual), "links_match": True,
            "pdf_files": pdf_files, "pdfs_readable": readable_pdfs,
        }
    except (OSError, sqlite3.Error, KeyError, TypeError, json.JSONDecodeError) as error:
        raise RecoveryError("Restored server-document verification failed.") from error


def verify_object_documents(directory):
    root = Path(directory) / "profile" / "object_documents"
    database = root / "object_records.sqlite3"
    if not database.is_file():
        return {"present": False, "integrity": True, "files": 0, "records": 0}
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            if not integrity:
                raise RecoveryError("Local object-document database integrity check failed.")
            if conn.execute("PRAGMA application_id").fetchone()[0] != 0x534D4F42:
                raise RecoveryError("Local object-document database identity mismatch.")
            if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise RecoveryError("Unsupported local object-document database version.")
            file_rows = conn.execute("SELECT relative_path,sha256 FROM files").fetchall()
            records = (
                conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
                + conn.execute("SELECT COUNT(*) FROM agreements").fetchone()[0]
            )
            file_ids = {row[0] for row in conn.execute("SELECT id FROM files")}
            links = 0
            for table in ("documents", "agreements"):
                links += conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE file_id IS NOT NULL'
                ).fetchone()[0]
                if conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE file_id IS NOT NULL '
                    "AND file_id NOT IN (SELECT id FROM files)"
                ).fetchone()[0]:
                    raise RecoveryError("An object document references a missing file record.")
            if list(conn.execute("PRAGMA foreign_key_check")):
                raise RecoveryError("Object-document database has broken record links.")
        pdf_files = 0
        for relative_path, expected_hash in file_rows:
            relative = safe_name(relative_path)
            path = root.joinpath(*relative.parts).resolve(strict=True)
            try:
                path.relative_to(root.resolve(strict=True))
            except ValueError as error:
                raise RecoveryError("Local object attachment path escapes its storage.") from error
            if not path.is_file():
                raise RecoveryError("Local object attachment is missing or has a hash mismatch.")
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != expected_hash:
                raise RecoveryError("Local object attachment is missing or has a hash mismatch.")
            if path.suffix.casefold() == ".pdf":
                _pdf_page_count(content, relative.as_posix())
                pdf_files += 1
        return {
            "present": True, "integrity": True, "files": len(file_rows), "records": records,
            "links": links, "pdf_files": pdf_files, "pdfs_readable": pdf_files,
        }
    except (OSError, sqlite3.Error, KeyError) as error:
        raise RecoveryError("Local object-document restore verification failed.") from error


def verify_server(directory):
    """Boot the bundled compatible server against the restored copy, never production."""
    directory = Path(directory).resolve()
    database = directory / "backup" / "server_snapshot.db"
    config_path = directory / "recovery" / "configuration.json"
    if config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        config = {
            "SECRET_KEY": secrets.token_urlsafe(32),
            "SYNC_API_KEY": secrets.token_urlsafe(32),
            "ADMIN_PASSWORD": secrets.token_urlsafe(32),
            "ADMIN_USERNAME": "recovery-verifier",
        }
    key = (directory / "recovery" / "employee-card.key").read_text().strip()
    server_documents = verify_server_documents(directory)
    source = directory / "source"
    sys.path.insert(0, str(source))
    os.environ.update(
        DATABASE_PATH=str(database), SECRET_KEY=config["SECRET_KEY"],
        SYNC_API_KEY=config["SYNC_API_KEY"], ADMIN_PASSWORD=config["ADMIN_PASSWORD"],
        SMARTREPORT_EMPLOYEE_CARD_KEY=key,
        SMARTREPORT_DOCUMENTS_DIR=str(directory / "backup" / "documents"),
        USE_SECURE_COOKIES="true",
    )
    if config.get("ADMIN_USERNAME"):
        os.environ["ADMIN_USERNAME"] = config["ADMIN_USERNAME"]
    os.chdir(directory)
    import socket

    def no_network(*args, **kwargs):
        raise RecoveryError("Network access is forbidden during isolated recovery.")

    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    socket.create_connection = no_network
    with closing(sqlite3.connect(database)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RecoveryError("Restored database integrity check failed.")
        before = table_hashes(connection)
    import server
    import employee_cards
    from cryptography.fernet import Fernet
    with closing(server.get_db_connection()) as connection:
        if table_hashes(connection) != before:
            raise RecoveryError("Server initialization changed restored tables.")
        rows = connection.execute(
            "SELECT id,card_cipher FROM meta_employees WHERE card_cipher IS NOT NULL AND card_cipher != ''"
        ).fetchall()
        if not rows:
            raise RecoveryError("No encrypted cards available to verify.")
        for row in rows:
            employee_cards.decrypt_card(row["card_cipher"], row["id"])
        os.environ["SMARTREPORT_EMPLOYEE_CARD_KEY"] = Fernet.generate_key().decode()
        try:
            employee_cards.decrypt_card(rows[0]["card_cipher"], rows[0]["id"])
        except employee_cards.CardError:
            pass
        else:
            raise RecoveryError("Wrong card key unexpectedly accepted.")
        os.environ["SMARTREPORT_EMPLOYEE_CARD_KEY"] = key
        # Check the protected HTTP reader without issuing tokens or writing the database.
        client = server.app.test_client()
        import employee_cards_api
        # Register only the existing card routes in an isolated authenticated test app.
        from flask import Flask
        test_app = Flask("isolated_card_reader")
        employee_cards_api.register_employee_cards(
            test_app, server.get_db_connection, lambda: {"id": 0, "role": "admin"}, None,
        )
        path = f"/api/employee-cards/{rows[0]['id']}"
        from sync_dataset import HEADER, file_dataset
        headers = {HEADER: file_dataset(database)}
        if client.get(path, base_url="https://isolated.invalid", headers=headers).status_code != 403:
            raise RecoveryError("Unauthenticated production reader was not denied.")
        response = test_app.test_client().get(path, base_url="https://isolated.invalid")
        if response.status_code != 200:
            raise RecoveryError("Restored card reader failed.")
        if table_hashes(connection) != before:
            raise RecoveryError("Recovery verification changed database contents.")
    return {
        "database_integrity": True, "all_business_table_hashes_unchanged": True,
        "encrypted_cards_read": len(rows), "wrong_key_denied": True,
        "unauthenticated_http_denied": True, "authenticated_http_read": True,
        "dpapi_used": "desktop_auth" in sys.modules, "production_accessed": False,
        "network_disabled_in_verifier": True, "python_prefix": sys.prefix,
        "server_documents": server_documents,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--password-stdin", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    password = (sys.stdin.buffer.readline().decode("utf-8").rstrip("\r\n")
                if args.password_stdin else getpass.getpass("Recovery password (not stored): "))
    try:
        files = decrypt(args.bundle.read_bytes(), password)
        if args.verify_only:
            with tempfile.TemporaryDirectory(prefix="smartreport-independent-") as temp:
                destination = Path(temp) / "restored"
                restore(files, destination)
                original_cwd = Path.cwd()
                try:
                    result = verify_server(destination)
                finally:
                    os.chdir(original_cwd)
                result["object_documents"] = verify_object_documents(destination)
                result["local_profile"] = verify_local_profile(destination)
                print(json.dumps(result))
        else:
            if not args.destination:
                raise RecoveryError("Specify a NEW empty --destination, never a production path.")
            restore(files, args.destination)
            print("Restored to a NEW directory. Do not replace any live database.")
    except RecoveryError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as error:
        import traceback
        frames = [(Path(frame.filename).name, frame.name, frame.lineno)
                  for frame in traceback.extract_tb(error.__traceback__)]
        print(json.dumps({
            "error": type(error).__name__, "errno": getattr(error, "errno", None),
            "sqlite_code": getattr(error, "sqlite_errorname", None), "frames": frames,
        }), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
