import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import requests

import server_backup


class FullBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="smartreport-full-backup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "source.db"
        self.documents = self.root / "originals"
        self.documents.mkdir()
        self.archive = self.root / "backup.zip"
        self.destination = self.root / "restored"
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.executescript("""
                CREATE TABLE expense_documents (
                    id INTEGER PRIMARY KEY, is_deleted INTEGER NOT NULL,
                    stored_name TEXT, file_sha256 TEXT
                );
                CREATE TABLE expense_document_attachments (
                    id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL
                        REFERENCES expense_documents(id),
                    stored_name TEXT NOT NULL, file_sha256 TEXT NOT NULL
                );
            """)
            for identifier, deleted in ((1, 0), (2, 1), (3, 0)):
                name = f"stored-{identifier}.pdf"
                content = f"%PDF fixture {identifier}".encode()
                digest = hashlib.sha256(content).hexdigest()
                (self.documents / name).write_bytes(content)
                conn.execute("INSERT INTO expense_documents VALUES (?, ?, ?, ?)",
                             (identifier, deleted, name, digest))
                if identifier != 3:
                    conn.execute("INSERT INTO expense_document_attachments VALUES (?, ?, ?, ?)",
                                 (identifier, identifier, name, digest))
        (self.documents / "unlinked-original.jpg").write_bytes(b"unlinked original")

    def build(self):
        return server_backup.create_full_backup(self.database, self.documents, self.archive)

    def rewrite_archive(self, change):
        with zipfile.ZipFile(self.archive) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        change(contents)
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, content in contents.items():
                archive.writestr(name, content)

    def test_restore_keeps_deleted_legacy_links_and_unlinked_files(self):
        manifest = self.build()
        self.assertTrue(manifest["complete"])
        self.assertEqual(len(manifest["files"]), 4)
        self.destination.mkdir()
        restored = server_backup.restore_backup(self.archive, self.destination)
        self.assertTrue(restored["complete"])
        with closing(sqlite3.connect(self.destination / "server_snapshot.db")) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            rows = conn.execute(
                "SELECT id,is_deleted,stored_name,file_sha256 FROM expense_documents ORDER BY id"
            ).fetchall()
            self.assertEqual([row[1] for row in rows], [0, 1, 0])
            self.assertEqual(conn.execute(
                "SELECT id,document_id FROM expense_document_attachments ORDER BY id"
            ).fetchall(), [(1, 1), (2, 2)])
        for _, _, name, digest in rows:
            self.assertEqual(hashlib.sha256(
                (self.destination / "documents" / name).read_bytes()
            ).hexdigest(), digest)
        self.assertEqual((self.destination / "documents" / "unlinked-original.jpg").read_bytes(),
                         b"unlinked original")

    def test_missing_source_original_fails_without_archive(self):
        (self.documents / "stored-2.pdf").unlink()
        with self.assertRaisesRegex(server_backup.BackupError, "stored-2.pdf"):
            self.build()
        self.assertFalse(self.archive.exists())

    def test_corrupt_source_original_fails_without_archive(self):
        (self.documents / "stored-2.pdf").write_bytes(b"damaged")
        with self.assertRaisesRegex(server_backup.BackupError, "SHA-256"):
            self.build()
        self.assertFalse(self.archive.exists())

    def test_missing_documents_directory_is_not_empty_success(self):
        with self.assertRaises(server_backup.BackupError):
            server_backup.create_full_backup(
                self.database, self.root / "absent", self.archive,
            )
        self.assertFalse(self.archive.exists())

    def test_missing_archived_original_does_not_restore_database(self):
        self.build()
        self.rewrite_archive(lambda contents: contents.pop("documents/stored-2.pdf"))
        with self.assertRaises(server_backup.BackupError):
            server_backup.restore_backup(self.archive, self.destination)
        self.assertFalse(self.destination.exists())

    def test_corrupt_archived_original_does_not_restore_database(self):
        self.build()
        self.rewrite_archive(lambda contents: contents.update(
            {"documents/stored-2.pdf": b"damaged"},
        ))
        with self.assertRaisesRegex(server_backup.BackupError, "SHA-256"):
            server_backup.restore_backup(self.archive, self.destination)
        self.assertFalse(self.destination.exists())

    def test_corrupt_database_is_rejected(self):
        self.build()
        self.rewrite_archive(lambda contents: contents.update(
            {"server_snapshot.db": b"not SQLite"},
        ))
        with self.assertRaises(server_backup.BackupError):
            server_backup.restore_backup(self.archive, self.destination)
        self.assertFalse(self.destination.exists())

    def test_manifest_cannot_omit_database_referenced_original(self):
        self.build()

        def omit(contents):
            manifest = json.loads(contents["manifest.json"])
            manifest["files"] = [item for item in manifest["files"]
                                 if item["stored_name"] != "stored-2.pdf"]
            contents["manifest.json"] = json.dumps(manifest).encode()
            del contents["documents/stored-2.pdf"]

        self.rewrite_archive(omit)
        with self.assertRaisesRegex(server_backup.BackupError, "stored-2.pdf"):
            server_backup.restore_backup(self.archive, self.destination)
        self.assertFalse(self.destination.exists())

    def test_existing_destination_data_and_archive_are_never_overwritten(self):
        self.build()
        before = self.archive.read_bytes()
        with self.assertRaises(server_backup.BackupError):
            self.build()
        self.assertEqual(self.archive.read_bytes(), before)
        self.destination.mkdir()
        sentinel = self.destination / "keep.db"
        sentinel.write_bytes(b"existing database")
        with self.assertRaises(server_backup.BackupError):
            server_backup.restore_backup(self.archive, self.destination)
        self.assertEqual(sentinel.read_bytes(), b"existing database")

    def test_path_traversal_archive_is_rejected(self):
        self.build()
        self.rewrite_archive(lambda contents: contents.update({"../outside.pdf": b"bad"}))
        with self.assertRaises(server_backup.BackupError):
            server_backup.restore_backup(self.archive, self.destination)
        self.assertFalse((self.root / "outside.pdf").exists())

    def test_manifest_incomplete_flag_is_rejected(self):
        self.build()

        def mark_incomplete(contents):
            manifest = json.loads(contents["manifest.json"])
            manifest["complete"] = False
            contents["manifest.json"] = json.dumps(manifest).encode()

        self.rewrite_archive(mark_incomplete)
        with self.assertRaises(server_backup.BackupError):
            server_backup.restore_backup(self.archive, self.destination)

    def test_failed_restore_preserves_existing_empty_directory(self):
        self.build()
        self.destination.mkdir()
        self.rewrite_archive(lambda contents: contents.pop("documents/stored-1.pdf"))
        with self.assertRaises(server_backup.BackupError):
            server_backup.restore_backup(self.archive, self.destination)
        self.assertTrue(self.destination.is_dir())
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_conflicting_legacy_hash_is_rejected(self):
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("UPDATE expense_documents SET file_sha256=? WHERE id=1", ("0" * 64,))
        with self.assertRaisesRegex(server_backup.BackupError, "Conflicting"):
            self.build()
        self.assertFalse(self.archive.exists())

    def test_download_rejects_incomplete_server_response(self):
        with patch.object(server_backup.requests, "get") as get:
            response = get.return_value.__enter__.return_value
            response.status_code = 409
            response.json.return_value = {"error": "Missing originals: stored-2.pdf"}
            with self.assertRaisesRegex(server_backup.BackupError, "stored-2.pdf"):
                server_backup.pull_backup("http://127.0.0.1", "test-only", self.destination)
            self.assertEqual(list(self.destination.iterdir()), [])

    def test_old_server_is_not_silently_backed_up_partially(self):
        with patch.object(server_backup.requests, "get") as get:
            response = get.return_value.__enter__.return_value
            response.status_code = 404
            response.raise_for_status.side_effect = requests.HTTPError("Full backup endpoint unavailable")
            with self.assertRaises(requests.HTTPError):
                server_backup.pull_backup("http://127.0.0.1", "test-only", self.destination)
            self.assertEqual(get.call_count, 1)
            self.assertEqual(list(self.destination.iterdir()), [])

    def test_source_database_and_originals_remain_unchanged(self):
        before_db = self.database.read_bytes()
        before_files = {path.name: path.read_bytes() for path in self.documents.iterdir()}
        self.build()
        server_backup.restore_backup(self.archive, self.destination)
        self.assertEqual(self.database.read_bytes(), before_db)
        self.assertEqual({path.name: path.read_bytes() for path in self.documents.iterdir()},
                         before_files)

    def test_offline_restore_command_reports_failure_and_success(self):
        self.build()
        command = [sys.executable, "-m", "server_backup", "--restore", str(self.archive),
                   "--destination", str(self.destination)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Verified offline restore", result.stdout)
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Restore failed", result.stderr)
        self.assertNotIn("Verified offline restore", result.stdout)


if __name__ == "__main__":
    unittest.main()
