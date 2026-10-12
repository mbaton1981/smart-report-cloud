import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from migrate_ob import MIGRATION_ID, backup_filename, migrate_database


class ObMigrationTests(unittest.TestCase):
    def test_production_backup_name_matches_requested_path(self):
        self.assertEqual(
            backup_filename("/data/cloud_database.db", date(2026, 10, 12)),
            "cloud_database_before_ob_2026-10-12.db",
        )

    def test_migration_is_idempotent_backed_up_and_preserves_legacy_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "legacy.db"
            conn = sqlite3.connect(database_path)
            conn.executescript('''
                CREATE TABLE shifts (
                    id INTEGER PRIMARY KEY, date TEXT NOT NULL, employee TEXT NOT NULL,
                    company TEXT NOT NULL, object_name TEXT NOT NULL, hours REAL NOT NULL,
                    rate REAL NOT NULL, transport REAL, comment TEXT, invoiced INTEGER,
                    sync_id TEXT, sync_version INTEGER, entry_type TEXT
                );
                CREATE TABLE incoming_submissions (
                    id INTEGER PRIMARY KEY, sync_id TEXT, submission_type TEXT,
                    date TEXT, employee TEXT, company TEXT, object_name TEXT, hours REAL
                );
                INSERT INTO shifts VALUES (1, '2026-10-12', 'A', 'C', 'O', 8, 100, 0, 'legacy', 0, 'stable-1', 1, 'work');
                INSERT INTO shifts VALUES (2, '2026-10-12', 'A', 'C', 'O', 4, 0, 0, 'atf', 0, 'stable-2', 1, 'atf');
                INSERT INTO incoming_submissions VALUES (1, 'stable-1', 'work', '2026-10-12', 'A', 'C', 'O', 8);
            ''')
            conn.commit()
            conn.close()

            first_backup = root / "backup-1.db"
            first_result = migrate_database(database_path, first_backup)
            second_backup = root / "backup-2.db"
            second_result = migrate_database(database_path, second_backup)

            self.assertTrue(first_backup.is_file())
            self.assertTrue(second_backup.is_file())
            self.assertTrue(first_result["added_columns"])
            self.assertEqual(second_result["added_columns"], [])
            conn = sqlite3.connect(database_path)
            self.assertEqual(
                conn.execute("SELECT id, hours, entry_type, comment FROM shifts ORDER BY id").fetchall(),
                [(1, 8.0, "work", "legacy"), (2, 4.0, "atf", "atf")],
            )
            self.assertEqual(
                conn.execute("SELECT migration_id FROM schema_migrations").fetchall(),
                [(MIGRATION_ID,)],
            )
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            conn.close()


if __name__ == "__main__":
    unittest.main()