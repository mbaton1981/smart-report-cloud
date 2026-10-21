"""Prepare new paired snapshots; never open a live database for writing."""
import argparse
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from sync_dataset import dataset_id

LOCAL_CLEAR = (
    "supplier_document_payments", "supplier_document_vat_lines", "supplier_document_allocations",
    "supplier_documents", "ob_segments", "shifts", "balance", "company_expenses",
    "shift_sync_outbox", "expense_document_outbox", "shift_sync_state",
)
SERVER_CLEAR = (
    "expense_processing", "expense_document_attachments", "expense_allocations", "expense_documents",
    "ob_segments", "incoming_submissions", "cloud_shifts", "api_tokens", "sync_metadata",
)


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _snapshot(source, destination):
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src, \
            closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Snapshot integrity failed.")
        if dst.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Snapshot has broken foreign keys.")


def prepare(local_source, server_source, output, protected_ids=(1, 2)):
    local_source, server_source, output = (Path(p).resolve() for p in (local_source, server_source, output))
    if not local_source.is_file() or not server_source.is_file() or output.exists():
        raise ValueError("Existing source snapshots and a new output directory are required.")
    if output in (local_source.parent, server_source.parent):
        raise ValueError("Output must be separate from source snapshots.")
    source_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (local_source, server_source)}
    output.mkdir(parents=True)
    epoch = str(uuid.uuid4())
    report = {"dataset_id": epoch, "databases": {}, "production_modified": False}
    for label, source, clear in (("desktop", local_source, LOCAL_CLEAR), ("server", server_source, SERVER_CLEAR)):
        destination = output / (label + ".db")
        _snapshot(source, destination)
        with closing(sqlite3.connect(destination)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            if dataset_id(conn):
                raise ValueError("Source already has a dataset boundary; prepare from the agreed original snapshots.")
            unknown_clear = set(clear) - set(tables)
            if unknown_clear:
                raise ValueError(f"Missing expected tables: {sorted(unknown_clear)}")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone():
                raise ValueError("Triggers need explicit review before preparation.")
            before = {name: conn.execute(f"SELECT * FROM {_quote(name)} ORDER BY rowid").fetchall() for name in tables}
            if label == "server":
                conn.row_factory = sqlite3.Row
                users = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
                admins = [r for r in users if r["role"] == "admin"]
                owner = [r for r in users if r["username"] == "Aliaksei Patonich"]
                if (len(admins) != 1 or len(owner) != 1 or admins[0]["id"] != protected_ids[0]
                        or owner[0]["id"] != protected_ids[1] or not all(r["password_hash"] for r in [*admins, *owner])):
                    raise ValueError("Protected accounts are ambiguous or lack a PIN; ask the owner.")
                conn.row_factory = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                for name in clear:
                    conn.execute(f"DELETE FROM {_quote(name)}")
                if label == "server":
                    conn.execute("UPDATE users SET password_hash=NULL WHERE id NOT IN (?,?)", protected_ids)
                    after = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
                    columns = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
                    pin_index, id_index = columns.index("password_hash"), columns.index("id")
                    for old, new in zip(before["users"], after, strict=True):
                        if old[id_index] in protected_ids:
                            if old != new:
                                raise ValueError("Protected account changed.")
                        elif new[pin_index] is not None or old[:pin_index] + old[pin_index + 1:] != new[:pin_index] + new[pin_index + 1:]:
                            raise ValueError("User identity/permissions changed.")
                    report["users"] = {
                        "count": len(users), "protected_ids": list(protected_ids),
                        "pins_unset": len(users) - len(protected_ids),
                        "roles_activity_links_unchanged": True, "protected_hashes_unchanged": True,
                    }
                for name in tables:
                    if name not in clear and not (label == "server" and name == "users"):
                        if conn.execute(f"SELECT * FROM {_quote(name)} ORDER BY rowid").fetchall() != before[name]:
                            raise ValueError(f"Reference table changed: {name}")
                conn.execute("CREATE TABLE sync_dataset(id INTEGER PRIMARY KEY CHECK(id=1), dataset_id TEXT NOT NULL)")
                conn.execute("INSERT INTO sync_dataset VALUES (1,?)", (epoch,))
                if conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("Cleanup broke foreign keys.")
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Cleanup broke integrity.")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            report["databases"][label] = {
                "path": str(destination), "cleared": {n: len(before[n]) for n in clear},
                "preserved": {n: len(before[n]) for n in tables if n not in clear},
            }
        report["databases"][label]["sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
    for source, digest in source_hashes.items():
        if hashlib.sha256(Path(source).read_bytes()).hexdigest() != digest:
            raise ValueError("Source snapshot changed.")
    report["source_snapshots_unchanged"] = True
    (output / "preparation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-snapshot", required=True, type=Path)
    parser.add_argument("--server-snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.local_snapshot, args.server_snapshot, args.output), indent=2))
