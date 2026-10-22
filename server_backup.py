"""Проверенный снимок сервера и оригиналы именно из этого снимка, без изменения данных."""
from contextlib import closing
import hashlib
import json
import logging
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests

from sync_dataset import HEADER, dataset_id, require_response_dataset


def pull_backup(url, token, destination, *, expected_dataset=None):
    headers = {"Authorization": f"Bearer {token}"}
    if expected_dataset:
        headers[HEADER] = expected_dataset
    base = url.rstrip("/")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    target = Path(tempfile.mkdtemp(
        prefix=datetime.now(timezone.utc).strftime("server_backup_%Y%m%dT%H%M%SZ_"),
        dir=destination,
    ))
    (target / "documents").mkdir()
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(), "server": base,
        "status": "incomplete", "dataset_id": expected_dataset, "files": [], "problems": [],
    }

    def save_manifest():
        temporary = target / "manifest.json.tmp"
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target / "manifest.json")

    save_manifest()
    try:
        snapshot = target / "server_snapshot.db"
        with requests.get(f"{base}/api/admin/backup/database", headers=dict(headers),
                          timeout=120, stream=True) as response:
            response.raise_for_status()
            require_response_dataset(response, expected_dataset)
            response_dataset = response.headers.get(HEADER)
            with snapshot.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    output.write(chunk)
        with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("Снимок базы не прошёл проверку целостности.")
            snapshot_dataset = dataset_id(connection)
            if snapshot_dataset != response_dataset or (
                expected_dataset and snapshot_dataset != expected_dataset
            ):
                raise ValueError("Набор данных снимка не соответствует серверу или рабочему профилю.")
            connection.row_factory = sqlite3.Row
            # The downloaded snapshot, not a later live listing, defines required originals.
            originals = [dict(row) for row in connection.execute("""
                SELECT document_id, id, original_name, file_sha256 FROM expense_document_attachments
                UNION ALL
                SELECT d.id, NULL, d.original_name, d.file_sha256 FROM expense_documents d
                WHERE d.stored_name IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM expense_document_attachments a WHERE a.document_id=d.id
                )
                ORDER BY 1, 2
            """)]
        manifest["dataset_id"] = snapshot_dataset
        manifest["snapshot_sha256"] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        manifest["expected_originals"] = len(originals)
        if snapshot_dataset:
            headers[HEADER] = snapshot_dataset
        for attachment in originals:
            document_id, attachment_id = attachment["document_id"], attachment["id"]
            route = (f"/api/expense-documents/{document_id}/attachments/{attachment_id}/original"
                     if attachment_id else f"/api/expense-documents/{document_id}/original")
            original_name = Path((attachment["original_name"] or "document").replace("\\", "/")).name
            name = f"{document_id}_{attachment_id or 0}_{original_name}"
            path = target / "documents" / name
            digest = hashlib.sha256()
            try:
                with requests.get(base + route, headers=dict(headers), timeout=120, stream=True) as response:
                    response.raise_for_status()
                    require_response_dataset(response, snapshot_dataset)
                    with path.open("wb") as output:
                        for chunk in response.iter_content(1024 * 1024):
                            digest.update(chunk)
                            output.write(chunk)
            except (requests.RequestException, RuntimeError, OSError) as error:
                logging.warning("Не удалось сохранить оригинал документа %s: %s", document_id, error)
                manifest["problems"].append({"document_id": document_id, "file": name, "error": str(error)})
                save_manifest()
                continue
            matches = digest.hexdigest() == attachment["file_sha256"]
            manifest["files"].append({
                "document_id": document_id, "file": name, "sha256": digest.hexdigest(),
                "matches_server_record": matches,
            })
            if not matches:
                logging.warning("SHA-256 оригинала документа %s не совпал со снимком", document_id)
                manifest["problems"].append({
                    "document_id": document_id, "file": name, "error": "SHA-256 не совпал со снимком",
                })
            save_manifest()
        if not manifest["problems"]:
            manifest["status"] = "complete"
        save_manifest()
    except (requests.RequestException, sqlite3.Error, OSError, ValueError, RuntimeError) as error:
        manifest["status"] = "failed"
        manifest["problems"].append({"error": str(error)})
        save_manifest()
        logging.exception("Серверная резервная копия не завершена: %s", target)
        raise
    return target, manifest
