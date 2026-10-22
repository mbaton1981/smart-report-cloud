from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

import desktop_auth
import server_backup
from sync_dataset import HEADER


class FrozenProfileTests(unittest.TestCase):
    def test_unconfigured_frozen_executable_rejects_before_creating_default_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install = root / "Programs" / "SmartReport"
            install.mkdir(parents=True)
            original = Path.cwd()
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "executable", str(install / "SmartReport.exe")), \
                    patch.object(sys, "argv", ["SmartReport.exe"]), \
                    patch.object(desktop_auth, "app_data_dir") as default:
                with self.assertRaisesRegex(desktop_auth.AuthError, "ярлык"):
                    desktop_auth.use_data_dir_as_working_directory()
                default.assert_not_called()
                self.assertEqual(Path.cwd(), original)

    def test_explicit_launcher_profile_and_dataset_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "smart_report.db"
            with closing(sqlite3.connect(database)) as conn:
                conn.execute("CREATE TABLE sync_dataset(id INTEGER PRIMARY KEY,dataset_id TEXT)")
                conn.execute("INSERT INTO sync_dataset VALUES(1,'approved')")
                conn.commit()
            env = {
                "SMARTREPORT_DB_FILE": str(database),
                "SMARTREPORT_DATA_DIR": str(root / "profile"),
                "SMARTREPORT_EXPECTED_DATASET": "approved",
            }
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "executable", str(root / "SmartReport.exe")), \
                    patch.object(sys, "argv", ["SmartReport.exe"]):
                self.assertFalse(desktop_auth.use_data_dir_as_working_directory())
                self.assertEqual(os.environ["SMARTREPORT_DB_FILE"], str(database))
                self.assertEqual(os.environ["SMARTREPORT_DATA_DIR"], str(root / "profile"))
                os.environ["SMARTREPORT_EXPECTED_DATASET"] = "wrong"
                with self.assertRaises(desktop_auth.AuthError):
                    desktop_auth.use_data_dir_as_working_directory()

    def test_diagnostic_without_explicit_database_does_not_open_default(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "argv", ["SmartReport.exe", "--diagnostic", "selftest"]), \
                patch.object(desktop_auth, "app_data_dir") as default:
            with self.assertRaises(desktop_auth.AuthError):
                desktop_auth.use_data_dir_as_working_directory()
            default.assert_not_called()

    def test_non_frozen_development_start_is_not_redirected(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(sys, "frozen", False, create=True):
            self.assertFalse(desktop_auth.use_data_dir_as_working_directory())

    def test_prepared_paired_install_still_validates_and_selects_its_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install = root / "Programs" / "SmartReport-paired"
            install.mkdir(parents=True)
            (install / "paired_install.json").write_text("{}", encoding="utf-8")
            data = root / install.name
            data.mkdir()
            database = data / "smart_report.db"
            with closing(sqlite3.connect(database)) as conn:
                conn.executescript("""
                    CREATE TABLE sync_dataset(id INTEGER PRIMARY KEY,dataset_id TEXT);
                    INSERT INTO sync_dataset VALUES(1,'approved');
                """)
                conn.commit()
            marker = data / "working_profile.json"
            marker.write_text(json.dumps({
                "approved_transition": True, "dataset_id": "approved",
                "server_url": desktop_auth.DEFAULT_SERVER_URL,
            }), encoding="utf-8")
            with patch.dict(os.environ, {"LOCALAPPDATA": str(root)}, clear=True), \
                    patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "argv", ["SmartReport.exe"]), \
                    patch.object(sys, "executable", str(install / "SmartReport.exe")), \
                    patch.object(desktop_auth.os, "chdir") as chdir:
                self.assertTrue(desktop_auth.use_data_dir_as_working_directory())
                self.assertEqual(os.environ["SMARTREPORT_DB_FILE"], str(database))
                chdir.assert_called_once_with(data)


class ServerBackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.original = b"%PDF isolated invoice"
        self.snapshot = self.root / "source.db"
        with closing(sqlite3.connect(self.snapshot)) as conn:
            conn.executescript("""
                CREATE TABLE sync_dataset(id INTEGER PRIMARY KEY,dataset_id TEXT);
                INSERT INTO sync_dataset VALUES(1,'approved');
                CREATE TABLE expense_documents(id INTEGER PRIMARY KEY,original_name TEXT,
                    file_sha256 TEXT,stored_name TEXT);
                CREATE TABLE expense_document_attachments(id INTEGER PRIMARY KEY,document_id INTEGER,
                    original_name TEXT,file_sha256 TEXT);
            """)
            conn.execute("INSERT INTO expense_documents VALUES(1,NULL,NULL,NULL)")
            conn.execute("INSERT INTO expense_document_attachments VALUES(11,1,'invoice.pdf',?)",
                         (hashlib.sha256(self.original).hexdigest(),))
            conn.commit()

    def response(self, data, *, dataset="approved"):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers = {HEADER: dataset} if dataset else {}
        response.iter_content.return_value = [data]
        response.raise_for_status.return_value = None
        return response

    def run_backup(self, responses, **kwargs):
        with patch.object(server_backup.requests, "get", side_effect=responses) as get:
            result = server_backup.pull_backup("https://example.invalid", "test-token", self.root / "output", **kwargs)
            return result, get.call_args_list

    def test_snapshot_inventory_and_dataset_headers_produce_verified_complete_backup(self):
        (target, manifest), calls = self.run_backup([
            self.response(self.snapshot.read_bytes()), self.response(self.original),
        ], expected_dataset="approved")
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["problems"], [])
        self.assertEqual(len(manifest["files"]), 1)
        self.assertTrue(manifest["files"][0]["matches_server_record"])
        self.assertEqual((target / "documents" / manifest["files"][0]["file"]).read_bytes(), self.original)
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call.kwargs["headers"][HEADER], "approved")
        self.assertIn("/attachments/11/original", calls[1].args[0])
        self.assertEqual(json.loads((target / "manifest.json").read_text(encoding="utf-8"))["status"], "complete")

    def test_dataset_discovered_from_snapshot_is_sent_to_original_requests(self):
        (target, manifest), calls = self.run_backup([
            self.response(self.snapshot.read_bytes()), self.response(self.original),
        ])
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(calls[-1].kwargs["headers"][HEADER], "approved")

    def test_missing_file_and_corrupt_hash_are_explicit_incomplete_backups(self):
        for response in (self.response(b"corrupted"), self.response(b"")):
            with self.subTest(response=response):
                if response.iter_content.return_value == [b""]:
                    response.raise_for_status.side_effect = requests.HTTPError("HTTP 404")
                (target, manifest), _ = self.run_backup([
                    self.response(self.snapshot.read_bytes()), response,
                ])
                self.assertEqual(manifest["status"], "incomplete")
                self.assertTrue(manifest["problems"])
                self.assertNotEqual(target, self.root)

    def test_snapshot_failure_persists_failed_manifest_and_raises(self):
        for response in (self.response(b"not sqlite"), self.response(self.snapshot.read_bytes(), dataset="wrong")):
            with self.subTest(response=response):
                with patch.object(server_backup.requests, "get", return_value=response):
                    with self.assertRaises((sqlite3.DatabaseError, RuntimeError, ValueError)):
                        server_backup.pull_backup("https://example.invalid", "test-token",
                                                  self.root / "failed", expected_dataset="approved")
        manifests = list((self.root / "failed").rglob("manifest.json"))
        self.assertEqual(len(manifests), 2)
        for path in manifests:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertTrue(manifest["problems"])

    def test_changed_dataset_on_attachment_is_not_accepted(self):
        (target, manifest), _ = self.run_backup([
            self.response(self.snapshot.read_bytes()), self.response(self.original, dataset="different"),
        ])
        self.assertEqual(manifest["status"], "incomplete")
        self.assertTrue(manifest["problems"])
        self.assertEqual(manifest["files"], [])

    def test_empty_and_legacy_dataset_snapshots_are_supported(self):
        with closing(sqlite3.connect(self.snapshot)) as conn:
            conn.execute("DELETE FROM expense_document_attachments")
            conn.execute("DROP TABLE sync_dataset")
            conn.commit()
        (target, manifest), calls = self.run_backup([self.response(self.snapshot.read_bytes(), dataset=None)])
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["expected_originals"], 0)
        self.assertEqual(manifest["files"], [])
        self.assertIsNone(manifest["dataset_id"])
        self.assertEqual(len(calls), 1)

    def test_snapshot_body_must_match_its_dataset_header(self):
        with closing(sqlite3.connect(self.snapshot)) as conn:
            conn.execute("UPDATE sync_dataset SET dataset_id='different'")
            conn.commit()
        with patch.object(server_backup.requests, "get", return_value=self.response(self.snapshot.read_bytes())):
            with self.assertRaisesRegex(ValueError, "снимка"):
                server_backup.pull_backup("https://example.invalid", "test-token", self.root / "failed")

    def test_http_error_before_snapshot_is_marked_failed_without_secret(self):
        with patch.object(server_backup.requests, "get", side_effect=requests.ConnectionError("offline")):
            with self.assertRaises(requests.ConnectionError):
                server_backup.pull_backup("https://example.invalid", "test-token", self.root / "failed")
        path = next((self.root / "failed").rglob("manifest.json"))
        content = path.read_text(encoding="utf-8")
        self.assertEqual(json.loads(content)["status"], "failed")
        self.assertNotIn("test-token", content)


if __name__ == "__main__":
    unittest.main()
