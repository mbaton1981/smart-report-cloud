import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import desktop_auth
from prepare_transition import LOCAL_CLEAR, SERVER_CLEAR, prepare
from sync_dataset import file_dataset


class TransitionPreparationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="smartreport-transition-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.local = self.root / "local_snapshot.db"
        self.server = self.root / "server_snapshot.db"
        for path, operations in ((self.local, LOCAL_CLEAR), (self.server, SERVER_CLEAR)):
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE categories(id INTEGER PRIMARY KEY,name TEXT)")
                conn.execute("INSERT INTO categories VALUES (31,'Preserved category')")
                for table in operations:
                    conn.execute(f'CREATE TABLE "{table}"(id INTEGER PRIMARY KEY, value TEXT)')
                    conn.execute(f'INSERT INTO "{table}" VALUES (82,?)', ("test operation",))
                if path == self.server:
                    conn.execute("""CREATE TABLE users(
                        id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT,
                        role TEXT, employee_id INTEGER, is_active INTEGER)""")
                    conn.executemany("INSERT INTO users VALUES (?,?,?,?,?,?)", [
                        (1, "admin", "synthetic-admin-hash", "admin", None, 1),
                        (2, "Aliaksei Patonich", "synthetic-owner-hash", "user", 11, 1),
                        (3, "Other active user", "synthetic-other-hash", "user", 12, 1),
                        (4, "Inactive user", "synthetic-inactive-hash", "user", 13, 0),
                    ])
                conn.commit()

    def test_pair_clears_only_operations_preserving_accounts_rules_and_source_bytes(self):
        hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.local, self.server)}
        report = prepare(self.local, self.server, self.root / "prepared")
        self.assertTrue(report["source_snapshots_unchanged"])
        self.assertEqual(report["users"]["pins_unset"], 2)
        expected_epoch = None
        for label, operations in (("desktop", LOCAL_CLEAR), ("server", SERVER_CLEAR)):
            target = self.root / "prepared" / (label + ".db")
            epoch = file_dataset(target)
            self.assertTrue(epoch)
            if expected_epoch is None:
                expected_epoch = epoch
            self.assertEqual(expected_epoch, epoch)
            with closing(sqlite3.connect(target)) as conn:
                self.assertEqual(conn.execute("SELECT * FROM categories").fetchall(), [(31, "Preserved category")])
                for table in operations:
                    self.assertEqual(conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0], 0)
                if label == "server":
                    self.assertEqual(conn.execute("SELECT * FROM users ORDER BY id").fetchall(), [
                        (1, "admin", "synthetic-admin-hash", "admin", None, 1),
                        (2, "Aliaksei Patonich", "synthetic-owner-hash", "user", 11, 1),
                        (3, "Other active user", None, "user", 12, 1),
                        (4, "Inactive user", None, "user", 13, 0),
                    ])
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), report["databases"][label]["sha256"])
        for path, digest in hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        saved = json.loads((self.root / "prepared" / "preparation.json").read_text(encoding="utf-8"))
        self.assertNotIn("synthetic-admin-hash", json.dumps(saved))

    def test_ambiguous_protected_identity_stops_without_modifying_sources(self):
        with closing(sqlite3.connect(self.server)) as conn:
            conn.execute("INSERT INTO users VALUES (5,'Aliaksei Patonich','synthetic-duplicate','user',14,1)")
            conn.commit()
        before = self.server.read_bytes()
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            prepare(self.local, self.server, self.root / "ambiguous")
        self.assertEqual(self.server.read_bytes(), before)
        self.assertFalse((self.root / "ambiguous" / "preparation.json").exists())

    def test_wal_snapshot_hash_is_recorded_after_closing_and_checkpointing(self):
        with closing(sqlite3.connect(self.server)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        report = prepare(self.local, self.server, self.root / "wal_prepared")
        target = Path(report["databases"]["server"]["path"])
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), report["databases"]["server"]["sha256"])

    def test_existing_output_is_never_overwritten(self):
        output = self.root / "existing"
        output.mkdir()
        marker = output / "keep.txt"
        marker.write_text("keep", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "new output"):
            prepare(self.local, self.server, output)
        self.assertEqual(marker.read_text(encoding="ascii"), "keep")

    def test_direct_candidate_executable_cannot_open_working_database(self):
        install = self.root / "Programs" / "SmartReport-Transition"
        install.mkdir(parents=True)
        (install / "candidate_install.json").write_text('{"isolated_candidate":true}', encoding="ascii")
        profile = self.root / "SmartReport-Transition"
        profile.mkdir()
        database = profile / "smart_report.db"
        database.touch()
        (profile / "candidate_profile.json").write_text(json.dumps({
            "isolated_candidate": True, "database": str(database),
            "source_database": str(self.local), "release_version": "1.0.9-rc2",
        }), encoding="utf-8")
        with patch.object(desktop_auth.sys, "frozen", True, create=True), \
                patch.object(desktop_auth.sys, "executable", str(install / "SmartReport.exe")), \
                patch.object(desktop_auth.sys, "argv", ["SmartReport.exe"]), \
                patch.dict(os.environ, {"LOCALAPPDATA": str(self.root), "SMARTREPORT_DB_FILE": str(self.local),
                                        "SMARTREPORT_CLOUD_URL": "https://example.invalid", "SYNC_API_KEY": "test-only"}), \
                patch.object(desktop_auth.os, "chdir") as chdir:
            self.assertTrue(desktop_auth.use_data_dir_as_working_directory())
            self.assertEqual(os.environ["SMARTREPORT_DB_FILE"], str(database))
            self.assertTrue(desktop_auth.isolated_candidate_mode())
            self.assertEqual(os.environ["SMARTREPORT_CANDIDATE_VERSION"], "1.0.9-rc2")
            self.assertNotIn("SYNC_API_KEY", os.environ)
            chdir.assert_called_once_with(profile)

    def test_direct_candidate_executable_fails_closed_without_prepared_profile(self):
        install = self.root / "Programs" / "SmartReport-Transition"
        install.mkdir(parents=True)
        (install / "candidate_install.json").touch()
        with patch.object(desktop_auth.sys, "frozen", True, create=True), \
                patch.object(desktop_auth.sys, "executable", str(install / "SmartReport.exe")), \
                patch.object(desktop_auth.sys, "argv", ["SmartReport.exe"]), \
                patch.dict(os.environ, {"LOCALAPPDATA": str(self.root), "SMARTREPORT_DB_FILE": str(self.local)}):
            with self.assertRaisesRegex(desktop_auth.AuthError, "не подготовлен"):
                desktop_auth.use_data_dir_as_working_directory()

    def test_approved_paired_install_pins_database_server_and_requires_login(self):
        install = self.root / "Programs" / "SmartReport-1.0.9"
        install.mkdir(parents=True)
        (install / "paired_install.json").touch()
        profile = self.root / install.name
        profile.mkdir()
        database = profile / "smart_report.db"
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("CREATE TABLE sync_dataset(id INTEGER PRIMARY KEY,dataset_id TEXT)")
            conn.execute("INSERT INTO sync_dataset VALUES (1,'approved-pair')")
            conn.commit()
        marker = profile / "working_profile.json"
        marker.write_text(json.dumps({
            "approved_transition": True, "dataset_id": "approved-pair",
            "server_url": desktop_auth.DEFAULT_SERVER_URL,
        }), encoding="utf-8")
        with patch.object(desktop_auth.sys, "frozen", True, create=True), \
                patch.object(desktop_auth.sys, "executable", str(install / "SmartReport.exe")), \
                patch.object(desktop_auth.sys, "argv", ["SmartReport.exe"]), \
                patch.dict(os.environ, {"LOCALAPPDATA": str(self.root), "SMARTREPORT_TRIAL_MODE": "1",
                                        "SYNC_API_KEY": "test-only", "SMARTREPORT_CANDIDATE_PROFILE": "wrong"}), \
                patch.object(desktop_auth.os, "chdir"):
            self.assertTrue(desktop_auth.use_data_dir_as_working_directory())
            self.assertEqual(os.environ["SMARTREPORT_DB_FILE"], str(database))
            self.assertEqual(os.environ["SMARTREPORT_CLOUD_URL"], desktop_auth.DEFAULT_SERVER_URL)
            for key in ("SYNC_API_KEY", "SMARTREPORT_TRIAL_MODE", "SMARTREPORT_CANDIDATE_PROFILE"):
                self.assertNotIn(key, os.environ)
        marker.write_text(json.dumps({
            "approved_transition": True, "dataset_id": "wrong-pair",
            "server_url": desktop_auth.DEFAULT_SERVER_URL,
        }), encoding="utf-8")
        with patch.object(desktop_auth.sys, "frozen", True, create=True), \
                patch.object(desktop_auth.sys, "executable", str(install / "SmartReport.exe")), \
                patch.object(desktop_auth.sys, "argv", ["SmartReport.exe"]), \
                patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            with self.assertRaisesRegex(desktop_auth.AuthError, "не соответствует"):
                desktop_auth.use_data_dir_as_working_directory()


if __name__ == "__main__":
    unittest.main()
