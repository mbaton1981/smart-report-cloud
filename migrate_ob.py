import argparse
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


logger = logging.getLogger("smartreport.ob.migration")
MIGRATION_ID = "ob_interval_segments_v1"
OB_COLUMNS = {
    "start_time": "TEXT",
    "end_time": "TEXT",
    "ob_base_rate": "REAL",
}


def backup_filename(database_path, backup_date=None):
    backup_date = backup_date or datetime.now(timezone.utc).date()
    return f"{Path(database_path).stem}_before_ob_{backup_date.isoformat()}.db"


def backup_database(database_path, backup_path):
    source_path = Path(database_path).resolve()
    destination_path = Path(backup_path).resolve()
    if source_path == destination_path:
        raise ValueError("Путь резервной копии должен отличаться от пути базы")
    if not source_path.is_file():
        raise FileNotFoundError(f"Файл базы данных не найден: {source_path}")
    if destination_path.exists():
        raise FileExistsError(f"Файл backup уже существует: {destination_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(str(source_path), timeout=30)
    destination = sqlite3.connect(str(destination_path), timeout=30)
    try:
        source.backup(destination)
        result = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise sqlite3.DatabaseError(f"Проверка backup завершилась ошибкой: {result}")
    except Exception:
        destination.close()
        source.close()
        destination_path.unlink(missing_ok=True)
        raise
    destination.close()
    source.close()
    logger.info("Создана и проверена резервная копия: %s", destination_path)
    return destination_path


def migrate_database(database_path, backup_path):
    source_path = Path(database_path).resolve()
    backup_database(source_path, backup_path)
    conn = sqlite3.connect(str(source_path), timeout=30)
    added_columns = []
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        tables = {row[0] for row in cursor.fetchall()}
        work_tables = [table for table in ("shifts", "incoming_submissions") if table in tables]
        if not work_tables:
            raise sqlite3.DatabaseError("В базе не найдены shifts или incoming_submissions")

        for table in work_tables:
            cursor.execute(f'PRAGMA table_info("{table}")')
            columns = {row[1] for row in cursor.fetchall()}
            for column, column_type in OB_COLUMNS.items():
                if column not in columns:
                    cursor.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {column_type}')
                    added_columns.append(f"{table}.{column}")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ob_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shift_id TEXT NOT NULL,
                segment_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                hours REAL NOT NULL,
                category TEXT NOT NULL,
                premium_percent REAL NOT NULL,
                multiplier REAL NOT NULL,
                base_rate REAL NOT NULL,
                calculated_rate REAL NOT NULL,
                amount REAL NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (shift_id, segment_date, start_time, end_time, category)
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ob_segments_shift_id ON ob_segments(shift_id, segment_date)")
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL,
                description TEXT NOT NULL
            )
        ''')
        cursor.execute('''
            INSERT OR IGNORE INTO schema_migrations (migration_id, applied_at, description)
            VALUES (?, ?, ?)
        ''', (
            MIGRATION_ID,
            datetime.now(timezone.utc).isoformat(),
            "Additive OB interval fields and calculated segment storage",
        ))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    logger.info("Миграция %s завершена; добавлены поля: %s", MIGRATION_ID, added_columns or "нет")
    return {"backup_path": str(Path(backup_path).resolve()), "added_columns": added_columns}


def main():
    parser = argparse.ArgumentParser(description="Явная миграция SQLite для OB-интервалов")
    parser.add_argument("--database", required=True, help="Явный путь к SQLite-базе")
    parser.add_argument("--backup-dir", required=True, help="Каталог для обязательной резервной копии")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    backup_name = backup_filename(args.database)
    result = migrate_database(args.database, Path(args.backup_dir) / backup_name)
    print(f"OB schema migration complete. Backup: {result['backup_path']}")


if __name__ == "__main__":
    main()