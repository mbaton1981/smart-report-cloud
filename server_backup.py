"""Копия данных сервера на этот компьютер: снимок базы и все оригиналы с проверкой SHA-256."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import requests


def pull_backup(url, token, destination):
    headers = {"Authorization": f"Bearer {token}"}
    base = url.rstrip("/")
    target = Path(destination) / datetime.now(timezone.utc).strftime("server_backup_%Y%m%dT%H%M%SZ")
    (target / "documents").mkdir(parents=True)
    with requests.get(f"{base}/api/admin/backup/database", headers=headers, timeout=120, stream=True) as response:
        response.raise_for_status()
        with (target / "server_snapshot.db").open("wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                output.write(chunk)
    listing = requests.get(f"{base}/api/expense-documents", headers=headers, timeout=60)
    listing.raise_for_status()
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "server": base, "files": [], "problems": []}
    for document in listing.json()["documents"]:
        for attachment in document.get("attachments") or []:
            route = (f"/api/expense-documents/{document['id']}/attachments/{attachment['id']}/original"
                     if attachment.get("id") else f"/api/expense-documents/{document['id']}/original")
            name = f"{document['id']}_{attachment.get('id') or 0}_{Path(attachment.get('original_name') or 'document').name}"
            path = target / "documents" / name
            digest = hashlib.sha256()
            try:
                with requests.get(base + route, headers=headers, timeout=120, stream=True) as response:
                    response.raise_for_status()
                    with path.open("wb") as output:
                        for chunk in response.iter_content(1024 * 1024):
                            digest.update(chunk)
                            output.write(chunk)
            except requests.RequestException as error:
                manifest["problems"].append({"document_id": document["id"], "file": name, "error": str(error)})
                continue
            matches = digest.hexdigest() == attachment.get("sha256")
            manifest["files"].append({"document_id": document["id"], "file": name, "sha256": digest.hexdigest(), "matches_server_record": matches})
            if not matches:
                manifest["problems"].append({"document_id": document["id"], "file": name, "error": "SHA-256 не совпал"})
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return target, manifest
