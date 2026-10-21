"""Full server archives and offline recovery into a new, empty directory."""
import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import requests


class BackupError(RuntimeError):
    """The backup or restored data cannot be certified as complete."""


def _digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _relative_name(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise BackupError(f"Unsafe archive path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise BackupError(f"Unsafe archive path: {value!r}")
    return value


def _database_files(database):
    references = {}
    conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("Database integrity_check failed.")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("Database foreign_key_check failed.")
        for table, query in (
            ("expense_documents", "SELECT id,stored_name,file_sha256 FROM expense_documents"),
            ("expense_document_attachments",
             "SELECT document_id,stored_name,file_sha256 FROM expense_document_attachments"),
        ):
            for document_id, name, digest in conn.execute(query):
                if name is None and digest is None and table == "expense_documents":
                    continue
                _relative_name(name)
                if not isinstance(digest, str) or len(digest) != 64 or any(
                    char not in "0123456789abcdef" for char in digest
                ):
                    raise BackupError(f"Missing or invalid SHA-256: {table}, document {document_id}, {name}")
                if name in references and references[name] != digest:
                    raise BackupError(f"Conflicting database SHA-256: {name}")
                references[name] = digest
    except sqlite3.DatabaseError as error:
        raise BackupError(f"Invalid backup database: {error}") from error
    finally:
        conn.close()
    return references


def create_full_backup(database, documents, archive):
    """Snapshot every DB row and copy all originals without filtering card state."""
    database, documents, archive = Path(database).resolve(), Path(documents).resolve(), Path(archive).resolve()
    if not database.is_file() or not documents.is_dir():
        raise BackupError("Source database or documents directory is missing.")
    if archive.exists():
        raise BackupError(f"Archive already exists: {archive}")
    if archive.is_relative_to(documents):
        raise BackupError("Archive destination must be outside the documents directory.")
    with tempfile.TemporaryDirectory(prefix="smartreport-full-backup-") as temporary:
        staging = Path(temporary)
        snapshot = staging / "server_snapshot.db"
        source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)
        destination = sqlite3.connect(snapshot)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        references = _database_files(snapshot)
        originals = staging / "documents"
        originals.mkdir()
        found = set()
        files = []
        for source_file in sorted(documents.rglob("*")):
            if source_file.is_symlink():
                raise BackupError(f"Symbolic links are not supported: {source_file.name}")
            if source_file.is_dir():
                continue
            name = _relative_name(source_file.relative_to(documents).as_posix())
            target = originals / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, target)
            digest = _digest(target)
            if name in references and digest != references[name]:
                raise BackupError(f"Original SHA-256 mismatch: {name}")
            found.add(name)
            files.append({"stored_name": name, "sha256": digest, "bytes": target.stat().st_size})
        missing = references.keys() - found
        if missing:
            raise BackupError("Missing originals: " + ", ".join(sorted(missing)))
        manifest = {
            "format": "smartreport-full-backup", "version": 1, "complete": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database_sha256": _digest(snapshot), "files": files, "problems": [],
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        archive.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation never overwrites a previously downloaded backup.
        with archive.open("xb") as output:
            try:
                with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
                    bundle.write(snapshot, "server_snapshot.db")
                    bundle.write(staging / "manifest.json", "manifest.json")
                    for item in files:
                        bundle.write(originals / item["stored_name"], "documents/" + item["stored_name"])
            except Exception:
                output.close()
                archive.unlink()
                raise
        return manifest


def restore_backup(archive, destination):
    """Verify the entire archive before publishing an offline restored directory."""
    archive, destination = Path(archive).resolve(), Path(destination).absolute()
    if destination.is_symlink() or (destination.exists() and (
        not destination.is_dir() or any(destination.iterdir())
    )):
        raise BackupError("Restore destination must be a new or empty directory.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="smartreport-restore-", dir=destination.parent) as temporary:
            staging = Path(temporary) / "verified"
            staging.mkdir()
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                if len(names) != len(set(names)):
                    raise BackupError("Duplicate archive entries.")
                for name in names:
                    _relative_name(name)
                if "manifest.json" not in names or "server_snapshot.db" not in names:
                    raise BackupError("Archive is missing its manifest or database.")
                manifest = json.loads(bundle.read("manifest.json"))
                if (not isinstance(manifest, dict)
                        or manifest.get("format") != "smartreport-full-backup"
                        or manifest.get("version") != 1 or manifest.get("complete") is not True
                        or manifest.get("problems") != [] or not isinstance(manifest.get("files"), list)):
                    raise BackupError("Unsupported or incomplete backup manifest.")
                expected_names = {"manifest.json", "server_snapshot.db"}
                entries = {}
                for item in manifest["files"]:
                    if not isinstance(item, dict):
                        raise BackupError("Invalid manifest file entry.")
                    name = _relative_name(item.get("stored_name"))
                    if name in entries:
                        raise BackupError(f"Duplicate manifest file: {name}")
                    entries[name] = item
                    expected_names.add("documents/" + name)
                if set(names) != expected_names:
                    raise BackupError("Archive file list does not match the manifest.")
                for name in names:
                    path = staging / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(name) as source, path.open("xb") as output:
                        shutil.copyfileobj(source, output)
            database = staging / "server_snapshot.db"
            if _digest(database) != manifest.get("database_sha256"):
                raise BackupError("Database SHA-256 mismatch.")
            references = _database_files(database)
            missing = references.keys() - entries.keys()
            if missing:
                raise BackupError("Manifest omits database originals: " + ", ".join(sorted(missing)))
            for name, item in entries.items():
                path = staging / "documents" / name
                digest = _digest(path)
                if digest != item.get("sha256") or (
                    name in references and digest != references[name]
                ):
                    raise BackupError(f"Original SHA-256 mismatch: {name}")
                if path.stat().st_size != item.get("bytes"):
                    raise BackupError(f"Original size mismatch: {name}")
            (staging / "documents").mkdir(exist_ok=True)
            if destination.exists():
                destination.rmdir()  # Refuses if the destination became non-empty.
            staging.rename(destination)
            return manifest
    except (zipfile.BadZipFile, json.JSONDecodeError) as error:
        raise BackupError(f"Invalid backup archive: {error}") from error


def pull_backup(url, token, destination):
    """Download and verify a single full snapshot; never fall back to partial exports."""
    target = Path(destination) / (
        datetime.now(timezone.utc).strftime("server_backup_%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="smartreport-download-", dir=target.parent) as temporary:
        archive = Path(temporary) / "backup.zip"
        with requests.get(
            f"{url.rstrip('/')}/api/admin/backup/full",
            headers={"Authorization": f"Bearer {token}"}, timeout=120, stream=True,
        ) as response:
            if response.status_code == 409:
                raise BackupError(f"Server could not create a complete backup: {response.json().get('error')}")
            response.raise_for_status()
            with archive.open("xb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    output.write(chunk)
        manifest = restore_backup(archive, target)
        shutil.copyfile(archive, target / "backup.zip")
    return target, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore", type=Path, required=True, metavar="BACKUP_ZIP")
    parser.add_argument("--destination", type=Path, required=True, metavar="EMPTY_DIRECTORY")
    options = parser.parse_args()
    try:
        manifest = restore_backup(options.restore, options.destination)
    except (BackupError, OSError) as error:
        parser.exit(1, f"Restore failed: {error}\n")
    print(f"Verified offline restore: {options.destination}; originals: {len(manifest['files'])}")


if __name__ == "__main__":
    main()
