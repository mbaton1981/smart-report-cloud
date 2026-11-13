import hashlib
import io
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
import unittest

from pypdf import PdfWriter

from portable_recovery import (
    RecoveryError, decrypt, encrypt, publish, restore, safe_name,
    snapshot_local_profile, verify_local_profile, verify_server_documents,
)


def make_pdf():
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(stream)
    return stream.getvalue()


class PortableRecoveryTests(unittest.TestCase):
    password = "synthetic-password-for-tests-only"
    files = {"backup/test.db": b"synthetic data", "recovery/key": b"synthetic key"}

    def test_roundtrip_no_profile_or_dpapi(self):
        self.assertEqual(decrypt(encrypt(self.files, self.password), self.password), self.files)

    def test_wrong_password_and_tampering_rejected(self):
        data = encrypt(self.files, self.password)
        for payload, password in (
            (data, "another-synthetic-password"), (data[:-1] + bytes([data[-1] ^ 1]), self.password),
            (data[:30], self.password), (b"unknown!" + data[8:], self.password),
        ):
            with self.subTest(length=len(payload)), self.assertRaises(RecoveryError):
                decrypt(payload, password)

    def test_password_minimum(self):
        with self.assertRaises(RecoveryError):
            encrypt(self.files, "short")

    def test_paths_rejected(self):
        for name in ("../key", "/key", "C:/key", "a\\key", "a//b", "./key", ""):
            with self.subTest(name=name), self.assertRaises(RecoveryError):
                safe_name(name)

    def test_new_versions_never_overwrite_old_even_after_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            first = publish(temp, self.files, self.password)
            original = first.read_bytes()
            second = publish(temp, self.files, self.password)
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), original)
            with self.assertRaises(RecoveryError):
                publish(temp, self.files, "short")
            self.assertEqual(len(list(Path(temp).glob("*.srbackup"))), 2)

    def test_restore_only_into_new_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "new"
            restore(self.files, destination)
            self.assertEqual((destination / "recovery" / "key").read_bytes(), b"synthetic key")
            with self.assertRaises(FileExistsError):
                restore(self.files, destination)

    def test_randomized_encryption_and_no_plaintext(self):
        first = encrypt(self.files, self.password)
        self.assertNotEqual(first, encrypt(self.files, self.password))
        self.assertNotIn(b"synthetic key", first)
        self.assertNotIn(b"recovery/key", first)

    def test_local_profile_snapshot_restores_wal_drafts_originals_and_revisions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profile-root"
            incoming = root / "profile" / "incoming-documents" / "review"
            incoming.mkdir(parents=True)
            main_db = root / "smart_report.db"
            main_connection = sqlite3.connect(main_db)
            try:
                main_connection.execute("PRAGMA journal_mode=WAL")
                main_connection.execute("PRAGMA wal_autocheckpoint=0")
                main_connection.execute("CREATE TABLE shifts(id INTEGER PRIMARY KEY, hours REAL)")
                main_connection.execute("INSERT INTO shifts VALUES(227, 8)")
                main_connection.commit()
                self.assertTrue(Path(str(main_db) + "-wal").exists())

                pdf_bytes = make_pdf()
                digest = hashlib.sha256(pdf_bytes).hexdigest()
                review_db = incoming / "review.sqlite3"
                with closing(sqlite3.connect(review_db)) as connection:
                    connection.executescript("""
                        CREATE TABLE documents(id INTEGER PRIMARY KEY, sha256 TEXT);
                        CREATE TABLE originals(sha256 TEXT PRIMARY KEY, filename TEXT, content BLOB);
                        CREATE TABLE revisions(id INTEGER PRIMARY KEY, payload TEXT);
                    """)
                    connection.execute("INSERT INTO documents VALUES(1, ?)", (digest,))
                    connection.execute("INSERT INTO originals VALUES(?, 'receipt.pdf', ?)",
                                       (digest, pdf_bytes))
                    connection.execute("INSERT INTO revisions VALUES(1, '{\"manual\": true}')")
                    connection.commit()
                (incoming / "receipt.pdf").write_bytes(pdf_bytes)
                (incoming / "review.json").write_text('{"manual": true}', encoding="utf-8")

                files = snapshot_local_profile(root)
            finally:
                main_connection.close()
            self.assertIn("profile/smart_report.db", files)
            self.assertIn("profile/incoming-documents/review/review.sqlite3", files)
            self.assertIn("profile/incoming-documents/review/receipt.pdf", files)
            self.assertIn("profile/incoming-documents/review/review.json", files)
            self.assertFalse(any("token" in name.casefold() or "password" in name.casefold()
                                 for name in files))

            restored = Path(temp) / "restored"
            restore(files, restored)
            with closing(sqlite3.connect(restored / "profile" / "smart_report.db")) as connection:
                self.assertEqual(connection.execute("SELECT hours FROM shifts WHERE id=227").fetchone()[0], 8)
            result = verify_local_profile(restored)
            self.assertTrue(result["integrity"])
            self.assertTrue(result["links_consistent"])
            self.assertEqual(result["incoming_reviews"]["profile/incoming-documents/review/review.sqlite3"]
                             ["revisions"], 1)
            self.assertEqual(result["incoming_pdf_files"], 1)
            self.assertEqual(result["incoming_pdfs_readable"], 1)
            self.assertEqual(result["incoming_pdf_pages"], 1)

    def test_local_profile_snapshot_rejects_credentials_and_detects_changed_pdf(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profile-root"
            root.mkdir()
            database = sqlite3.connect(root / "smart_report.db")
            database.execute("CREATE TABLE auth(access_token TEXT)")
            database.commit()
            database.close()
            with self.assertRaises(RecoveryError):
                snapshot_local_profile(root)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profile-root"
            incoming = root / "profile" / "incoming-documents"
            incoming.mkdir(parents=True)
            database = sqlite3.connect(root / "smart_report.db")
            database.execute("CREATE TABLE drafts(id INTEGER PRIMARY KEY)")
            database.commit()
            database.close()
            pdf_path = incoming / "original.pdf"
            pdf_path.write_bytes(make_pdf())
            files = snapshot_local_profile(root)
            restored = Path(temp) / "restored"
            restore(files, restored)
            (restored / "profile" / "incoming-documents" / "original.pdf").write_bytes(b"changed")
            with self.assertRaises(RecoveryError):
                verify_local_profile(restored)

    def test_restored_server_originals_are_checked_against_database_and_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backup = root / "backup"
            documents = backup / "documents"
            documents.mkdir(parents=True)
            content = make_pdf()
            filename = "9_3_invoice.pdf"
            (documents / filename).write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            with closing(sqlite3.connect(backup / "server_snapshot.db")) as connection:
                connection.executescript("""
                    CREATE TABLE expense_document_attachments(
                        id INTEGER PRIMARY KEY, document_id INTEGER, original_name TEXT, file_sha256 TEXT
                    );
                    CREATE TABLE expense_documents(
                        id INTEGER PRIMARY KEY, original_name TEXT, file_sha256 TEXT, stored_name TEXT
                    );
                """)
                connection.execute(
                    "INSERT INTO expense_document_attachments VALUES(3, 9, 'invoice.pdf', ?)",
                    (digest,),
                )
                connection.commit()
            snapshot_path = backup / "server_snapshot.db"
            snapshot_bytes = snapshot_path.read_bytes()
            snapshot_hash = hashlib.sha256(snapshot_bytes).hexdigest()
            (backup / "manifest.json").write_text(json.dumps({
                "status": "complete",
                "expected_originals": 1,
                "snapshot_sha256": snapshot_hash,
                "files": [{
                    "document_id": 9, "file": filename, "sha256": digest,
                    "matches_server_record": True,
                }],
            }), encoding="utf-8")

            result = verify_server_documents(root)
            self.assertTrue(result["verified"])
            self.assertEqual(result["files"], 1)
            self.assertEqual(result["sha256_matches"], 1)
            self.assertEqual(result["links_match"], True)
            self.assertEqual(result["pdfs_readable"], 1)

            snapshot_path.write_bytes(b"damaged database snapshot")
            with self.assertRaises(RecoveryError):
                verify_server_documents(root)
            snapshot_path.write_bytes(snapshot_bytes)
            (documents / filename).write_bytes(b"damaged")
            with self.assertRaises(RecoveryError):
                verify_server_documents(root)


if __name__ == "__main__":
    unittest.main()
