import sys
import os
import shutil
import traceback
import subprocess
import sqlite3
import math
import requests
import json
import logging
import time
import uuid
import tempfile
import mimetypes
import hashlib
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from xml.sax.saxutils import escape
from datetime import date, datetime, timedelta, timezone

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QTabWidget,
    QFrame, QFormLayout, QDateEdit, QCheckBox, QFileDialog, QInputDialog,
    QDialog, QDialogButtonBox, QDoubleSpinBox, QScrollArea, QSizePolicy,
    QTextEdit, QSplitter, QGraphicsView, QGraphicsScene
)
from PyQt6.QtCore import Qt, QDate, QTimer, QThread, pyqtSignal, QEvent, QObject, QRunnable, QThreadPool
from PyQt6.QtGui import QIcon, QColor, QDesktopServices, QImage, QPixmap
from PyQt6.QtCore import QUrl

# Импорты для генерации PDF
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, LongTable, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from customer_report import format_quantity, money, positive_rate, prepare_customer_costs, write_customer_pdf
from ob_calculator import (
    calculate_work_interval,
    describe_category_time_ranges,
    round_time_to_half_hour,
)
from expense_documents import (
    apply_expense_documents_migration, build_project_order_allocations, decimal_text, document_storage_root,
    extract_local_document, find_document_duplicates, get_object_expense_summary,
    match_objects, parse_decimal, parse_document_text, parse_project_order_allocations,
    safe_folder_name, sha256_file,
    store_original_file,
    validate_allocations,
)
from expense_documents import (
    non_invoice_document_type, parse_invoice_pages, parse_invoice_money, net_invoice_issues,
    invoice_context, due_date_issues, due_date_edit,
)
from desktop_ui import ReportTable as QTableWidget

import desktop_auth

desktop_auth.use_data_dir_as_working_directory()

# Локальная база данных SQLite и папки
DB_FILE = os.environ.get("SMARTREPORT_DB_FILE", "smart_report.db")
BACKUP_DIR = "backups"
REPORTS_DIR = "reports"
SALARIES_DIR = "salaries"

# 🌐 Адрес облачного сервера на Render и ваш секретный ключ синхронизации
CLOUD_URL = desktop_auth.server_url()
ISOLATED_CANDIDATE = desktop_auth.isolated_candidate_mode()
SYNC_API_KEY = os.environ.get("SYNC_API_KEY", "")  # только для тестов; в установщике ключа нет


def sync_headers(extra=None):
    headers = dict(extra or {})
    from sync_dataset import HEADER, file_dataset
    dataset = file_dataset(DB_FILE)
    if dataset:
        headers[HEADER] = dataset
    if SYNC_API_KEY:
        headers["X-Sync-Key"] = SYNC_API_KEY
    elif desktop_auth.current_token():
        headers["Authorization"] = f"Bearer {desktop_auth.current_token()}"
    return headers


def has_sync_credentials():
    return not ISOLATED_CANDIDATE and bool(SYNC_API_KEY or desktop_auth.current_token())


def raise_for_sync_response(response):
    if response.ok:
        return
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, str) and error.strip():
        raise RuntimeError(
            f"Сервер отклонил синхронизацию (HTTP {response.status_code}): {error.strip()}"
        )
    response.raise_for_status()


def ensure_sync_dataset():
    from sync_dataset import file_dataset, require_response_dataset
    dataset = file_dataset(DB_FILE)
    if dataset:
        response = requests.get(
            f"{CLOUD_URL.rstrip('/')}/sync/capabilities", headers=sync_headers(), timeout=4
        )
        require_response_dataset(response, dataset)
        raise_for_sync_response(response)


def request_expense_api(method, path, **kwargs):
    if not has_sync_credentials():
        raise RuntimeError("Не задан ключ доступа к общему реестру документов: выполните вход.")
    headers = sync_headers(kwargs.pop("headers", {}))
    ensure_sync_dataset()
    response = requests.request(
        method, f"{CLOUD_URL.rstrip('/')}{path}", headers=headers, timeout=20, **kwargs
    )
    from sync_dataset import file_dataset, require_response_dataset
    require_response_dataset(response, file_dataset(DB_FILE))
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.ok:
        raise RuntimeError(payload.get("error") or f"Сервер вернул HTTP {response.status_code}.")
    return payload

RECORD_TYPE_WORK = "work"
RECORD_TYPE_ATF = "atf"
RECORD_TYPE_SICK = "sick"
RECORD_TYPE_VACATION = "vacation"
RECORD_TYPE_HOLIDAY = "holiday"
RECORD_TYPE_OB = "ob"
RECORD_TYPE_REVIEW = "review"
RECORD_TYPE_LABELS = {
    RECORD_TYPE_WORK: "Обычная работа",
    RECORD_TYPE_OB: "OB / Сверхурочные",
    RECORD_TYPE_ATF: "АТФ",
    RECORD_TYPE_SICK: "Больничный",
    RECORD_TYPE_VACATION: "Отпуск / Semestertimmar",
    RECORD_TYPE_HOLIDAY: "Красные дни / Röda dagar",
    RECORD_TYPE_REVIEW: "Требует проверки",
}

def record_type_short_label(record_type):
    mapping = {
        RECORD_TYPE_WORK: "Работа",
        RECORD_TYPE_OB: "OB",
        RECORD_TYPE_ATF: "АТФ",
        RECORD_TYPE_SICK: "Больничный",
        RECORD_TYPE_VACATION: "Отпуск",
        RECORD_TYPE_HOLIDAY: "Красные дни",
        RECORD_TYPE_REVIEW: "Проверка",
    }
    return mapping.get(record_type, "Работа")

def format_ob_detail(start_time, end_time, total_hours, segments=None):
    if segments and (not start_time or not end_time):
        start_time = segments[0].get("start_time")
        end_time = segments[-1].get("end_time")
    if not start_time or not end_time:
        return ""
    cat_short = {
        "weekday_day": "День",
        "weekday_early_evening": "OB1",
        "weekday_late_evening_night": "OB2",
        "saturday": "Сб",
        "sunday": "Вс",
        "weekday_early_morning_unpaid_pending_confirmation": "Утро",
    }
    interval_str = f"{start_time}–{end_time} ({float(total_hours):g} ч)"
    if not segments:
        return interval_str
    parts = []
    for s in segments:
        short_cat = cat_short.get(s.get("category"), s.get("category", "OB"))
        hrs = float(s.get("hours", 0.0))
        parts.append(f"{hrs:g} ч {short_cat}")
    breakdown_str = " / ".join(parts)
    return f"{interval_str}: {breakdown_str}" if breakdown_str else interval_str
SPECIAL_OBJECTS = {
    RECORD_TYPE_ATF: ("Arbetstidsförkortning", "p1000"),
    RECORD_TYPE_SICK: ("Sjuk", "p1001"),
    RECORD_TYPE_VACATION: ("Semestertimmar", "p1002"),
    RECORD_TYPE_HOLIDAY: (None, "p1004"),
}


def matches_special_object(object_name, marking, reference):
    expected_name, expected_marking = reference
    return marking == expected_marking and (
        expected_name is None or object_name == expected_name
    )


def swedish_public_holidays(year):
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    easter_month = (h + l - 7 * m + 114) // 31
    easter_day = (h + l - 7 * m + 114) % 31 + 1
    easter = date(year, easter_month, easter_day)
    midsummer_day = date(year, 6, 20) + timedelta(
        days=(5 - date(year, 6, 20).weekday()) % 7
    )
    all_saints_day = date(year, 10, 31) + timedelta(
        days=(5 - date(year, 10, 31).weekday()) % 7
    )
    holidays = {
        date(year, 1, 1), date(year, 1, 6),
        easter - timedelta(days=2), easter, easter + timedelta(days=1),
        date(year, 5, 1), easter + timedelta(days=39),
        easter + timedelta(days=49), midsummer_day, all_saints_day,
        date(year, 12, 25), date(year, 12, 26),
    }
    if year >= 2005:
        holidays.add(date(year, 6, 6))
    return holidays


def swedish_workday_norm(date_from, date_to, hours_per_day=8.0):
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if start > end:
        return 0.0
    holidays = set()
    for year in range(start.year, end.year + 1):
        holidays.update(swedish_public_holidays(year))
    workdays = 0
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in holidays:
            workdays += 1
        current += timedelta(days=1)
    return workdays * float(hours_per_day)


def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_local_object_sync_schema(conn):
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(objects)")
    columns = {row["name"] for row in cursor.fetchall()}
    for column_name, definition in (
        ("sync_id", "TEXT"),
        ("server_id", "INTEGER"),
        ("is_active", "INTEGER NOT NULL DEFAULT 1"),
        ("sync_version", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column_name not in columns:
            cursor.execute(f"ALTER TABLE objects ADD COLUMN {column_name} {definition}")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_objects_sync_id ON objects(sync_id) WHERE sync_id IS NOT NULL")

    cursor.execute("PRAGMA index_list(objects)")
    indexes = cursor.fetchall()
    unique_name_only = False
    index_sql = []
    for index in indexes:
        index_name = str(index["name"])
        escaped_name = index_name.replace('"', '""')
        cursor.execute(f'PRAGMA index_info("{escaped_name}")')
        indexed_columns = tuple(row["name"] for row in cursor.fetchall())
        if index["unique"] and indexed_columns == ("name",):
            unique_name_only = True
            continue
        definition = cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (index_name,)
        ).fetchone()
        if definition and definition[0]:
            index_sql.append(str(definition[0]))

    if not unique_name_only:
        conn.commit()
        return

    triggers = [row[0] for row in cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'objects' AND sql IS NOT NULL"
    ).fetchall()]
    conn.commit()
    foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE objects_sync_migration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                markning TEXT,
                company TEXT,
                rate REAL DEFAULT 0.0,
                transport_rate REAL DEFAULT 0.0,
                sync_id TEXT UNIQUE,
                server_id INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1,
                sync_version INTEGER NOT NULL DEFAULT 0,
                UNIQUE(name, company)
            )
        """)
        conn.execute("""
            INSERT INTO objects_sync_migration (
                id, name, markning, company, rate, transport_rate, sync_id, server_id, is_active, sync_version
            )
            SELECT id, name, markning, company, rate, transport_rate, sync_id, server_id, is_active, sync_version
            FROM objects
        """)
        conn.execute("DROP TABLE objects")
        conn.execute("ALTER TABLE objects_sync_migration RENAME TO objects")
        for statement in index_sql:
            conn.execute(statement)
        for statement in triggers:
            conn.execute(statement)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError("Object identity migration would break existing foreign-key links.")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if foreign_keys_enabled:
            conn.execute("PRAGMA foreign_keys = ON")

def init_db_once():
    is_new_db = not os.path.exists(DB_FILE)
    
    for d in [REPORTS_DIR, SALARIES_DIR, BACKUP_DIR]:
        if not os.path.exists(d):
            os.makedirs(d)

    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS objects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            markning TEXT,
            company TEXT,
            rate REAL DEFAULT 0.0,
            transport_rate REAL DEFAULT 0.0,
            sync_id TEXT UNIQUE,
            server_id INTEGER,
            is_active INTEGER NOT NULL DEFAULT 1,
            sync_version INTEGER NOT NULL DEFAULT 0,
            UNIQUE(name, company)
        )
    ''')
    
    cursor.execute("PRAGMA table_info(objects)")
    obj_columns = [col[1] for col in cursor.fetchall()]
    if 'rate' not in obj_columns:
        cursor.execute("ALTER TABLE objects ADD COLUMN rate REAL DEFAULT 0.0")
    if 'transport_rate' not in obj_columns:
        cursor.execute("ALTER TABLE objects ADD COLUMN transport_rate REAL DEFAULT 0.0")
    ensure_local_object_sync_schema(conn)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS object_sync_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL UNIQUE,
            object_id INTEGER NOT NULL REFERENCES objects(id) ON DELETE RESTRICT,
            sync_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_object_sync_outbox_pending ON object_sync_outbox(id)")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            salary_rate REAL DEFAULT 0.0,
            is_active INTEGER NOT NULL DEFAULT 1
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS employee_sync_outbox (
            operation_id TEXT PRIMARY KEY,
            employee_name TEXT NOT NULL,
            employee_key TEXT,
            revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_employee_sync_outbox_name ON employee_sync_outbox(employee_name)")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS employee_sync_state (
            employee_key TEXT PRIMARY KEY,
            employee_name TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('deleted', 'active')),
            sync_version INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS employee_restore_outbox (
            operation_id TEXT PRIMARY KEY,
            employee_key TEXT NOT NULL,
            employee_name TEXT NOT NULL,
            salary_rate REAL,
            revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_employee_restore_outbox_order ON employee_restore_outbox(created_at, operation_id)")
    for table_name, columns_to_add in (
        ("employee_sync_outbox", (("employee_key", "TEXT"), ("revision", "INTEGER NOT NULL DEFAULT 0"))),
        ("employee_sync_state", (("sync_version", "INTEGER NOT NULL DEFAULT 0"),)),
        ("employee_restore_outbox", (("salary_rate", "REAL"), ("revision", "INTEGER NOT NULL DEFAULT 0"))),
    ):
        cursor.execute(f"PRAGMA table_info({table_name})")
        existing_columns = {column[1] for column in cursor.fetchall()}
        for column_name, column_definition in columns_to_add:
            if column_name not in existing_columns:
                cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")
    cursor.execute("SELECT operation_id, employee_name FROM employee_sync_outbox WHERE employee_key IS NULL")
    for pending_delete in cursor.fetchall():
        employee_key = employee_sync_key(pending_delete["employee_name"])
        updated_at = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "UPDATE employee_sync_outbox SET employee_key = ?, revision = MAX(revision, 1) WHERE operation_id = ?",
            (employee_key, pending_delete["operation_id"]),
        )
        cursor.execute("""
            INSERT OR IGNORE INTO employee_sync_state (employee_key, employee_name, state, sync_version, updated_at)
            VALUES (?, ?, 'deleted', 1, ?)
        """, (
            employee_key, pending_delete["employee_name"], updated_at,
        ))
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS object_sync_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL UNIQUE,
            object_id INTEGER NOT NULL REFERENCES objects(id) ON DELETE RESTRICT,
            sync_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_object_sync_outbox_order ON object_sync_outbox(id)")

    cursor.execute("PRAGMA table_info(employees)")
    emp_columns = [col[1] for col in cursor.fetchall()]
    if 'is_active' not in emp_columns:
        cursor.execute("ALTER TABLE employees ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            employee TEXT NOT NULL,
            company TEXT NOT NULL,
            object_name TEXT NOT NULL,
            hours REAL NOT NULL,
            rate REAL NOT NULL,
            transport REAL DEFAULT 0.0,
            comment TEXT,
            invoiced INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS balance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            employee TEXT NOT NULL,
            description TEXT NOT NULL,
            change_amount REAL NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS company_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            amount REAL NOT NULL
        )
    ''')

    cursor.execute("PRAGMA table_info(shifts)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'invoiced' not in columns:
        cursor.execute("ALTER TABLE shifts ADD COLUMN invoiced INTEGER DEFAULT 0")
    if 'sync_id' not in columns:
        cursor.execute("ALTER TABLE shifts ADD COLUMN sync_id TEXT")
    if 'sync_version' not in columns:
        cursor.execute("ALTER TABLE shifts ADD COLUMN sync_version INTEGER NOT NULL DEFAULT 0")
    entry_type_added = 'entry_type' not in columns
    if entry_type_added:
        cursor.execute("ALTER TABLE shifts ADD COLUMN entry_type TEXT NOT NULL DEFAULT 'work'")
        named_special_objects = {
            record_type: reference for record_type, reference in SPECIAL_OBJECTS.items()
            if reference[0] is not None
        }
        cursor.execute(
            f"SELECT id, company, object_name FROM shifts WHERE object_name IN "
            f"({','.join('?' for _ in named_special_objects)})",
            tuple(reference[0] for reference in named_special_objects.values()),
        )
        for old_shift in cursor.fetchall():
            matching_types = []
            for record_type, (object_name, marking) in named_special_objects.items():
                if old_shift['object_name'] != object_name:
                    continue
                cursor.execute(
                    "SELECT id FROM objects WHERE company = ? AND name = ? AND COALESCE(markning, '') = ?",
                    (old_shift['company'], object_name, marking)
                )
                if len(cursor.fetchall()) == 1:
                    matching_types.append(record_type)
            migrated_type = matching_types[0] if len(matching_types) == 1 else RECORD_TYPE_REVIEW
            cursor.execute("UPDATE shifts SET entry_type = ? WHERE id = ?", (migrated_type, old_shift['id']))
    cursor.execute("PRAGMA table_info(shifts)")
    columns = {col[1] for col in cursor.fetchall()}
    if 'work_rate_cache' not in columns:
        cursor.execute("ALTER TABLE shifts ADD COLUMN work_rate_cache REAL NOT NULL DEFAULT 0.0")
    if 'work_transport_cache' not in columns:
        cursor.execute("ALTER TABLE shifts ADD COLUMN work_transport_cache REAL NOT NULL DEFAULT 0.0")
    if 'start_time' not in columns:
        cursor.execute("ALTER TABLE shifts ADD COLUMN start_time TEXT")
    if 'end_time' not in columns:
        cursor.execute("ALTER TABLE shifts ADD COLUMN end_time TEXT")
    if 'ob_base_rate' not in columns:
        cursor.execute("ALTER TABLE shifts ADD COLUMN ob_base_rate REAL")
    cursor.execute("UPDATE shifts SET work_rate_cache = COALESCE(rate, 0.0), work_transport_cache = COALESCE(transport, 0.0) WHERE entry_type = 'work'")

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
        CREATE TABLE IF NOT EXISTS shift_sync_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL UNIQUE,
            sync_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            base_version INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_shift_sync_outbox_pending ON shift_sync_outbox(state, sync_id, id)")
    cursor.execute("UPDATE shift_sync_outbox SET state = 'blocked' WHERE state = 'pending' AND sync_id IN (SELECT sync_id FROM shifts WHERE entry_type <> ?)", (RECORD_TYPE_WORK,))
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shift_sync_state (
            name TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        )
    ''')
    cursor.execute("INSERT OR IGNORE INTO shift_sync_state (name, value) VALUES ('cursor', 0)")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS expense_document_outbox (
            client_id TEXT PRIMARY KEY,
            operation TEXT NOT NULL CHECK (operation IN ('upsert', 'delete')),
            server_document_id INTEGER,
            server_version INTEGER,
            revision INTEGER NOT NULL DEFAULT 1,
            payload_json TEXT NOT NULL,
            attachment_paths_json TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'conflict')),
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    cursor.execute("PRAGMA table_info(expense_document_outbox)")
    expense_outbox_columns = {column[1] for column in cursor.fetchall()}
    if "server_version" not in expense_outbox_columns:
        cursor.execute("ALTER TABLE expense_document_outbox ADD COLUMN server_version INTEGER")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_expense_document_outbox_pending "
        "ON expense_document_outbox(state, updated_at, client_id)"
    )

    cursor.execute("SELECT id, date, employee, company, object_name, hours, rate, transport, comment, invoiced, sync_id, entry_type FROM shifts")
    existing_shifts = cursor.fetchall()
    for shift in existing_shifts:
        sync_id = shift['sync_id'] or str(uuid.uuid4())
        if not shift['sync_id']:
            cursor.execute("UPDATE shifts SET sync_id = ? WHERE id = ?", (sync_id, shift['id']))
            payload = {
                "date": shift['date'], "employee": shift['employee'], "company": shift['company'],
                "object_name": shift['object_name'], "hours": shift['hours'], "rate": shift['rate'],
                "transport": shift['transport'] or 0.0, "comment": shift['comment'] or "",
                "record_type": shift['entry_type'],
                "invoiced": shift['invoiced'] or 0
            }
            cursor.execute('''
                INSERT OR IGNORE INTO shift_sync_outbox
                    (operation_id, sync_id, operation, base_version, payload_json, created_at)
                VALUES (?, ?, 'upsert', 0, ?, ?)
            ''', (str(uuid.uuid4()), sync_id, json.dumps(payload), datetime.utcnow().isoformat()))
    cursor.execute("UPDATE shift_sync_outbox SET state = 'blocked' WHERE state = 'pending' AND sync_id IN (SELECT sync_id FROM shifts WHERE entry_type <> ?)", (RECORD_TYPE_WORK,))
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shifts_sync_id ON shifts(sync_id) WHERE sync_id IS NOT NULL")

    cursor.execute("SELECT COUNT(*) FROM employees")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT OR IGNORE INTO employees (name, salary_rate) VALUES (?, ?)", ("Aliaksei Patonich", 0.0))

    if is_new_db:
        initial_data = [
            ("Privat", "Badbacken 2", "p1010", 520.0, 0.0),
            ("Privat", "Koksgaatan 40", "p1011", 520.0, 0.0),
            ("Privat", "Tallvagen 12", "p1001", 520.0, 0.0),
            ("Dvaliks", "Klockargardsstigen 3", "***", 550.0, 0.0),
            ("Privat", "Norra vagen 7", "p1013", 520.0, 0.0),
            ("Privat", "Punchvagen 5", "p1012", 520.0, 0.0),
            ("Privat", "Storholmsvagen 24", "p1014", 520.0, 0.0),
            ("Privat", "Östra vägen 13", "p1017", 520.0, 0.0),
            ("Privat", "Punchvagen 5 Ext", "p1016", 520.0, 0.0),
            ("SBT", "Teknologgatan 7", "p2086", 580.0, 0.0),
            ("SBT", "Bisittargatan 20A", "p2083", 580.0, 0.0),
            ("Dvaliks", "Kronstigen 12", "***", 550.0, 0.0),
            ("Dvaliks", "Gustav 3 boulevard 167", "Gustav 3", 550.0, 0.0),
            ("SBT", "Krogtappan 97", "***", 580.0, 0.0),
            ("Privat", "Badbacken 2 Altan", "p1018", 520.0, 0.0),
            ("Privat", "Punchvagen 5 roof altan", "p1019", 520.0, 0.0),
            ("SBT", "Hamarby Alle 3b", "***", 580.0, 0.0),
            ("Privat", "Norra vagen 18 Niklas", "p1020", 520.0, 0.0),
            ("Privat", "Tallvagen 12 Fasad Mal", "p1021", 520.0, 0.0),
            ("Privat", "Badbacken 5 ext", "p1022", 520.0, 0.0),
            ("Privat", "Östra vägen 7B", "p1023", 520.0, 0.0),
            ("Privat", "Johan Hammarstrom", "p1024", 520.0, 0.0),
            ("Privat", "Norra vagen 26 Katarina", "p1025", 520.0, 0.0),
            ("Privat", "Tallvagen 14 Egil", "p1026", 520.0, 0.0),
            ("SBT", "Kraftriket 21", "p2152", 580.0, 0.0),
            ("SBT", "Teknikringen 35 Hogdel", "p2154", 580.0, 0.0),
            ("Dvaliks", "Drottning vag 123", "***", 550.0, 0.0),
            ("Pareto Properties AB", "Sonnebovagen 16 Sollentuna", "p1027", 600.0, 0.0),
            ("Dvaliks", "Soderproken 16 Lidingo", "p1028", 550.0, 0.0),
            ("SBT", "Alvsjoborgatan 1-3", "p2166", 580.0, 0.0),
            ("Dvaliks", "Nyneshamn", "p1029", 550.0, 0.0),
            ("SBT", "Vikingshillsvägen 15", "***", 580.0, 0.0),
            ("Pareto Properties AB", "Alfred Nobels Alle 109", "p1030", 600.0, 0.0),
            ("Renatur", "Tomteboda glass", "p1031", 550.0, 0.0),
            ("Renatur", "Tomteboda doors", "p1032", 550.0, 0.0),
            ("Privat", "Uddens vag 12 Tomas", "p1033", 520.0, 0.0),
            ("SBT", "Karlsviksgatan 15", "p2128", 580.0, 0.0),
            ("Renatur", "Tomteboda arbetsledning", "***", 550.0, 0.0),
            ("Renatur", "Tomteboda ÄTA", "p1034", 550.0, 0.0),
            ("Privat", "Tallvagen 11 1:116, Lidingö Kejill", "p1035", 520.0, 0.0),
            ("Privat", "Kolarbacken 52 136 48 Handen/Vega Daniel", "p1036", 520.0, 0.0),
            ("Renatur", "Tomteboda ÄTA Hotel", "****", 550.0, 0.0),
            ("Privat", "Tallvagen 11 1:116, Lidingö Kejill ÄTA", "p1037", 520.0, 0.0)
        ]

        unique_companies = sorted(list(set(item[0] for item in initial_data)))
        for comp in unique_companies:
            cursor.execute("INSERT OR IGNORE INTO companies (name) VALUES (?)", (comp,))

        for comp, name, mark, rate, tr in initial_data:
            cursor.execute('''
                INSERT OR IGNORE INTO objects (name, markning, company, rate, transport_rate) VALUES (?, ?, ?, ?, ?)
            ''', (name, mark, comp, rate, tr))

    ensure_object_name_history(cursor)
    conn.commit()
    conn.close()
    apply_expense_documents_migration(
        DB_FILE, Path(DB_FILE).resolve().parent / BACKUP_DIR
    )


def ensure_object_name_history(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS object_name_history (
            sync_id TEXT NOT NULL, company TEXT NOT NULL, name TEXT NOT NULL,
            PRIMARY KEY (sync_id, company, name)
        )
    """)
    cursor.execute("""
        INSERT OR IGNORE INTO object_name_history (sync_id, company, name)
        SELECT sync_id, company, name FROM objects WHERE sync_id IS NOT NULL
    """)


def invoice_object_names(cursor, object_row):
    names = {object_row['name']}
    exists = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='object_name_history'"
    ).fetchone()
    if exists and object_row['sync_id']:
        names.update(row[0] for row in cursor.execute(
            "SELECT name FROM object_name_history WHERE sync_id=? AND company=?",
            (object_row['sync_id'], object_row['company']),
        ))
    placeholders = ','.join('?' for _name in names)
    parameters = (object_row['company'], *sorted(names), object_row['id'])
    collision = cursor.execute(
        f"SELECT id FROM objects WHERE company=? AND name IN ({placeholders}) AND id<>? LIMIT 1",
        parameters,
    ).fetchone()
    if exists and not collision:
        collision = cursor.execute(
            "SELECT other.id FROM object_name_history history JOIN objects other ON other.sync_id=history.sync_id "
            f"WHERE history.company=? AND history.name IN ({placeholders}) AND other.id<>? LIMIT 1",
            parameters,
        ).fetchone()
    if collision:
        raise ValueError("Историческое название связано с несколькими объектами; автоматический отбор заблокирован.")
    return sorted(names)


def enqueue_object_sync_event(cursor, object_id, operation, extra=None):
    row = cursor.execute("SELECT * FROM objects WHERE id = ?", (int(object_id),)).fetchone()
    extra_payload = dict(extra or {})
    sync_id = row["sync_id"] if row is not None else None
    if operation == "link_legacy" and not sync_id:
        sync_id = extra_payload.pop("sync_id", None)
    if row is None or not sync_id:
        raise ValueError("Объект legacy не сопоставлен с серверным ID; синхронизация заблокирована.")
    revision = int(row["sync_version"] or 0) + 1
    event = {
        "operation_id": str(uuid.uuid4()),
        "sync_id": str(sync_id),
        "revision": revision,
        "operation": str(operation),
        "object_id": int(row["id"]),
        "object": {
            "name": str(row["name"]), "company": str(row["company"] or ""),
            "markning": str(row["markning"] or ""),
            "rate": float(row["rate"] or 0.0),
            "transport_rate": float(row["transport_rate"] or 0.0),
        },
    }
    if extra:
        event["object"].update(extra_payload)
    cursor.execute("""
        INSERT INTO object_sync_outbox (
            operation_id, object_id, sync_id, operation, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
    """, (
        event["operation_id"], int(row["id"]), event["sync_id"], event["operation"],
        json.dumps(event, ensure_ascii=False, sort_keys=True), datetime.now(timezone.utc).isoformat(),
    ))
    if operation == "link_legacy":
        cursor.execute(
            "UPDATE objects SET sync_id = ?, sync_version = ? WHERE id = ?",
            (event["sync_id"], revision, int(row["id"])),
        )
    else:
        cursor.execute("UPDATE objects SET sync_version = ? WHERE id = ?", (revision, int(row["id"])))
    return event


def merge_server_object_catalog(cursor, remote_objects):
    if not isinstance(remote_objects, list) or len(remote_objects) > 2000:
        raise ValueError("Серверный справочник объектов имеет неверный формат или размер.")

    stats = {
        "imported": 0, "updated": 0, "linked": 0, "unresolved_legacy": 0,
        "conflicts": 0, "unresolved_reasons": {}, "conflict_reasons": {},
    }

    def record_reason(bucket, reason):
        bucket[reason] = bucket.get(reason, 0) + 1

    remote_legacy_counts = {}
    for item in remote_objects:
        if not isinstance(item, dict) or str(item.get("sync_id") or "").strip():
            continue
        remote_key = (
            str(item.get("name") or "").strip(),
            " ".join(str(item.get("company") or "").split()),
            str(item.get("markning") or "").strip(),
        )
        remote_legacy_counts[remote_key] = remote_legacy_counts.get(remote_key, 0) + 1

    for remote in remote_objects:
        if not isinstance(remote, dict):
            raise ValueError("Некорректная запись серверного объекта.")
        try:
            server_id = int(remote.get("id"))
            name = str(remote.get("name") or "").strip()
            company = " ".join(str(remote.get("company") or "").split())
            markning = str(remote.get("markning") or "").strip()
            sync_id = str(remote.get("sync_id") or "").strip() or None
            rate = float(remote.get("rate") or 0.0)
            transport_rate = float(remote.get("transport_rate") or 0.0)
            active_value = remote.get("is_active", 1)
            if active_value not in (0, 1, False, True):
                raise ValueError()
            is_active = int(active_value)
            server_revision = int(remote.get("sync_version") or 0)
        except (TypeError, ValueError):
            raise ValueError("Некорректные поля серверного объекта.")
        if (server_id <= 0 or not name or len(name) > 200 or not company or len(company) > 150
                or len(markning) > 50 or server_revision < 0):
            raise ValueError("Некорректные поля серверного объекта.")
        if not 0 <= rate <= 100000 or not 0 <= transport_rate <= 100000 or not math.isfinite(rate) or not math.isfinite(transport_rate):
            raise ValueError("Некорректные ставки серверного объекта.")

        if sync_id:
            local = cursor.execute(
                "SELECT id, name, markning, company, rate, transport_rate, is_active, sync_version "
                "FROM objects WHERE sync_id = ?",
                (sync_id,),
            ).fetchone()
            if local:
                if local["company"] != company:
                    stats["conflicts"] += 1
                    record_reason(stats["conflict_reasons"], "sync_id закреплён за другой фирмой")
                    continue
                pending = cursor.execute(
                    "SELECT 1 FROM object_sync_outbox WHERE sync_id = ? LIMIT 1", (sync_id,)
                ).fetchone()
                if pending:
                    continue
                local_revision = int(local["sync_version"] or 0)
                if server_revision < local_revision:
                    stats["conflicts"] += 1
                    record_reason(stats["conflict_reasons"], "серверная revision ниже локальной")
                    continue
                if server_revision == local_revision:
                    local_state = (
                        local["name"], local["markning"] or "", local["company"],
                        float(local["rate"] or 0.0), float(local["transport_rate"] or 0.0),
                        int(bool(local["is_active"])),
                    )
                    remote_state = (name, markning, company, rate, transport_rate, is_active)
                    if local_state != remote_state:
                        stats["conflicts"] += 1
                        record_reason(stats["conflict_reasons"], "одинаковая revision содержит разные данные")
                        continue
                    if local_revision == server_revision:
                        cursor.execute(
                            "UPDATE objects SET server_id = ? WHERE id = ? AND sync_id = ?",
                            (server_id, int(local["id"]), sync_id),
                        )
                        continue
                if local['name'] != name:
                    ensure_object_name_history(cursor)
                cursor.execute("""
                    UPDATE objects
                    SET name = ?, markning = ?, rate = ?, transport_rate = ?, is_active = ?,
                        server_id = ?, sync_version = ?
                    WHERE id = ? AND sync_id = ?
                """, (
                    name, markning, rate, transport_rate, is_active, server_id, server_revision,
                    int(local["id"]), sync_id,
                ))
                stats["updated"] += 1
                continue

        collision = cursor.execute(
            "SELECT id FROM objects WHERE name = ? AND company = ?", (name, company)
        ).fetchone()
        if collision:
            legacy_key = (name, company, markning)
            local_candidates = cursor.execute(
                "SELECT id, sync_id, server_id, sync_version, rate, transport_rate, is_active "
                "FROM objects WHERE name = ? AND company = ? AND COALESCE(markning, '') = ?",
                legacy_key,
            ).fetchall()
            reasons = []
            if sync_id:
                reasons.append("серверная запись уже связана с другим sync_id")
            if len(local_candidates) == 0:
                reasons.append("локальная запись не совпадает по markning")
            elif len(local_candidates) > 1:
                reasons.append("найдено несколько локальных записей с тем же markning")
            if not sync_id and remote_legacy_counts.get(legacy_key) != 1:
                reasons.append("найдено несколько серверных Legacy-кандидатов")
            if len(local_candidates) == 1:
                local = local_candidates[0]
                pending = cursor.execute(
                    "SELECT 1 FROM object_sync_outbox WHERE object_id = ? LIMIT 1",
                    (int(local["id"]),),
                ).fetchone()
                local_rate = float(local["rate"] or 0.0)
                local_transport = float(local["transport_rate"] or 0.0)
                rate_compatible = local_rate == rate or (rate == 0 and local_rate > 0)
                transport_compatible = (
                    local_transport == transport_rate
                    or (transport_rate == 0 and local_transport > 0)
                )
                if local["sync_id"] is not None:
                    reasons.append("локальная запись уже связана с другим sync_id")
                if local["server_id"] is not None:
                    reasons.append("у локальной записи уже задан server_id")
                if pending:
                    reasons.append("для объекта уже есть ожидающее sync-событие")
                if int(local["sync_version"] or 0) != server_revision or server_revision != 0:
                    reasons.append("revision не совпадают или Legacy-связь уже изменялась")
                if int(bool(local["is_active"])) != is_active:
                    reasons.append("локальное и серверное состояние активности различаются")
                if not rate_compatible or not transport_compatible:
                    reasons.append("ненулевые локальные и серверные ставки различаются")
                if not reasons:
                    enqueue_object_sync_event(cursor, int(local["id"]), "link_legacy", {
                        "sync_id": str(uuid.uuid4()),
                        "server_object_id": server_id,
                        "expected_server_name": name,
                        "expected_server_company": company,
                        "expected_server_markning": markning,
                        "expected_server_rate": rate,
                        "expected_server_transport_rate": transport_rate,
                        "expected_server_is_active": is_active,
                        "expected_server_revision": server_revision,
                    })
                    stats["linked"] += 1
                    continue
            stats["unresolved_legacy"] += 1
            for reason in set(reasons or ["нет безопасного точного соответствия"]):
                record_reason(stats["unresolved_reasons"], reason)
            continue

        cursor.execute("INSERT OR IGNORE INTO companies (name) VALUES (?)", (company,))
        cursor.execute("""
            INSERT INTO objects (
                name, markning, company, rate, transport_rate, sync_id, server_id,
                is_active, sync_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            name, markning, company, rate, transport_rate, sync_id,
            server_id if sync_id else None, is_active, server_revision if sync_id else 0,
        ))
        stats["imported"] += 1
        if not sync_id:
            reasons = []
            if server_revision != 0:
                reasons.append("Legacy-запись имеет ненулевую revision")
            if remote_legacy_counts.get((name, company, markning)) != 1:
                reasons.append("серверный Legacy-кандидат неоднозначен")
            if not reasons:
                enqueue_object_sync_event(cursor, int(cursor.lastrowid), "link_legacy", {
                    "sync_id": str(uuid.uuid4()),
                    "server_object_id": server_id,
                    "expected_server_name": name,
                    "expected_server_company": company,
                    "expected_server_markning": markning,
                    "expected_server_rate": rate,
                    "expected_server_transport_rate": transport_rate,
                    "expected_server_is_active": is_active,
                    "expected_server_revision": server_revision,
                })
                stats["linked"] += 1
            else:
                stats["unresolved_legacy"] += 1
                for reason in set(reasons):
                    record_reason(stats["unresolved_reasons"], reason)
    return stats


def exact_legacy_object_candidates(objects, company, name, markning):
    return [
        item for item in objects
        if not item.get("sync_id")
        and item.get("company") == company
        and item.get("name") == name
        and str(item.get("markning") or "") == str(markning or "")
    ]


def employee_sync_key(name):
    return " ".join(str(name or "").split()).casefold()


def merge_server_employee_catalog(cursor, remote_employees):
    if not isinstance(remote_employees, list) or len(remote_employees) > 1000:
        raise ValueError("Серверный справочник сотрудников имеет неверный формат или размер.")

    remote_counts = {}
    for item in remote_employees:
        if not isinstance(item, dict):
            raise ValueError("Некорректная запись серверного сотрудника.")
        employee_name = " ".join(str(item.get("name") or "").split())
        employee_key = employee_sync_key(employee_name)
        if employee_key:
            remote_counts[employee_key] = remote_counts.get(employee_key, 0) + 1

    local_by_key = {}
    for row in cursor.execute("SELECT name FROM employees").fetchall():
        local_by_key.setdefault(employee_sync_key(row["name"]), []).append(row["name"])

    stats = {"imported": 0, "unresolved": 0}
    for item in remote_employees:
        employee_name = " ".join(str(item.get("name") or "").split())
        employee_key = employee_sync_key(employee_name)
        try:
            salary_rate = round(float(item.get("salary_rate") or 0.0), 2)
            is_active = int(item.get("is_active", 1))
            server_revision = int(item.get("sync_version") or 0)
        except (TypeError, ValueError):
            raise ValueError("Некорректные поля серверного сотрудника.")
        if (not employee_key or len(employee_name) > 150 or not math.isfinite(salary_rate)
                or not 0 <= salary_rate <= 100000 or is_active not in (0, 1) or server_revision < 0):
            raise ValueError("Некорректные поля серверного сотрудника.")
        if remote_counts[employee_key] > 1:
            stats["unresolved"] += 1
            continue
        local_matches = local_by_key.get(employee_key, [])
        if local_matches:
            if len(local_matches) > 1:
                stats["unresolved"] += 1
            continue

        state = cursor.execute(
            "SELECT state FROM employee_sync_state WHERE employee_key = ?", (employee_key,)
        ).fetchone()
        pending = cursor.execute(
            "SELECT 1 FROM employee_sync_outbox WHERE employee_key = ? "
            "UNION ALL SELECT 1 FROM employee_restore_outbox WHERE employee_key = ? LIMIT 1",
            (employee_key, employee_key),
        ).fetchone()
        if (state and state["state"] == "deleted") or pending:
            continue

        cursor.execute(
            "INSERT INTO employees (name, salary_rate, is_active) VALUES (?, ?, ?)",
            (employee_name, salary_rate, is_active),
        )
        state_value = "active" if is_active else "deleted"
        cursor.execute("""
            INSERT INTO employee_sync_state (
                employee_key, employee_name, state, sync_version, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(employee_key) DO UPDATE SET
                employee_name = excluded.employee_name,
                state = excluded.state,
                sync_version = MAX(employee_sync_state.sync_version, excluded.sync_version),
                updated_at = excluded.updated_at
        """, (
            employee_key, employee_name, state_value, server_revision,
            datetime.now(timezone.utc).isoformat(),
        ))
        local_by_key[employee_key] = [employee_name]
        stats["imported"] += 1
    return stats


def enqueue_employee_sync_event(cursor, employee_name, operation):
    employee_key = employee_sync_key(employee_name)
    if not employee_key or operation not in {"deactivate", "activate"}:
        raise ValueError("Некорректная операция синхронизации сотрудника.")
    state = cursor.execute(
        "SELECT state, sync_version FROM employee_sync_state WHERE employee_key = ?",
        (employee_key,),
    ).fetchone()
    pending_delete = cursor.execute(
        "SELECT MAX(revision) FROM employee_sync_outbox WHERE employee_key = ?",
        (employee_key,),
    ).fetchone()[0]
    pending_restore = cursor.execute(
        "SELECT MAX(revision) FROM employee_restore_outbox WHERE employee_key = ?",
        (employee_key,),
    ).fetchone()[0]
    current_revision = max(
        int(state["sync_version"] or 0) if state else 0,
        int(pending_delete or 0),
        int(pending_restore or 0),
    )
    if operation == "activate" and not (state and state["state"] == "deleted") and pending_delete is None:
        return None
    revision = current_revision + 1
    operation_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    if operation == "deactivate":
        cursor.execute("DELETE FROM employee_restore_outbox WHERE employee_key = ?", (employee_key,))
        cursor.execute("DELETE FROM employee_sync_outbox WHERE employee_key = ?", (employee_key,))
        cursor.execute("""
            INSERT INTO employee_sync_outbox (operation_id, employee_name, employee_key, revision, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (operation_id, employee_name, employee_key, revision, created_at))
        state_value = "deleted"
    else:
        matching_employees = [
            row for row in cursor.execute("SELECT name, salary_rate FROM employees").fetchall()
            if employee_sync_key(row["name"]) == employee_key
        ]
        if len(matching_employees) != 1:
            raise ValueError("Для восстановления нужна ровно одна активная локальная запись сотрудника.")
        salary_rate = float(matching_employees[0]["salary_rate"] or 0.0)
        if not 0 <= salary_rate <= 100000 or not math.isfinite(salary_rate):
            raise ValueError("Ставка восстанавливаемого сотрудника вне допустимого диапазона.")
        cursor.execute("DELETE FROM employee_sync_outbox WHERE employee_key = ?", (employee_key,))
        cursor.execute("""
            INSERT INTO employee_restore_outbox (
                operation_id, employee_key, employee_name, salary_rate, revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (operation_id, employee_key, employee_name, salary_rate, revision, created_at))
        state_value = "active"
    cursor.execute("""
        INSERT INTO employee_sync_state (employee_key, employee_name, state, sync_version, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(employee_key) DO UPDATE SET
            employee_name = excluded.employee_name,
            state = excluded.state,
            sync_version = excluded.sync_version,
            updated_at = excluded.updated_at
    """, (employee_key, employee_name, state_value, revision, created_at))
    return operation_id


def apply_employee_sync_ack(cursor, event, result):
    state = result.get("employee")
    if not isinstance(state, dict):
        return
    revision = int(result.get("revision", event["revision"]))
    employee_key = event["employee_key"]
    current = cursor.execute(
        "SELECT state, sync_version FROM employee_sync_state WHERE employee_key = ?",
        (employee_key,),
    ).fetchone()
    if current and int(current["sync_version"] or 0) > revision:
        return

    cursor.execute("""
        INSERT INTO employee_sync_state (employee_key, employee_name, state, sync_version, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(employee_key) DO UPDATE SET
            employee_name = excluded.employee_name,
            state = excluded.state,
            sync_version = excluded.sync_version,
            updated_at = excluded.updated_at
    """, (
        employee_key, state["name"], "active" if state["is_active"] else "deleted", revision,
        datetime.now(timezone.utc).isoformat(),
    ))
    local_matches = [
        row["name"] for row in cursor.execute("SELECT name FROM employees").fetchall()
        if employee_sync_key(row["name"]) == employee_key
    ]
    if len(local_matches) > 1:
        raise ValueError(f"Локальная запись сотрудника «{state['name']}» неоднозначна.")
    if state["is_active"]:
        if local_matches:
            cursor.execute(
                "UPDATE employees SET name = ?, salary_rate = ?, is_active = 1 WHERE name = ?",
                (state["name"], float(state["salary_rate"] or 0.0), local_matches[0]),
            )
        else:
            cursor.execute(
                "INSERT INTO employees (name, salary_rate, is_active) VALUES (?, ?, 1)",
                (state["name"], float(state["salary_rate"] or 0.0)),
            )
    elif local_matches:
        cursor.execute("UPDATE employees SET is_active = 0 WHERE name = ?", (local_matches[0],))

def enqueue_shift_operation(cursor, sync_id, operation, base_version, payload, state=None):
    record_type = payload.get('record_type', RECORD_TYPE_WORK)
    if record_type != RECORD_TYPE_WORK:
        cursor.execute("DELETE FROM shift_sync_outbox WHERE sync_id = ? AND state IN ('pending', 'blocked')", (sync_id,))
        if operation == 'delete' and int(base_version) == 0:
            return
    state = state or ('pending' if record_type == RECORD_TYPE_WORK else 'blocked')
    cursor.execute('''
        INSERT INTO shift_sync_outbox
            (operation_id, sync_id, operation, base_version, payload_json, state, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        str(uuid.uuid4()), sync_id, operation, int(base_version),
        json.dumps(payload, ensure_ascii=False), state, datetime.utcnow().isoformat()
    ))


def ensure_ob_schema(cursor):
    cursor.execute("PRAGMA table_info(shifts)")
    columns = {col[1] for col in cursor.fetchall()}
    if 'entry_type' not in columns:
        cursor.execute("ALTER TABLE shifts ADD COLUMN entry_type TEXT NOT NULL DEFAULT 'work'")
    if 'work_rate_cache' not in columns:
        cursor.execute("ALTER TABLE shifts ADD COLUMN work_rate_cache REAL NOT NULL DEFAULT 0.0")
    if 'work_transport_cache' not in columns:
        cursor.execute("ALTER TABLE shifts ADD COLUMN work_transport_cache REAL NOT NULL DEFAULT 0.0")
    if 'start_time' not in columns:
        cursor.execute("ALTER TABLE shifts ADD COLUMN start_time TEXT")
    if 'end_time' not in columns:
        cursor.execute("ALTER TABLE shifts ADD COLUMN end_time TEXT")
    if 'ob_base_rate' not in columns:
        cursor.execute("ALTER TABLE shifts ADD COLUMN ob_base_rate REAL")
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


def ob_schema_ready(cursor):
    ensure_ob_schema(cursor)
    cursor.execute("PRAGMA table_info(shifts)")
    columns = {row[1] for row in cursor.fetchall()}
    required = {"start_time", "end_time", "ob_base_rate"}
    cursor.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ob_segments'")
    return required.issubset(columns) and cursor.fetchone() is not None


def invoice_ob_schema_present(cursor):
    cursor.execute("PRAGMA table_info(ob_segments)")
    columns = {row[1] for row in cursor.fetchall()}
    return {
        "shift_id", "segment_date", "start_time", "end_time",
        "hours", "category", "premium_percent", "amount", "base_rate",
        "multiplier", "calculated_rate",
    }.issubset(columns)


def get_local_employee_ob_base_rate(cursor, employee):
    cursor.execute("SELECT COALESCE(salary_rate, 0.0) FROM employees WHERE name = ?", (employee,))
    row = cursor.fetchone()
    if not row:
        raise ValueError("Для OB не найден профиль сотрудника с базовой ставкой")
    return float(row[0])


def replace_ob_segments(cursor, sync_id, segments):
    cursor.execute("DELETE FROM ob_segments WHERE shift_id = ?", (sync_id,))
    now = datetime.now(timezone.utc).isoformat()
    cursor.executemany('''
        INSERT INTO ob_segments (
            shift_id, segment_date, start_time, end_time, hours, category,
            premium_percent, multiplier, base_rate, calculated_rate, amount,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', [(
        sync_id, segment["segment_date"], segment["start_time"], segment["end_time"],
        segment["hours"], segment["category"], segment["premium_percent"],
        segment["multiplier"], segment["base_rate"], segment["calculated_rate"],
        segment["amount"], now, now,
    ) for segment in segments])


def get_ob_segments(cursor, sync_id):
    cursor.execute("""
        SELECT segment_date, start_time, end_time, hours, category, premium_percent,
               multiplier, base_rate, calculated_rate, amount
        FROM ob_segments WHERE shift_id = ? ORDER BY segment_date, start_time, id
    """, (sync_id,))
    return [dict(row) for row in cursor.fetchall()]


def collect_invoice_ob_details(cursor, company, object_name, date_from, date_to):
    category_labels = {
        "weekday_day": "dagtid",
        "weekday_early_evening": "OB1",
        "weekday_late_evening_night": "OB2",
        "weekday_early_morning_unpaid_pending_confirmation": "morgon",
        "saturday": "lördag",
        "sunday": "söndag",
    }
    filters = ["date >= ?", "date <= ?", "entry_type = 'ob'"]
    params = [date_from, date_to]
    if company is not None:
        filters.insert(0, "company = ?")
        params.insert(0, company)
    if object_name is not None:
        filters.insert(1 if company is not None else 0, "object_name = ?")
        params.insert(1 if company is not None else 0, object_name)
    cursor.execute(f"""
        SELECT employee, date, sync_id, start_time, end_time, hours
        FROM shifts
        WHERE {' AND '.join(filters)}
        ORDER BY employee, date, start_time, id
    """, params)

    details_by_employee = {}
    for shift in cursor.fetchall():
        segments = get_ob_segments(cursor, shift["sync_id"])
        if segments:
            segment_details = []
            for segment in segments:
                category = category_labels.get(segment["category"], segment["category"])
                segment_details.append(
                    f"{segment['start_time']}–{segment['end_time']}: "
                    f"{float(segment['hours']):.2f} tim {category} "
                    f"(×{float(segment['multiplier']):.2f})"
                )
            detail = (
                f"{shift['date']} {shift['start_time']}–{shift['end_time']}: "
                + " / ".join(segment_details)
            )
        else:
            detail = (
                f"{shift['date']} {shift['start_time']}–{shift['end_time']} "
                f"({float(shift['hours'] or 0.0):.2f} tim)"
            )
        details_by_employee.setdefault(shift["employee"], []).append(detail)

    return {
        employee: {
            "display": "\n".join(details),
            "tooltip": "\n".join(details),
        }
        for employee, details in details_by_employee.items()
    }


INVOICE_OB_CATEGORY_LABELS = {
    "weekday_early_evening": "OB1",
    "weekday_late_evening_night": "OB2",
    "saturday": "Lördag",
    "sunday": "Söndag",
}


def _invoice_segment_interval(segment):
    day = datetime.strptime(segment["segment_date"], "%Y-%m-%d").date()
    start_hour, start_minute = (int(value) for value in segment["start_time"].split(":"))
    end_hour, end_minute = (int(value) for value in segment["end_time"].split(":"))
    start = day.toordinal() * 1440 + start_hour * 60 + start_minute
    end = day.toordinal() * 1440 + end_hour * 60 + end_minute
    if end <= start:
        end += 1440
    return start, end


def _invoice_union_hours(intervals):
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    start, end = ordered[0]
    total_minutes = 0
    for next_start, next_end in ordered[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            total_minutes += end - start
            start, end = next_start, next_end
    total_minutes += end - start
    return round(total_minutes / 60, 6)


def _collect_invoice_ob_hours(cursor, company, object_names, date_from, date_to):
    if not object_names:
        return {}, {}
    placeholders = ",".join("?" for _name in object_names)
    rows = cursor.execute(f"""
        SELECT shifts.employee, ob_segments.segment_date, ob_segments.start_time,
               ob_segments.end_time, ob_segments.category, ob_segments.premium_percent
        FROM shifts
        JOIN ob_segments ON ob_segments.shift_id = shifts.sync_id
        WHERE shifts.company = ? AND shifts.object_name IN ({placeholders})
          AND shifts.date >= ? AND shifts.date <= ? AND shifts.entry_type = 'ob'
        ORDER BY shifts.employee, ob_segments.segment_date, ob_segments.start_time
    """, (company, *object_names, date_from, date_to)).fetchall()

    category_intervals = {}
    employee_intervals = {}
    for row in rows:
        if float(row[5] or 0) <= 0:
            continue
        interval = _invoice_segment_interval(row)
        category_intervals.setdefault((row[0], row[4]), []).append(interval)
        employee_intervals.setdefault(row[0], []).append(interval)

    result = {}
    for (employee, category), intervals in category_intervals.items():
        result.setdefault(employee, {})[category] = _invoice_union_hours(intervals)
    return result, {
        employee: _invoice_union_hours(intervals)
        for employee, intervals in employee_intervals.items()
    }


def collect_invoice_ob_category_hours(cursor, company, object_names, date_from, date_to):
    return _collect_invoice_ob_hours(cursor, company, object_names, date_from, date_to)[0]


def collect_invoice_payroll(cursor, company, object_names, date_from, date_to, has_ob_schema):
    placeholders = ",".join("?" for _ in object_names)
    shifts = cursor.execute(f"""
        SELECT employee, entry_type, hours, sync_id
        FROM shifts WHERE company=? AND object_name IN ({placeholders})
          AND date>=? AND date<=? AND entry_type IN ('work','ob')
        ORDER BY employee,date,id
    """, (company, *object_names, date_from, date_to)).fetchall()
    result = {}
    for shift in shifts:
        employee = shift["employee"]
        item = result.setdefault(employee, {
            "base_amount": Decimal("0"), "ob_premium": Decimal("0"),
            "rates": set(), "warnings": [],
        })
        hours = Decimal(str(shift["hours"]))
        if shift["entry_type"] == "work":
            profiles = cursor.execute("SELECT salary_rate FROM employees WHERE name=?", (employee,)).fetchall()
            rate = profiles[0][0] if len(profiles) == 1 else None
            if rate is None or not math.isfinite(float(rate)) or float(rate) <= 0:
                item["base_amount"] = None
                item["warnings"].append(f"{employee}: зарплатная ставка не определена.")
            elif item["base_amount"] is not None:
                item["base_amount"] += hours * Decimal(str(rate))
                item["rates"].add(f"{float(rate):.2f} SEK/h (текущая ставка сотрудника)")
        else:
            segments = get_ob_segments(cursor, shift["sync_id"]) if has_ob_schema else []
            if not segments or abs(sum(Decimal(str(s["hours"])) for s in segments) - hours) > Decimal("0.000001"):
                item["base_amount"] = item["ob_premium"] = None
                item["warnings"].append(f"{employee}: начисления OB не подтверждены сохранёнными сегментами.")
                continue
            for segment in segments:
                base = Decimal(str(segment["hours"])) * Decimal(str(segment["base_rate"]))
                total = Decimal(str(segment["amount"]))
                if item["base_amount"] is not None:
                    item["base_amount"] += base
                if item["ob_premium"] is not None:
                    item["ob_premium"] += total - base
                item["rates"].add(
                    f"{float(segment['base_rate']):.2f} SEK/h; "
                    f"+{float(segment['premium_percent']):g}% "
                    f"{segment['start_time']}–{segment['end_time']} (сохранено)"
                )
    for item in result.values():
        for key in ("base_amount", "ob_premium"):
            if item[key] is not None:
                item[key] = float(item[key].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        item["total"] = (
            item["base_amount"] + item["ob_premium"]
            if item["base_amount"] is not None and item["ob_premium"] is not None else None
        )
        item["rates"] = sorted(item["rates"])
    return result


def calculate_payroll_components(
    work_hours, atf_hours, sick_hours, salary_rate, ob_amount,
    vacation_percent, tax_percent, employer_fee_percent,
    atf_percent=0.0, sick_percent=0.0,
):
    work_pay = float(work_hours or 0.0) * float(salary_rate or 0.0)
    ob_pay = float(ob_amount or 0.0)
    base_salary = work_pay + ob_pay
    atf_pay = float(atf_hours or 0.0) * float(salary_rate or 0.0) * float(atf_percent or 0.0) / 100
    sick_pay = float(sick_hours or 0.0) * float(salary_rate or 0.0) * float(sick_percent or 0.0) / 100
    vacation_pay = base_salary * float(vacation_percent or 0.0) / 100
    taxable_gross = base_salary + atf_pay + sick_pay + vacation_pay
    tax = taxable_gross * float(tax_percent or 0.0) / 100
    net_pay = taxable_gross - tax
    employer_fee = taxable_gross * float(employer_fee_percent or 0.0) / 100
    return {
        "work_pay": round(work_pay, 2),
        "ob_pay": round(ob_pay, 2),
        "base_salary": round(base_salary, 2),
        "atf_pay": round(atf_pay, 2),
        "sick_pay": round(sick_pay, 2),
        "vacation_pay": round(vacation_pay, 2),
        "taxable_gross": round(taxable_gross, 2),
        "tax": round(tax, 2),
        "net_pay": round(net_pay, 2),
        "employer_fee": round(employer_fee, 2),
        "employer_total_cost": round(taxable_gross + employer_fee, 2),
    }


def compact_blocked_shift_operations(cursor, allowed_types=None):
    allowed_types = set(allowed_types or (
        RECORD_TYPE_WORK, RECORD_TYPE_ATF, RECORD_TYPE_SICK, RECORD_TYPE_VACATION,
        RECORD_TYPE_HOLIDAY,
    ))
    cursor.execute("SELECT DISTINCT sync_id FROM shift_sync_outbox WHERE state = 'blocked'")
    sync_ids = [row['sync_id'] for row in cursor.fetchall()]
    released = 0
    for sync_id in sync_ids:
        cursor.execute("SELECT * FROM shifts WHERE sync_id = ?", (sync_id,))
        local = cursor.fetchone()
        if local:
            if local['entry_type'] not in allowed_types:
                continue
            payload = shift_payload(local)
            cursor.execute("DELETE FROM shift_sync_outbox WHERE sync_id = ? AND state IN ('pending', 'blocked')", (sync_id,))
            enqueue_shift_operation(cursor, sync_id, 'upsert', local['sync_version'], payload, state='pending')
            released += 1
            continue

        cursor.execute("SELECT * FROM shift_sync_outbox WHERE sync_id = ? AND state = 'blocked' ORDER BY id DESC", (sync_id,))
        blocked = cursor.fetchall()
        tombstone = next((row for row in blocked if row['operation'] == 'delete'), None)
        if tombstone and int(tombstone['base_version']) > 0:
            payload = json.loads(tombstone['payload_json'])
            if payload.get('record_type') in allowed_types:
                cursor.execute("DELETE FROM shift_sync_outbox WHERE sync_id = ? AND state IN ('pending', 'blocked')", (sync_id,))
                enqueue_shift_operation(cursor, sync_id, 'delete', tombstone['base_version'], payload, state='pending')
                released += 1
        else:
            cursor.execute("DELETE FROM shift_sync_outbox WHERE sync_id = ? AND state IN ('pending', 'blocked')", (sync_id,))
    return released

def shift_payload(row):
    payload = {
        "date": row['date'], "employee": row['employee'], "company": row['company'],
        "object_name": row['object_name'], "hours": row['hours'], "rate": row['rate'],
        "transport": row['transport'] or 0.0, "comment": row['comment'] or "",
        "record_type": row['entry_type'] if 'entry_type' in row.keys() else RECORD_TYPE_WORK,
        "invoiced": row['invoiced'] or 0
    }
    if payload["record_type"] == RECORD_TYPE_OB:
        payload.update({
            "start_time": row["start_time"],
            "end_time": row["end_time"],
        })
    return payload


def record_type_label(record_type):
    return RECORD_TYPE_LABELS.get(record_type, RECORD_TYPE_LABELS[RECORD_TYPE_REVIEW])


def resolve_special_object(cursor, company, record_type):
    reference = SPECIAL_OBJECTS.get(record_type)
    if not reference:
        return None, "Неизвестный тип записи."
    if reference[0] is None:
        cursor.execute(
            "SELECT id, name, markning, company FROM objects WHERE company = ? "
            "AND is_active = 1 AND COALESCE(markning, '') = ?",
            (company, reference[1]),
        )
    else:
        cursor.execute(
            "SELECT id, name, markning, company FROM objects WHERE company = ? "
            "AND name = ? AND is_active = 1 AND COALESCE(markning, '') = ?",
            (company, reference[0], reference[1]),
        )
    matches = cursor.fetchall()
    if len(matches) != 1:
        expected = f"{reference[0]} / {reference[1]}" if reference[0] else reference[1]
        if not matches:
            return None, f"В справочнике фирмы «{company}» не найдена запись «{expected}»."
        return None, f"В справочнике фирмы «{company}» неоднозначна запись «{expected}»."
    return matches[0], None

def enqueue_matching_shifts(cursor, where_clause, params):
    cursor.execute(f"SELECT * FROM shifts WHERE {where_clause}", params)
    for row in cursor.fetchall():
        sync_id = row['sync_id'] or str(uuid.uuid4())
        if not row['sync_id']:
            cursor.execute("UPDATE shifts SET sync_id = ? WHERE id = ?", (sync_id, row['id']))
        enqueue_shift_operation(cursor, sync_id, 'upsert', row['sync_version'], shift_payload(row))

class ExpenseReadSignals(QObject):
    completed = pyqtSignal(object, object, object, str)


class ExpenseReadTask(QRunnable):
    def __init__(self, key):
        super().__init__()
        self.key = key
        self.signals = ExpenseReadSignals()

    def run(self):
        try:
            objects = request_expense_api("GET", "/api/expense-objects").get("objects", [])
            params = {name: value for name, value in zip(("date_from", "date_to"), self.key) if value}
            documents = request_expense_api("GET", "/api/expense-documents", params=params).get("documents", [])
            self.signals.completed.emit(self.key, objects, documents, "")
        except Exception as error:
            self.signals.completed.emit(self.key, [], [], str(error))


class SyncWorker(QThread):
    completed = pyqtSignal(bool, int, int, int, int, int, str, str, str)

    def __init__(self, window):
        super().__init__(window)
        self.window = window

    def run(self):
        try:
            result = self.window.perform_sync_logic()
            self.completed.emit(
                True, result["conflicts"], result["linked_legacy"],
                result["unresolved_legacy"], result["imported_employees"],
                result["unresolved_employees"], result["conflict_details"],
                result["unresolved_legacy_details"], "",
            )
        except Exception as error:
            self.completed.emit(False, 0, 0, 0, 0, 0, "", "", str(error))

def parse_float(val_str, default=0.0):
    if not val_str:
        return default
    try:
        cleaned = str(val_str).strip().replace(',', '.')
        return float(cleaned)
    except ValueError:
        return default

def translate_to_swedish(text, *, allow_network=True):
    if not text or not str(text).strip():
        return ""
    
    raw_text = str(text).strip()
    
    if allow_network:
        try:
            url = f"https://api.mymemory.translated.net/get?q={requests.utils.quote(raw_text)}&langpair=ru|sv"
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                if data and "responseData" in data and data["responseData"].get("translatedText"):
                    translated = data["responseData"]["translatedText"]
                    if "MYMEMORY WARNING" not in translated:
                        return translated
        except Exception:
            pass
    
    t = raw_text.lower()
    if not allow_network:
        for original, translated in (
            ("внутренних окон комнат", "invändiga fönster i rummen"),
            ("коробов вентиляции", "ventilationskanaler"),
            ("кухонных витяжок", "köksfläktar"),
            ("кухонных вытяжек", "köksfläktar"),
            ("водяных труб", "vattenrör"),
            ("демонтаж,", "demontering av "),
            ("демонтаж", "demontering av"),
        ):
            t = t.replace(original, translated)
        import re
        t = re.sub(r"([,;:])", r" \1 ", t)
    replacements = {
        "гипсокартон": "gips", "гипсу": "gips", "картон": "kartong",
        "вентиляц": "ventilation", "электрик": "elarbete", "проводк": "eldragning",
        "розетк": "vägguttag", "демонтаж": "rivning", "демонт": "rivning",
        "монтаж": "montering", "монт": "montering", "покраск": "målning",
        "красил": "målade", "краск": "färg", "утеплен": "isolering",
        "изоляц": "isolering", "установк": "installation", "шпаклев": "spackling",
        "шпатлев": "spackling", "уборк": "städning", "убирал": "städade",
        "ремонт": "renovering", "стен": "väggar", "потолк": "tak",
        "пол": "golv", "окн": "fönster", "окон": "fönster", "двер": "dörrar", "плитк": "kakel",
        "замен": "byte", "стяжк": "avjämningsmassa", "работ": "arbeten",
        "мусор": "avfall", "доставк": "leverans"
    }
    words = t.split()
    translated_words = []
    for word in words:
        clean_w = word.strip(".,;:!?()[]{}\"'")
        if not allow_network and clean_w == "и":
            translated_words.append("och")
            continue
        matched = False
        for ru_root, se_word in replacements.items():
            if ru_root in clean_w:
                result_word = se_word
                if word and word[0].isupper():
                    result_word = result_word.capitalize()
                translated_words.append(result_word)
                matched = True
                break
        if not matched:
            translated_words.append(word)
    result = " ".join(translated_words)
    if not allow_network:
        result = result.replace(" ,", ",").replace(" ;", ";").replace(" :", ":")
    return result


class JournalTranslationDialog(QDialog):
    def __init__(self, pending, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Проверка перевода описаний для PDF")
        self.resize(1150, 600)
        self.translations = {}
        self._editors = []
        layout = QVBoxLayout(self)
        notice = QLabel(
            "Не удалось полностью перевести указанные записи. Проверьте или введите шведский текст справа.\n"
            "Исправления используются только в этом PDF. Исходные описания, смены и база не изменяются."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        table = QTableWidget(len(pending), 3)
        table.setHorizontalHeaderLabels(["Запись / дата / сотрудник / объект", "Исходное описание (без изменений)", "Перевод на шведский для PDF"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setWordWrap(True)
        for index, (row, suggestion, error) in enumerate(pending):
            context = (
                f"№{row['id']} · {row['date']}\n{row['employee']}\n"
                f"{row['object_name']}\nID: {row['sync_id']}"
            )
            for column, text in enumerate((context, row["comment"] or "")):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setToolTip(error if column == 0 else text)
                table.setItem(index, column, item)
            editor = QTextEdit()
            editor.setObjectName("journal_translation_" + str(row["id"]))
            editor.setPlainText(suggestion)
            table.setCellWidget(index, 2, editor)
            table.setRowHeight(index, 130)
            self._editors.append((row, editor))
        layout.addWidget(table)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Использовать перевод и создать PDF")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self):
        import re
        translations = {}
        for row, editor in self._editors:
            text = editor.toPlainText().strip()
            if not text or re.search(r"[\u0400-\u04ff]", text):
                self.error_label.setText(
                    f"Запись №{row['id']} · {row['date']} · {row['employee']}: "
                    "введите непустой перевод на шведском; в тексте осталась кириллица или поле пустое."
                )
                editor.setFocus()
                return
            translations[row["sync_id"]] = text
        self.translations = translations
        super().accept()


try:
    windows_font_path = os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'Fonts', 'arial.ttf')
    if os.path.exists(windows_font_path):
        pdfmetrics.registerFont(TTFont('CustomArial', windows_font_path))
        pdfmetrics.registerFont(TTFont('CustomArialBold', os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'Fonts', 'arialbd.ttf')))
        DEFAULT_FONT = 'CustomArial'
        DEFAULT_FONT_BOLD = 'CustomArialBold'
    else:
        DEFAULT_FONT = 'Helvetica'
        DEFAULT_FONT_BOLD = 'Helvetica-Bold'
except Exception:
    DEFAULT_FONT = 'Helvetica'
    DEFAULT_FONT_BOLD = 'Helvetica-Bold'

DARK_THEME_QSS = """
    QMainWindow, QWidget {
        background-color: #1e2229;
        color: #e2e8f0;
        font-family: 'Segoe UI', -apple-system, Arial, sans-serif;
        font-size: 13px;
    }
    QTabWidget::pane {
        border: 1px solid #2d3748;
        background-color: #242933;
        border-radius: 8px;
        top: -1px;
    }
    QLabel, QCheckBox {
        background-color: transparent;
        border: none;
    }
    QTabBar::tab {
        background-color: #2a313d;
        color: #a0aec0;
        padding: 9px 16px;
        margin-right: 4px;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        border: 1px solid #323946;
        border-bottom: none;
        font-weight: 600;
    }
    QTabBar::tab:selected {
        background-color: #242933;
        color: #ffffff;
        border-bottom: 2px solid #4299e1;
    }
    QTabBar::tab:hover {
        background-color: #323a48;
        color: #ffffff;
    }
    QPushButton {
        background-color: #313c4e;
        color: #ffffff;
        border: 1px solid #4a5568;
        padding: 7px 14px;
        border-radius: 7px;
        min-height: 20px;
        font-weight: 600;
    }
    QPushButton:hover {
        background-color: #3c495f;
        border-color: #4299e1;
    }
    QPushButton:pressed {
        background-color: #2b6cb0;
        border-color: #3182ce;
    }
    QPushButton:disabled {
        background-color: #252b35;
        color: #718096;
        border-color: #323946;
    }
    QPushButton:focus {
        border-color: #90cdf4;
    }
    QPushButton#primaryAction {
        background-color: #2b6cb0;
        border-color: #4299e1;
    }
    QPushButton#primaryAction:hover {
        background-color: #3182ce;
        border-color: #63b3ed;
    }
    QPushButton#secondaryAction, QPushButton#editShiftButton {
        background-color: #2a313d;
        border-color: #4a5568;
    }
    QPushButton#secondaryAction:hover, QPushButton#editShiftButton:hover {
        background-color: #3c495f;
        border-color: #90cdf4;
    }
    QPushButton#deleteShiftButton {
        background-color: #632c2c;
        border-color: #9b3c3c;
    }
    QPushButton#deleteShiftButton:hover {
        background-color: #7a3434;
        border-color: #c53030;
    }
    QPushButton#editShiftButton, QPushButton#deleteShiftButton {
        padding: 5px 9px;
        font-size: 12px;
        min-height: 28px;
    }
    QPushButton#danger {
        background-color: #632c2c;
        border-color: #9b3c3c;
    }
    QPushButton#danger:hover {
        background-color: #7a3434;
        border-color: #c53030;
    }
    QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox, QTextEdit {
        background-color: #28303d;
        color: #f7fafc;
        border: 1px solid #3f4c60;
        padding: 6px 12px;
        border-radius: 6px;
        selection-background-color: #3182ce;
        selection-color: #ffffff;
    }
    QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus {
        border: 1px solid #63b3ed;
        background-color: #2d3748;
    }
    QComboBox::drop-down, QDateEdit::drop-down {
        subcontrol-origin: padding;
        subcontrol-position: center right;
        width: 32px;
        border-left: 1px solid #3f4c60;
        border-top-right-radius: 6px;
        border-bottom-right-radius: 6px;
        background-color: #313c4e;
    }
    QComboBox::down-arrow, QDateEdit::down-arrow {
        image: none;
        border-left: 5px solid transparent;
        border-right: 5px solid transparent;
        border-top: 6px solid #e2e8f0;
        width: 0px;
        height: 0px;
        margin-right: 10px;
    }
    QComboBox::down-arrow:hover, QDateEdit::down-arrow:hover {
        border-top: 6px solid #63b3ed;
    }
    QSpinBox, QDoubleSpinBox {
        padding-right: 30px;
        min-height: 22px;
    }
    QCheckBox#edit_shift_transport::indicator {
        width: 18px;
        height: 18px;
        border: 2px solid #cbd5e0;
        border-radius: 3px;
        background-color: #1a1f26;
    }
    QCheckBox#edit_shift_transport::indicator:checked {
        image: url("__TRANSPORT_CHECK_IMAGE__");
        background-color: #2b6cb0;
        border-color: #90cdf4;
    }
    QSpinBox::up-button, QDoubleSpinBox::up-button,
    QSpinBox::down-button, QDoubleSpinBox::down-button {
        subcontrol-origin: border;
        width: 26px;
        background-color: #3f4c60;
        border-left: 1px solid #64748b;
    }
    QSpinBox::up-button, QDoubleSpinBox::up-button {
        subcontrol-position: top right;
        border-bottom: 1px solid #64748b;
    }
    QSpinBox::down-button, QDoubleSpinBox::down-button {
        subcontrol-position: bottom right;
    }
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
        background-color: #2b6cb0;
    }
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
        image: url("__SPIN_UP_IMAGE__");
        width: 12px;
        height: 8px;
    }
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
        image: url("__SPIN_DOWN_IMAGE__");
        width: 12px;
        height: 8px;
    }
    QTableWidget, QTableView {
        background-color: #1a1f26;
        alternate-background-color: #252d38;
        color: #e2e8f0;
        gridline-color: #2d3748;
        border: 1px solid #323946;
        border-radius: 8px;
        selection-background-color: #2b6cb0;
        selection-color: #ffffff;
    }
    QTableWidget::item, QTableView::item {
        padding: 4px 6px;
    }
    QTableWidget::item:hover:!selected, QTableView::item:hover:!selected {
        background-color: #303c4b;
    }
    QTableWidget::item:selected, QTableView::item:selected {
        background-color: #3182ce;
        color: #ffffff;
    }
    QTableWidget::item:selected:!active, QTableView::item:selected:!active {
        background-color: #2d567c;
    }
    QToolTip {
        background-color: #303c4b;
        color: #f7fafc;
        border: 1px solid #64748b;
        padding: 6px;
        border-radius: 6px;
    }
    QHeaderView::section {
        background-color: #28303d;
        color: #63b3ed;
        padding: 7px;
        border: 1px solid #323946;
        font-weight: bold;
        font-size: 12px;
    }
    QFrame#card {
        background-color: #242933;
        border: 1px solid #323946;
        border-radius: 8px;
        padding: 6px;
    }
"""
DARK_THEME_QSS = DARK_THEME_QSS.replace(
    "__SPIN_UP_IMAGE__", (Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / "spin-up.svg").as_posix()
).replace(
    "__SPIN_DOWN_IMAGE__", (Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / "spin-down.svg").as_posix()
).replace(
    "__TRANSPORT_CHECK_IMAGE__", (Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / "transport-check.svg").as_posix()
)

def create_date_field(default_date_str=""):
    date_edit = QDateEdit()
    date_edit.setMinimumWidth(144)
    date_edit.setCalendarPopup(True)
    date_edit.setDisplayFormat("yyyy-MM-dd")
    if default_date_str:
        qdate = QDate.fromString(default_date_str, "yyyy-MM-dd")
        if qdate.isValid():
            date_edit.setDate(qdate)
    else:
        date_edit.setDate(QDate.currentDate())
    date_edit.text = lambda: date_edit.date().toString("yyyy-MM-dd")
    date_edit.setText = lambda val: date_edit.setDate(QDate.fromString(val, "yyyy-MM-dd"))
    return date_edit


def load_document_preview_pixmap(source_path):
    suffix = Path(source_path).suffix.casefold()
    if suffix == ".pdf":
        import pypdfium2 as pdfium
        image = pdfium.PdfDocument(str(source_path))[0].render(scale=1.5).to_pil().convert("RGB")
        image = QImage(image.tobytes(), image.width, image.height, image.width * 3, QImage.Format.Format_RGB888).copy()
        return QPixmap.fromImage(image)
    return QPixmap(str(source_path))


class ZoomableDocumentView(QGraphicsView):
    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self._image = self._scene.addPixmap(pixmap)
        self.setScene(self._scene)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setMinimumSize(280, 320)
        QTimer.singleShot(0, self.fit_width)

    def zoom(self, factor):
        current = self.transform().m11()
        target = min(8.0, max(0.1, current * factor))
        self.scale(target / current, target / current)

    def fit_width(self):
        pixmap = self._image.pixmap()
        if pixmap.isNull() or pixmap.width() <= 0:
            return
        self.resetTransform()
        factor = max(0.1, (self.viewport().width() - 24) / pixmap.width())
        self.scale(factor, factor)
        self.centerOn(self._image)

    def show_original_size(self):
        self.resetTransform()
        self.centerOn(self._image)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)
            event.accept()
            return
        super().wheelEvent(event)


class DocumentPreviewDialog(QDialog):
    def __init__(self, pixmap, title, parent=None, source_path=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1180, 900)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.preview = ZoomableDocumentView(pixmap, self)
        for label, callback in (
            ("−", lambda: self.preview.zoom(1 / 1.2)),
            ("По ширине", self.preview.fit_width),
            ("100%", self.preview.show_original_size),
            ("+", lambda: self.preview.zoom(1.2)),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            controls.addWidget(button)
        if source_path:
            source_button = QPushButton("Открыть файл целиком")
            source_button.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(source_path)))
            )
            controls.addWidget(source_button)
        controls.addStretch()
        layout.addLayout(controls)
        layout.addWidget(self.preview, 1)


class DocumentGalleryDialog(QDialog):
    def __init__(self, paths, title, parent=None, initial_page=0, initial_attachment=0):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1180, 900)
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="smartreport_document_gallery_")
        self._pdf_documents = []
        self._pages = []
        self._attachment_offsets = []
        self._page_index = 0
        starting_page = 0
        for attachment_index, path in enumerate(paths):
            self._attachment_offsets.append(len(self._pages))
            if attachment_index == initial_attachment:
                starting_page = len(self._pages) + initial_page
            source = Path(path)
            if source.suffix.casefold() == ".pdf":
                import pypdfium2 as pdfium
                try:
                    document = pdfium.PdfDocument(str(source))
                except Exception:
                    for opened_document in self._pdf_documents:
                        opened_document.close()
                    self._temporary_directory.cleanup()
                    raise
                self._pdf_documents.append(document)
                self._pages.extend(("pdf", document, page, source.name) for page in range(len(document)))
            else:
                self._pages.append(("image", source, 0, source.name))

        layout = QVBoxLayout(self)
        self.preview = ZoomableDocumentView(QPixmap(), self)
        controls = QHBoxLayout()
        self.previous_button = QPushButton("‹")
        self.previous_button.setToolTip("Предыдущая страница")
        self.previous_button.clicked.connect(lambda: self.show_page(self._page_index - 1))
        self.next_button = QPushButton("›")
        self.next_button.setToolTip("Следующая страница")
        self.next_button.clicked.connect(lambda: self.show_page(self._page_index + 1))
        self.page_label = QLabel("")
        self.page_label.setMinimumWidth(150)
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        controls.addWidget(self.previous_button)
        controls.addWidget(self.page_label)
        controls.addWidget(self.next_button)
        controls.addStretch()
        layout.addLayout(controls)
        zoom_controls = QHBoxLayout()
        for label, callback in (
            ("−", lambda: self.preview.zoom(1 / 1.2)),
            ("По ширине", self.preview.fit_width),
            ("100%", self.preview.show_original_size),
            ("+", lambda: self.preview.zoom(1.2)),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            zoom_controls.addWidget(button)
        zoom_controls.addStretch()
        layout.addLayout(zoom_controls)
        layout.addWidget(self.preview, 1)
        self.show_page(starting_page)

    def show_page(self, index):
        if not self._pages:
            self.page_label.setText("Страниц нет")
            self.previous_button.setEnabled(False)
            self.next_button.setEnabled(False)
            return
        self._page_index = max(0, min(int(index), len(self._pages) - 1))
        kind, source, page_index, name = self._pages[self._page_index]
        if kind == "pdf":
            image = source[page_index].render(scale=1.5).to_pil().convert("RGB")
            qimage = QImage(
                image.tobytes(), image.width, image.height, image.width * 3,
                QImage.Format.Format_RGB888,
            ).copy()
            pixmap = QPixmap.fromImage(qimage)
        else:
            pixmap = QPixmap(str(source))
        self.preview._image.setPixmap(pixmap)
        self.preview.fit_width()
        self.page_label.setText(f"{self._page_index + 1} / {len(self._pages)} · {name}")
        self.previous_button.setEnabled(self._page_index > 0)
        self.next_button.setEnabled(self._page_index + 1 < len(self._pages))

    def show_source(self, attachment_index, page):
        if 0 <= attachment_index < len(self._attachment_offsets):
            end = self._attachment_offsets[attachment_index + 1] if attachment_index + 1 < len(self._attachment_offsets) else len(self._pages)
            index = self._attachment_offsets[attachment_index] + max(0, page - 1)
            if index < end:
                self.show_page(index)

    def closeEvent(self, event):
        for document in self._pdf_documents:
            document.close()
        self._temporary_directory.cleanup()
        super().closeEvent(event)


def create_half_hour_time_field(default_time):
    field = QComboBox()
    for minutes in range(0, 24 * 60, 30):
        value = f"{minutes // 60:02d}:{minutes % 60:02d}"
        field.addItem(value, value)
    field.setCurrentText(round_time_to_half_hour(default_time))
    return field


class SmartReportApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self._expense_temp_directory = tempfile.TemporaryDirectory(prefix="smartreport_expenses_")
        self.setWindowTitle("SmartReport — смены и расходы по объектам")
        if ISOLATED_CANDIDATE:
            version = os.environ.get("SMARTREPORT_CANDIDATE_VERSION") or "1.0.19-rc1"
            self.setWindowTitle(f"SmartReport {version} — изолированная база, без сервера")
            self.statusBar().showMessage("Локальный кандидат: синхронизация и почтовый перенос отключены.")
        self.resize(1240, 880)
        self.setStyleSheet(DARK_THEME_QSS)
        self._async_expense_reads = True
        self._expense_read_cache = {}
        self._expense_read_attempts = {}
        self._expense_read_busy = False
        self._expense_read_task = None
        self._expense_online = False

        init_db_once()
        self.init_ui()

        self.auto_sync_timer = QTimer(self)
        self.auto_sync_timer.timeout.connect(self.background_sync_with_cloud)
        self._sync_worker = None
        self._manual_sync_requested = False
        if not ISOLATED_CANDIDATE:
            self.auto_sync_timer.start(2000)

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)

        self.tabs = QTabWidget()
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideNone)
        self.tabs.currentChanged.connect(self.on_tab_changed)
        main_layout.addWidget(self.tabs)

        self.tab_hours = QWidget()
        self.tab_salary = QWidget()
        self.tab_balance = QWidget()
        self.tab_expenses = QWidget()
        self.tab_object_expenses = QWidget()
        self.tab_invoice = QWidget()
        self.tab_dagbok = QWidget()
        self.tab_sprav = QWidget()
        
        self.tabs.addTab(self.tab_hours, "📝 Ввод часов")
        self.tabs.addTab(self.tab_salary, "💰 Зарплата")
        self.tabs.addTab(self.tab_balance, "⚖️ Баланс")
        self.tabs.addTab(self.tab_expenses, "Расходы")
        self.tabs.addTab(self.tab_invoice, "📄 Отчёты и фактурирование")
        self.tabs.addTab(self.tab_sprav, "📚 Справочники")

        self.setup_hours_tab()
        self.setup_salary_tab()
        self.setup_balance_tab()
        self.setup_expenses_tab()
        self.setup_object_expenses_tab()
        self.setup_invoice_tab()
        self.setup_dagbok_tab()
        self.setup_reporting_pages()
        self.setup_sprav_tab()
        
        self.load_dropdowns()

        current_tab = self.tabs.currentIndex()
        blocked = self.tabs.blockSignals(True)
        for index in range(self.tabs.count()):
            title = self.tabs.tabText(index)
            page = self.tabs.widget(index)
            self.tabs.removeTab(index)
            page.setMinimumWidth(0)
            page.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            scroll_area = QScrollArea()
            scroll_area.setWidgetResizable(True)
            scroll_area.setFrameShape(QFrame.Shape.NoFrame)
            scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            scroll_area.setWidget(page)
            self.tabs.insertTab(index, scroll_area, title)
        self.tabs.blockSignals(blocked)
        self.tabs.setCurrentIndex(current_tab)

    def on_tab_changed(self, index):
        self.load_dropdowns(refresh_tables=False)
        if index == 1:
            self.load_salary_table_template()
        elif index == 2:
            self.load_balance_table()
        elif index == 3:
            self.load_expense_document_filters()
            self.load_expense_documents()
            if self.exp_document_tabs.currentIndex() == 1:
                self.load_object_expense_overview()
        elif index == 5:
            self.load_companies_table()
            self.load_objects_table()
            self.load_employees_table()

    def setup_hours_tab(self):
        layout = QVBoxLayout(self.tab_hours)
        card = QFrame()
        card.setObjectName("card")
        card_layout = QFormLayout(card)
        self.hours_form_layout = card_layout
        card_layout.setSpacing(8)

        self.date_input = create_date_field(QDate.currentDate().toString("yyyy-MM-dd"))
        self.emp_cb = QComboBox()
        self.comp_cb = QComboBox()
        self.load_companies_into_combobox(self.comp_cb)

        self.record_type_cb = QComboBox()
        for record_type in (
            RECORD_TYPE_WORK, RECORD_TYPE_OB, RECORD_TYPE_ATF,
            RECORD_TYPE_SICK, RECORD_TYPE_VACATION, RECORD_TYPE_HOLIDAY,
        ):
            self.record_type_cb.addItem(record_type_label(record_type), record_type)
        
        self.obj_cb = QComboBox()
        self.load_objects_for_company(self.comp_cb.currentText(), self.obj_cb, RECORD_TYPE_WORK)
        self.comp_cb.currentTextChanged.connect(self.reload_input_objects)
        self.record_type_cb.currentIndexChanged.connect(self.reload_input_objects)

        self.hours_input = QLineEdit("8.0")
        self.rate_input = QLineEdit()
        self.rate_input.setPlaceholderText("Например: 520 (или берется из объекта)")
        self.trans_input = QLineEdit("0")
        self.trans_input.setPlaceholderText("1 = включить транспорт объекта")
        self.comment_input = QLineEdit()
        self.comment_input.setPlaceholderText("Описание выполненных работ...")
        self.ob_start_time = create_half_hour_time_field("16:00")
        self.ob_end_time = create_half_hour_time_field("23:00")
        self.ob_preview_lbl = QLabel("Введите интервал для расчёта сегментов.")
        self.ob_preview_lbl.setWordWrap(True)

        card_layout.addRow("Дата (ГГГГ-ММ-ДД):", self.date_input)
        card_layout.addRow("Сотрудник:", self.emp_cb)
        card_layout.addRow("Фирма:", self.comp_cb)
        card_layout.addRow("Тип записи:", self.record_type_cb)
        card_layout.addRow("Объект:", self.obj_cb)
        self.record_type_status_lbl = QLabel("")
        self.record_type_status_lbl.setWordWrap(True)
        card_layout.addRow(self.record_type_status_lbl)
        card_layout.addRow("Часы:", self.hours_input)
        card_layout.addRow("Фактурная ставка (крон/час):", self.rate_input)
        card_layout.addRow("Транспорт (0 или 1):", self.trans_input)
        card_layout.addRow("Описание работ (Журнал):", self.comment_input)
        card_layout.addRow("Время начала:", self.ob_start_time)
        card_layout.addRow("Время окончания:", self.ob_end_time)
        card_layout.addRow("Рассчитанные сегменты:", self.ob_preview_lbl)

        self._ob_form_widgets = (
            self.ob_start_time, self.ob_end_time, self.ob_preview_lbl,
        )
        self._standard_hour_widgets = (self.hours_input, self.rate_input)
        self.record_type_cb.currentIndexChanged.connect(self.update_ob_form_visibility)
        self.date_input.dateChanged.connect(self.refresh_ob_preview)
        self.ob_start_time.currentTextChanged.connect(self.refresh_ob_preview)
        self.ob_end_time.currentTextChanged.connect(self.refresh_ob_preview)
        self.emp_cb.currentTextChanged.connect(self.refresh_ob_preview)
        self.update_ob_form_visibility()

        save_btn = QPushButton("💾 Сохранить смену")
        save_btn.setObjectName("primaryAction")
        save_btn.clicked.connect(self.save_shift)
        card_layout.addRow(save_btn)

        layout.addWidget(card)
        
        control_card = QFrame()
        control_card.setObjectName("card")
        control_card.setStyleSheet("QFrame#card { background-color: #242933; border: 1px solid #3182ce; border-radius: 8px; margin-top: 5px; }")
        c_layout = QVBoxLayout(control_card)
        c_layout.setSpacing(8)

        btn_row = QHBoxLayout()
        refresh_shifts_btn = QPushButton("🔄 Обновить данные")
        refresh_shifts_btn.setObjectName("secondaryAction")
        refresh_shifts_btn.clicked.connect(self.load_shifts_history)
        
        sync_cloud_btn = QPushButton("🌐 Синхронизация с облаком")
        sync_cloud_btn.setObjectName("primaryAction")
        sync_cloud_btn.clicked.connect(self.sync_with_cloud)

        del_shift_btn = QPushButton("🗑 Удалить")
        del_shift_btn.setObjectName("deleteShiftButton")
        del_shift_btn.setToolTip("Удалить выбранную смену")
        del_shift_btn.clicked.connect(self.delete_shift)
        edit_shift_btn = QPushButton("✏ Изменить")
        edit_shift_btn.setObjectName("editShiftButton")
        edit_shift_btn.setToolTip("Изменить выбранную смену")
        edit_shift_btn.clicked.connect(self.edit_shift)

        btn_row.addWidget(refresh_shifts_btn)
        btn_row.addWidget(sync_cloud_btn)
        btn_row.addWidget(edit_shift_btn)
        btn_row.addStretch()
        btn_row.addWidget(del_shift_btn)
        c_layout.addLayout(btn_row)

        filter_layout = QVBoxLayout()
        filter_date_row = QHBoxLayout()
        filter_select_row = QHBoxLayout()
        
        self.filter_date_from = create_date_field("2026-10-01")
        self.filter_date_from.dateChanged.connect(self.apply_shifts_filters)

        self.filter_date_to = create_date_field("2026-10-31")
        self.filter_date_to.dateChanged.connect(self.apply_shifts_filters)

        self.filter_emp = QComboBox()
        self.filter_emp.addItem("-- Все сотрудники --")
        self.filter_emp.currentIndexChanged.connect(self.apply_shifts_filters)

        self.filter_comp = QComboBox()
        self.filter_comp.addItem("-- Все фирмы --")
        self.filter_comp.currentIndexChanged.connect(self.apply_shifts_filters)

        self.filter_obj = QComboBox()
        self.filter_obj.addItem("-- Все объекты --")
        self.filter_obj.currentIndexChanged.connect(self.apply_shifts_filters)

        self.filter_status = QComboBox()
        self.filter_status.addItems(["-- Все статусы --", "В работе", "Выставлен в счёт"])
        self.filter_status.currentIndexChanged.connect(self.apply_shifts_filters)

        filter_date_row.addWidget(QLabel("<b>Период с:</b>"))
        filter_date_row.addWidget(self.filter_date_from, 1)
        filter_date_row.addWidget(QLabel("<b>по:</b>"))
        filter_date_row.addWidget(self.filter_date_to, 1)

        for filter_combo in (self.filter_emp, self.filter_comp, self.filter_obj, self.filter_status):
            filter_combo.setMinimumContentsLength(9)
            filter_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            filter_select_row.addWidget(filter_combo, 1)

        filter_layout.addLayout(filter_date_row)
        filter_layout.addLayout(filter_select_row)
        c_layout.addLayout(filter_layout)

        layout.addWidget(control_card)

        layout.addWidget(QLabel("<b>История смен в базе данных (зеленые — уже выставлены в счёт):</b>"))
        
        self.table_shifts = QTableWidget()
        self.table_shifts.setMinimumHeight(220)
        self.table_shifts.setColumnCount(13)
        self.table_shifts.setHorizontalHeaderLabels(["ID", "Дата", "Сотрудник", "Фирма", "Объект", "Тип", "Часы", "Детализация / OB", "Факт. ставка", "Транспорт", "Комментарий", "Статус фактуры", "OB сумма (SEK)"])
        self.table_shifts.setColumnHidden(0, True)
        shifts_header = self.table_shifts.horizontalHeader()
        shifts_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in {
            1: 95,   # Дата: узкая ровно под YYYY-MM-DD
            2: 125,  # Сотрудник: ровно под ФИО
            3: 105,  # Фирма
            4: 135,  # Объект
            5: 75,   # Тип: узкая (Работа / OB / АТФ / Больничный)
            6: 60,   # Часы: узкая под 2-3 цифры (напр. 8.0)
            7: 175,  # Детализация / OB (16:00–23:00 (7 ч) / 2 ч OB1 / 2 ч OB2...)
            8: 80,   # Факт. ставка: максимально узкая
            9: 70,   # Транспорт: узкая под 0.0 / 1.0
            11: 110, # Статус фактуры: узкая под статус (⌛ В работе / ✅ Выставлен)
            12: 95,  # OB сумма (SEK)
        }.items():
            self.table_shifts.setColumnWidth(column, width)
        shifts_header.setSectionResizeMode(10, QHeaderView.ResizeMode.Stretch)  # Комментарий: забирает всё свободное место
        self.table_shifts.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table_shifts.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table_shifts.setAlternatingRowColors(True)
        self.table_shifts.cellDoubleClicked.connect(lambda *_: self.edit_shift())
        layout.addWidget(self.table_shifts)
        self.load_shifts_history()

    def update_ob_form_visibility(self, *_):
        is_ob = self.record_type_cb.currentData() == RECORD_TYPE_OB
        for widget in self._ob_form_widgets:
            widget.setVisible(is_ob)
            label = self.hours_form_layout.labelForField(widget)
            if label:
                label.setVisible(is_ob)
        for widget in self._standard_hour_widgets:
            widget.setVisible(not is_ob)
            label = self.hours_form_layout.labelForField(widget)
            if label:
                label.setVisible(not is_ob)
        self.refresh_ob_preview()

    def refresh_ob_preview(self, *_):
        if not hasattr(self, "ob_preview_lbl") or self.record_type_cb.currentData() != RECORD_TYPE_OB:
            return
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            base_rate = get_local_employee_ob_base_rate(cursor, self.emp_cb.currentText().strip())
            if base_rate <= 0:
                self.ob_preview_lbl.setText("В профиле сотрудника не настроена базовая ставка. Попросите администратора обновить профиль.")
                return
            result = calculate_work_interval(
                self.date_input.text(),
                self.ob_start_time.currentText(),
                self.ob_end_time.currentText(),
                base_rate,
            )
        except (TypeError, ValueError) as error:
            self.ob_preview_lbl.setText(f"Ошибка интервала: {error}")
            return
        finally:
            conn.close()
        category_labels = {
            "weekday_early_morning_unpaid_pending_confirmation": "00:00–07:00 без оплаты до подтверждения договора",
            "weekday_day": "Дневное время",
            "weekday_early_evening": "Ранний вечер",
            "weekday_late_evening_night": "Поздний вечер/ночь",
            "saturday": "Суббота",
            "sunday": "Воскресенье",
        }
        lines = [
            f"{segment['segment_date']} {segment['start_time']}–{segment['end_time']}: "
            f"{category_labels.get(segment['category'], segment['category'])}, "
            f"{segment['hours']:.2f} ч, +{segment['premium_percent']:.0f}%, "
            f"×{segment['multiplier']:.2f}, сумма {segment['amount']:.2f} kr"
            for segment in result["segments"]
        ]
        self.ob_preview_lbl.setText(
            "<br>".join(lines) +
            f"<br><b>Всего: {result['total_hours']:.2f} ч; OB сумма: {result['total_amount']:.2f} kr</b>"
            "<br><small>Начальные тарифы требуют подтверждения по применяемому kollektivavtal.</small>"
        )

    def apply_shifts_filters(self):
        d_from = self.filter_date_from.text().strip()
        d_to = self.filter_date_to.text().strip()
        
        emp_val = self.filter_emp.currentText()
        comp_val = self.filter_comp.currentText()
        obj_val = self.filter_obj.currentText()
        status_val = self.filter_status.currentText()

        for row in range(self.table_shifts.rowCount()):
            match = True
            date_cell = self.table_shifts.item(row, 1).text()
            emp_cell = self.table_shifts.item(row, 2).text()
            comp_cell = self.table_shifts.item(row, 3).text()
            obj_cell = self.table_shifts.item(row, 4).text()
            status_cell = self.table_shifts.item(row, 11).text()

            if d_from and d_to and not (d_from <= date_cell <= d_to):
                match = False
            if emp_val != "-- Все сотрудники --" and emp_cell != emp_val:
                match = False
            if comp_val != "-- Все фирмы --" and comp_cell != comp_val:
                match = False
            if obj_val != "-- Все объекты --" and obj_cell != obj_val:
                match = False
            if status_val == "В работе" and "В работе" not in status_cell:
                match = False
            elif status_val == "Выставлен в счёт" and "Выставлен" not in status_cell:
                match = False

            self.table_shifts.setRowHidden(row, not match)

    def reload_input_objects(self, *_):
        record_type = self.record_type_cb.currentData()
        if record_type in SPECIAL_OBJECTS:
            self.record_type_status_lbl.setText("Тип хранится отдельно и не входит в оплату. Сервер пока не поддерживает синхронизацию АТФ/больничного.")
        else:
            self.record_type_status_lbl.clear()
        company = self.comp_cb.currentText()
        if not company:
            self.load_objects_for_company(company, self.obj_cb, record_type)
            return
        if self.load_objects_for_company(company, self.obj_cb, record_type):
            return
        if record_type in SPECIAL_OBJECTS:
            conn = get_db_connection()
            _, error = resolve_special_object(conn.cursor(), company, record_type)
            conn.close()
            QMessageBox.warning(self, "Справочник объекта", error or "Не удалось выбрать справочную запись.")

    def apply_remote_shift(self, cursor, shift):
        sync_id = str(shift.get('sync_id') or shift.get('request_id') or '').strip()
        if not sync_id:
            return

        record_type = shift.get('record_type', shift.get('submission_type', RECORD_TYPE_WORK))
        if record_type == RECORD_TYPE_OB and not ob_schema_ready(cursor):
            raise sqlite3.OperationalError("Для получения OB необходимо выполнить миграцию desktop SQLite с backup")

        cursor.execute("SELECT * FROM shifts WHERE sync_id = ?", (sync_id,))
        local = cursor.fetchone()
        if shift.get('deleted'):
            cursor.execute("UPDATE shift_sync_outbox SET state = 'conflict' WHERE sync_id = ? AND state IN ('pending', 'blocked')", (sync_id,))
            if ob_schema_ready(cursor):
                cursor.execute("DELETE FROM ob_segments WHERE shift_id = ?", (sync_id,))
            if local:
                cursor.execute("DELETE FROM shifts WHERE sync_id = ?", (sync_id,))
            return
        cursor.execute("SELECT operation, payload_json FROM shift_sync_outbox WHERE sync_id = ? AND state = 'blocked' ORDER BY id DESC LIMIT 1", (sync_id,))
        blocked_change = cursor.fetchone()
        if blocked_change:
            blocked_payload = json.loads(blocked_change['payload_json'])
            if blocked_change['operation'] == 'delete' or blocked_payload.get('record_type') != RECORD_TYPE_WORK:
                return
        if not local and shift.get('source') == 'legacy':
            cursor.execute('''
                SELECT * FROM shifts
                WHERE date = ? AND employee = ? AND company = ? AND object_name = ? AND hours = ?
            ''', (shift['date'], shift['employee'], shift['company'], shift['object_name'], shift['hours']))
            candidates = cursor.fetchall()
            if len(candidates) == 1:
                local = candidates[0]
                previous_sync_id = local['sync_id']
                cursor.execute("UPDATE shifts SET sync_id = ?, sync_version = ? WHERE id = ?", (sync_id, shift['version'], local['id']))
                cursor.execute("DELETE FROM shift_sync_outbox WHERE sync_id = ? AND state = 'pending'", (previous_sync_id,))
                cursor.execute("SELECT * FROM shifts WHERE id = ?", (local['id'],))
                local = cursor.fetchone()

        if local and local['entry_type'] != RECORD_TYPE_WORK:
            cursor.execute("SELECT 1 FROM shift_sync_outbox WHERE sync_id = ? AND state = 'blocked' LIMIT 1", (sync_id,))
            if cursor.fetchone():
                return

        remote_version = int(shift.get('version', 1))
        if local and remote_version > local["sync_version"] and shift.get("last_operation_id"):
            cursor.execute("""DELETE FROM shift_sync_outbox
                WHERE sync_id=? AND operation_id=? AND state='pending'""",
                (sync_id, shift["last_operation_id"]))
            if cursor.rowcount:
                cursor.execute("""UPDATE shift_sync_outbox SET base_version=?
                    WHERE id=(SELECT MIN(id) FROM shift_sync_outbox WHERE sync_id=? AND state='pending')""",
                    (remote_version, sync_id))
                cursor.execute("UPDATE shifts SET sync_version=? WHERE sync_id=?", (remote_version, sync_id))
                cursor.execute("SELECT * FROM shifts WHERE sync_id=?", (sync_id,))
                local = cursor.fetchone()
        cursor.execute("SELECT 1 FROM shift_sync_outbox WHERE sync_id = ? AND state = 'pending' LIMIT 1", (sync_id,))
        has_pending = cursor.fetchone() is not None
        if local and has_pending:
            if remote_version > local['sync_version']:
                cursor.execute("UPDATE shift_sync_outbox SET state = 'conflict' WHERE sync_id = ? AND state = 'pending'", (sync_id,))
            else:
                return

        record_type = shift.get('record_type') or shift.get('submission_type') or RECORD_TYPE_WORK
        if record_type not in (
            RECORD_TYPE_WORK, RECORD_TYPE_ATF, RECORD_TYPE_SICK,
            RECORD_TYPE_VACATION, RECORD_TYPE_HOLIDAY, RECORD_TYPE_OB,
        ):
            record_type = RECORD_TYPE_REVIEW
        if record_type == RECORD_TYPE_WORK:
            work_rate_cache = float(shift.get('rate', 0.0) or 0.0)
            work_transport_cache = float(shift.get('transport', 0.0) or 0.0)
        elif local:
            if local['entry_type'] == RECORD_TYPE_WORK:
                work_rate_cache = float(local['rate'] or 0.0)
                work_transport_cache = float(local['transport'] or 0.0)
            else:
                work_rate_cache = float(local['work_rate_cache'] or 0.0)
                work_transport_cache = float(local['work_transport_cache'] or 0.0)
        else:
            work_rate_cache = float(shift.get('rate', 0.0) or 0.0)
            work_transport_cache = float(shift.get('transport', 0.0) or 0.0)
        stored_rate = float(shift.get('rate', 0.0) or 0.0) if record_type == RECORD_TYPE_WORK else 0.0
        stored_transport = float(shift.get('transport', 0.0) or 0.0) if record_type in (RECORD_TYPE_WORK, RECORD_TYPE_OB) else 0.0
        values = (
            shift['date'], shift['employee'], shift['company'], shift['object_name'], shift['hours'],
            stored_rate, stored_transport, shift.get('comment', ''),
            int(bool(shift.get('invoiced', 0))) if record_type == RECORD_TYPE_WORK else 0,
            remote_version, record_type, work_rate_cache, work_transport_cache
        )
        needs_type_repair = local and (
            local['entry_type'] != record_type or
            (record_type == RECORD_TYPE_OB and (not local['start_time'] or not local['end_time']))
        )
        if local and remote_version <= local['sync_version'] and not needs_type_repair:
            return
        if ob_schema_ready(cursor):
            start_t = shift.get("start_time") if record_type == RECORD_TYPE_OB else None
            end_t = shift.get("end_time") if record_type == RECORD_TYPE_OB else None
            base_r = shift.get("ob_base_rate") if record_type == RECORD_TYPE_OB else None
            ob_values = (start_t, end_t, base_r)
            if local:
                cursor.execute('''
                    UPDATE shifts SET date = ?, employee = ?, company = ?, object_name = ?, hours = ?,
                        rate = ?, transport = ?, comment = ?, invoiced = ?, sync_version = ?, entry_type = ?,
                        work_rate_cache = ?, work_transport_cache = ?, start_time = ?, end_time = ?, ob_base_rate = ?
                    WHERE sync_id = ?
                ''', values + ob_values + (sync_id,))
            else:
                cursor.execute('''
                    INSERT INTO shifts
                        (date, employee, company, object_name, hours, rate, transport, comment, invoiced,
                         sync_id, sync_version, entry_type, work_rate_cache, work_transport_cache,
                         start_time, end_time, ob_base_rate)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', values[:-4] + (sync_id, remote_version, record_type, work_rate_cache, work_transport_cache) + ob_values)
        elif local:
            cursor.execute('''
                UPDATE shifts SET date = ?, employee = ?, company = ?, object_name = ?, hours = ?,
                    rate = ?, transport = ?, comment = ?, invoiced = ?, sync_version = ?, entry_type = ?,
                    work_rate_cache = ?, work_transport_cache = ?
                WHERE sync_id = ?
            ''', values + (sync_id,))
        else:
            cursor.execute('''
                INSERT INTO shifts
                    (date, employee, company, object_name, hours, rate, transport, comment, invoiced,
                     sync_id, sync_version, entry_type, work_rate_cache, work_transport_cache)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', values[:-4] + (sync_id, remote_version, record_type, work_rate_cache, work_transport_cache))
        if record_type == RECORD_TYPE_OB:
            segments = shift.get("ob_segments")
            if segments and segments != get_ob_segments(cursor, sync_id):
                replace_ob_segments(cursor, sync_id, segments)
            elif not segments and shift.get("start_time") and shift.get("end_time"):
                base_rate = float(shift.get("ob_base_rate") or 0.0)
                if base_rate <= 0:
                    try:
                        base_rate = get_local_employee_ob_base_rate(cursor, shift["employee"])
                    except Exception:
                        base_rate = 0.0
                try:
                    ob_result = calculate_work_interval(
                        shift["date"], shift["start_time"], shift["end_time"], base_rate
                    )
                    replace_ob_segments(cursor, sync_id, ob_result["segments"])
                except Exception as ex:
                    logger.warning(f"Не удалось рассчитать OB сегменты для смены {sync_id}: {ex}")
        elif ob_schema_ready(cursor):
            cursor.execute("DELETE FROM ob_segments WHERE shift_id = ?", (sync_id,))

    def queue_expense_document_operation(
        self, client_id, operation, payload, server_document_id=None,
        server_version=None, attachment_paths=None,
    ):
        if operation not in {"upsert", "delete"}:
            raise ValueError("Недопустимая операция расходного документа.")
        timestamp = datetime.now(timezone.utc).isoformat()
        conn = get_db_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT server_document_id, server_version, revision, attachment_paths_json "
                "FROM expense_document_outbox WHERE client_id = ?",
                (client_id,),
            ).fetchone()
            revision = int(existing["revision"] or 0) + 1 if existing else 1
            if server_document_id is None and existing:
                server_document_id = existing["server_document_id"]
            if server_version is None and existing:
                server_version = existing["server_version"]
            if attachment_paths is None and existing:
                attachment_paths_json = existing["attachment_paths_json"]
            else:
                attachment_paths_json = json.dumps(
                    [str(Path(path).resolve()) for path in (attachment_paths or [])],
                    ensure_ascii=False,
                )
            conn.execute("""
                INSERT INTO expense_document_outbox (
                    client_id, operation, server_document_id, server_version, revision, payload_json,
                    attachment_paths_json, state, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    operation = excluded.operation,
                    server_document_id = excluded.server_document_id,
                    server_version = excluded.server_version,
                    revision = excluded.revision,
                    payload_json = excluded.payload_json,
                    attachment_paths_json = excluded.attachment_paths_json,
                    state = 'pending', last_error = NULL, updated_at = excluded.updated_at
            """, (
                client_id, operation, server_document_id, server_version, revision,
                json.dumps(payload, ensure_ascii=False), attachment_paths_json,
                timestamp, timestamp,
            ))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return revision

    def _sync_expense_document_outbox(self):
        conn = get_db_connection()
        rows = conn.execute("""
            SELECT client_id, operation, server_document_id, server_version, revision,
                   payload_json, attachment_paths_json
            FROM expense_document_outbox
            WHERE state = 'pending'
            ORDER BY updated_at, client_id
        """).fetchall()
        conn.close()
        if rows:
            required_features = {
                "client_id", "soft_delete", "attachments",
                "nullable_company_allocations", "versioned_updates",
            }
            if any(json.loads(row["payload_json"]).get("order_proposals") for row in rows):
                required_features.update({"order_proposals", "supplier_project_code_links"})
            if any(json.loads(row["payload_json"]).get("allocation_basis") == "net" for row in rows):
                required_features.add("net_allocations_v1")
            if any("due_date" in json.loads(row["payload_json"]) for row in rows):
                required_features.add("due_date_v1")
            if any("payment_reference" in json.loads(row["payload_json"]) for row in rows):
                required_features.add("payment_reference_v1")
            if any(json.loads(row["payload_json"]).get("usage") == "object_cost" for row in rows):
                required_features.add("object_costs_v1")
            if any(json.loads(row["payload_json"]).get("usage") == "object_cost"
                   and len(json.loads(row["payload_json"]).get("allocations") or []) > 1 for row in rows):
                required_features.add("object_cost_allocations_v1")
            try:
                capability_response = requests.request(
                    "GET",
                    f"{CLOUD_URL.rstrip('/')}/api/expense-documents/capabilities",
                    headers=sync_headers(), timeout=5,
                )
                from sync_dataset import file_dataset, require_response_dataset
                require_response_dataset(capability_response, file_dataset(DB_FILE))
                capability_payload = capability_response.json()
                supported = (
                    capability_response.status_code == 200
                    and capability_payload.get("ok") is True
                    and int(capability_payload.get("api_version", 0)) >= 2
                    and required_features.issubset(set(capability_payload.get("features", [])))
                )
                capability_error = "Сервер расходов старый: обновите сервер до API v2; локальная очередь сохранена."
            except Exception as error:
                supported = False
                capability_error = f"Сервер расходов недоступен; локальная очередь сохранена: {error}"
            if not supported:
                error_conn = get_db_connection()
                try:
                    error_conn.execute("""
                        UPDATE expense_document_outbox
                        SET last_error = ?, updated_at = ?
                        WHERE state = 'pending'
                    """, (capability_error[:1000], datetime.now(timezone.utc).isoformat()))
                    error_conn.commit()
                finally:
                    error_conn.close()
                return {"synced": 0, "conflicts": 0, "blocked": len(rows)}
        synced = conflicts = cleanup_warnings = 0
        for queued in rows:
            client_id = queued["client_id"]
            revision = int(queued["revision"])
            payload = json.loads(queued["payload_json"])
            payload["client_id"] = client_id
            if queued["server_version"] is not None:
                payload["expected_version"] = int(queued["server_version"])
            request_files = []
            opened_files = []
            required_attachment_hashes = set(payload.get("required_attachment_sha256") or [])
            try:
                if queued["operation"] == "delete":
                    method = "DELETE"
                    endpoint = (
                        f"/api/expense-documents/{int(queued['server_document_id'])}"
                        if queued["server_document_id"] is not None
                        else f"/api/expense-documents/by-client/{client_id}"
                    )
                    data = None
                    files = None
                else:
                    method = "PUT" if queued["server_document_id"] is not None else "POST"
                    endpoint = (
                        f"/api/expense-documents/{int(queued['server_document_id'])}"
                        if queued["server_document_id"] is not None
                        else "/api/expense-documents"
                    )
                    attachment_paths = json.loads(queued["attachment_paths_json"] or "[]")
                    for attachment_path in attachment_paths:
                        path = Path(attachment_path)
                        if not path.is_file():
                            raise FileNotFoundError(f"Вложение расхода не найдено: {path.name}")
                        required_attachment_hashes.add(sha256_file(path))
                        stream = path.open("rb")
                        opened_files.append(stream)
                        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                        request_files.append(("originals", (path.name, stream, mime_type)))
                    files = request_files or None
                    data = {"payload": json.dumps(payload, ensure_ascii=False)} if files else None

                try:
                    response = requests.request(
                        method,
                        f"{CLOUD_URL.rstrip('/')}{endpoint}",
                        headers=sync_headers(),
                        json=payload if data is None else None,
                        data=data,
                        files=files,
                        timeout=30,
                    )
                finally:
                    for stream in opened_files:
                        stream.close()
                try:
                    result = response.json()
                except ValueError:
                    result = {}
                if not isinstance(result, dict):
                    raise ValueError("Некорректный ACK сервера; очередь и оригиналы сохранены для повтора.")
                status_code = int(response.status_code)
                if not 200 <= status_code < 300 or result.get("ok") is not True:
                    error = str(result.get("error") or f"Сервер вернул HTTP {status_code}.")
                    conflict = status_code in (400, 404, 409, 410, 422)
                    error_conn = get_db_connection()
                    try:
                        error_conn.execute("""
                            UPDATE expense_document_outbox
                            SET state = ?, server_version = COALESCE(?, server_version),
                                last_error = ?, updated_at = ?
                            WHERE client_id = ? AND revision = ?
                        """, (
                            "conflict" if conflict else "pending",
                            result.get("current_version"), error,
                            datetime.now(timezone.utc).isoformat(), client_id, revision,
                        ))
                        error_conn.commit()
                    finally:
                        error_conn.close()
                    conflicts += int(conflict)
                    continue

                remote_document = result.get("document") or {}
                if not isinstance(remote_document, dict):
                    raise ValueError("Сервер не подтвердил документ; очередь и оригиналы сохранены.")
                remote_id = remote_document.get("id")
                if queued["operation"] != "delete" and (
                    type(remote_id) is not int or remote_id <= 0
                    or remote_document.get("client_id") != client_id
                    or type(remote_document.get("sync_version")) is not int
                    or remote_document["sync_version"] <= 0
                    or queued["server_document_id"] is not None and remote_id != int(queued["server_document_id"])
                    or queued["server_version"] is not None and remote_document["sync_version"] <= int(queued["server_version"])
                ):
                    raise ValueError("Сервер не подтвердил идентификатор документа; оригинал и очередь сохранены для повтора.")
                if queued["operation"] != "delete" and required_attachment_hashes:
                    attachments = remote_document.get("attachments")
                    if not isinstance(attachments, list):
                        raise ValueError("В ACK отсутствует список обязательных вложений; очередь и оригиналы сохранены.")
                    confirmed_hashes = {item.get("sha256") for item in attachments
                                        if isinstance(item, dict) and item.get("has_original") is True
                                        and isinstance(item.get("sha256"), str)}
                    if (remote_document.get("has_original") is not True
                            or not required_attachment_hashes.issubset(confirmed_hashes)):
                        raise ValueError("ACK не подтверждает сохранение всех обязательных вложений по SHA-256; очередь и оригиналы сохранены.")
                if queued["operation"] != "delete" and "due_date" in payload:
                    expected_due = (datetime.strptime(str(payload["due_date"]), "%Y-%m-%d").strftime("%Y-%m-%d")
                                    if payload.get("due_date") else None)
                    if remote_document.get("due_date") != expected_due or remote_document.get("due_date_kind") != payload.get("due_date_kind", "manual" if expected_due else "absent"):
                        raise ValueError("ACK не подтвердил сохранение срока оплаты и его источника; очередь сохранена.")
                if queued["operation"] != "delete" and "payment_reference" in payload:
                    expected_reference = str(payload.get("payment_reference") or "").strip() or None
                    if remote_document.get("payment_reference") != expected_reference:
                        raise ValueError("ACK не подтвердил сохранение OCR/reference; очередь и оригинал сохранены.")
                if queued["operation"] != "delete" and payload.get("allocation_basis") == "net":
                    for key in ("amount_due", "rounding_amount"):
                        if decimal_text(parse_invoice_money(remote_document.get(key))) != decimal_text(parse_invoice_money(payload.get(key))):
                            raise ValueError(f"ACK не подтвердил {key}; очередь и оригинал сохранены.")
                ack_conn = get_db_connection()
                try:
                    ack_conn.execute("BEGIN IMMEDIATE")
                    current = ack_conn.execute(
                        "SELECT revision FROM expense_document_outbox WHERE client_id = ?",
                        (client_id,),
                    ).fetchone()
                    if current and int(current["revision"]) == revision:
                        ack_conn.execute(
                            "DELETE FROM expense_document_outbox WHERE client_id = ? AND revision = ?",
                            (client_id, revision),
                        )
                        paths_to_remove = json.loads(queued["attachment_paths_json"] or "[]")
                    else:
                        ack_conn.execute("""
                            UPDATE expense_document_outbox
                            SET server_document_id = COALESCE(?, server_document_id),
                                server_version = COALESCE(?, server_version),
                                updated_at = ?
                            WHERE client_id = ?
                        """, (
                            remote_id, remote_document.get("sync_version"),
                            datetime.now(timezone.utc).isoformat(), client_id,
                        ))
                        paths_to_remove = []
                    ack_conn.commit()
                except Exception:
                    ack_conn.rollback()
                    raise
                finally:
                    ack_conn.close()
                synced += 1
                for path in paths_to_remove:
                    try:
                        Path(path).unlink(missing_ok=True)
                    except OSError as error:
                        cleanup_warnings += 1
                        logging.warning("Документ %s подтверждён сервером; временный оригинал не удалён: %s", client_id, error)
            except (requests.RequestException, OSError, ValueError) as error:
                error_conn = get_db_connection()
                try:
                    error_conn.execute("""
                        UPDATE expense_document_outbox SET last_error = ?, updated_at = ?
                        WHERE client_id = ? AND revision = ? AND state = 'pending'
                    """, (
                        str(error)[:1000], datetime.now(timezone.utc).isoformat(),
                        client_id, revision,
                    ))
                    error_conn.commit()
                finally:
                    error_conn.close()
            finally:
                for stream in opened_files:
                    stream.close()
        result = {"synced": synced, "conflicts": conflicts}
        if cleanup_warnings:
            result["cleanup_warnings"] = cleanup_warnings
        return result

    def perform_sync_logic(self):
        if ISOLATED_CANDIDATE:
            raise RuntimeError("Синхронизация отключена в изолированном кандидате.")
        ensure_sync_dataset()
        headers = sync_headers()
        downloaded_ids = []
        typed_sync_supported = False
        vacation_sync_supported = False
        holiday_sync_supported = False
        ob_sync_supported = False
        schema_conn = get_db_connection()
        local_ob_enabled = ob_schema_ready(schema_conn.cursor())
        schema_conn.close()
        try:
            capabilities = requests.get(f"{CLOUD_URL}/sync/capabilities", headers=headers, timeout=4)
            if getattr(capabilities, 'status_code', None) == 200:
                capability_data = capabilities.json()
                submission_types = set(capability_data.get('submission_types', []))
                typed_sync_supported = (
                    {RECORD_TYPE_WORK, RECORD_TYPE_ATF, RECORD_TYPE_SICK}.issubset(submission_types)
                    and capability_data.get('typed_tombstones') is True
                )
                vacation_sync_supported = (
                    RECORD_TYPE_VACATION in submission_types
                    and capability_data.get('typed_tombstones') is True
                )
                holiday_sync_supported = (
                    RECORD_TYPE_HOLIDAY in submission_types
                    and capability_data.get('typed_tombstones') is True
                )
                ob_sync_supported = (
                    "ob" in set(capability_data.get('submission_types', []))
                    and capability_data.get('typed_tombstones') is True
                )
        except Exception:
            typed_sync_supported = False
            vacation_sync_supported = False
            holiday_sync_supported = False
            ob_sync_supported = False

        response = requests.get(f"{CLOUD_URL}/get-unsynced", headers=headers, timeout=4)
        response.raise_for_status()
        downloaded = response.json().get('shifts', [])
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            for shift in downloaded:
                remote_type = shift.get('record_type', shift.get('submission_type', RECORD_TYPE_WORK))
                if remote_type == RECORD_TYPE_OB and not local_ob_enabled:
                    continue
                self.apply_remote_shift(cursor, shift)
                downloaded_ids.append(shift['id'])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if downloaded_ids:
            acknowledged = requests.post(
                f"{CLOUD_URL}/mark-synced", headers=headers, json={"ids": downloaded_ids}, timeout=4
            )
            acknowledged.raise_for_status()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM shift_sync_state WHERE name = 'cursor'")
        current_cursor = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM shifts WHERE (entry_type = 'ob' AND (start_time IS NULL OR end_time IS NULL)) OR (entry_type = 'work' AND (rate = 0 OR rate IS NULL) AND (transport = 0 OR transport IS NULL))")
        has_suspicious_or_incomplete = cursor.fetchone()[0] > 0
        conn.close()

        manual_requested = bool(getattr(self, '__dict__', {}).get('_manual_sync_requested', False))
        pull_since = 0 if (manual_requested or has_suspicious_or_incomplete) else current_cursor
        response = requests.get(
            f"{CLOUD_URL}/sync/pull", headers=headers, params={"since": pull_since}, timeout=4
        )
        response.raise_for_status()
        pulled = response.json()
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            blocked_ob_sequences = []
            for shift in pulled.get('changes', []):
                remote_type = shift.get('record_type', shift.get('submission_type', RECORD_TYPE_WORK))
                if remote_type == RECORD_TYPE_OB and not local_ob_enabled:
                    blocked_ob_sequences.append(int(shift.get('change_seq', current_cursor + 1)))
                    continue
                self.apply_remote_shift(cursor, shift)
            next_cursor = pulled.get('cursor', current_cursor)
            if blocked_ob_sequences:
                next_cursor = max(current_cursor, min(blocked_ob_sequences) - 1)
            cursor.execute("UPDATE shift_sync_state SET value = ? WHERE name = 'cursor'", (next_cursor,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        if not ob_sync_supported:
            cursor.execute("SELECT id, payload_json FROM shift_sync_outbox WHERE state = 'pending'")
            for outbox_row in cursor.fetchall():
                if json.loads(outbox_row['payload_json']).get('record_type') == RECORD_TYPE_OB:
                    cursor.execute("UPDATE shift_sync_outbox SET state = 'blocked' WHERE id = ?", (outbox_row['id'],))
        if not vacation_sync_supported:
            cursor.execute("SELECT id, payload_json FROM shift_sync_outbox WHERE state = 'pending'")
            for outbox_row in cursor.fetchall():
                if json.loads(outbox_row['payload_json']).get('record_type') == RECORD_TYPE_VACATION:
                    cursor.execute("UPDATE shift_sync_outbox SET state = 'blocked' WHERE id = ?", (outbox_row['id'],))
        if not holiday_sync_supported:
            cursor.execute("SELECT id, payload_json FROM shift_sync_outbox WHERE state = 'pending'")
            for outbox_row in cursor.fetchall():
                if json.loads(outbox_row['payload_json']).get('record_type') == RECORD_TYPE_HOLIDAY:
                    cursor.execute("UPDATE shift_sync_outbox SET state = 'blocked' WHERE id = ?", (outbox_row['id'],))
        if typed_sync_supported or vacation_sync_supported or holiday_sync_supported or ob_sync_supported:
            allowed_types = set()
            if typed_sync_supported:
                allowed_types.update((RECORD_TYPE_WORK, RECORD_TYPE_ATF, RECORD_TYPE_SICK))
            if vacation_sync_supported:
                allowed_types.add(RECORD_TYPE_VACATION)
            if holiday_sync_supported:
                allowed_types.add(RECORD_TYPE_HOLIDAY)
            if ob_sync_supported:
                allowed_types.add(RECORD_TYPE_OB)
            compact_blocked_shift_operations(cursor, allowed_types)
        cursor.execute('''
            SELECT o.operation_id, o.sync_id, o.operation, o.base_version, o.payload_json
            FROM shift_sync_outbox o
            WHERE o.state = 'pending'
            ORDER BY o.id
        ''')
        pending_rows = cursor.fetchall()
        conn.commit()
        conn.close()
        changes = []
        seen_sync_ids = set()
        for row in pending_rows:
            if row['sync_id'] in seen_sync_ids:
                continue
            seen_sync_ids.add(row['sync_id'])
            changes.append({
                "operation_id": row['operation_id'], "sync_id": row['sync_id'],
                "operation": row['operation'], "base_version": row['base_version'],
                "shift": json.loads(row['payload_json'])
            })

        conflicts = 0
        sync_conflict_reasons = {}
        if changes:
            pushed = requests.post(
                f"{CLOUD_URL}/sync/push", headers=headers, json={"changes": changes}, timeout=4
            )
            raise_for_sync_response(pushed)
            results = {result['operation_id']: result for result in pushed.json().get('results', [])}
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                for change in changes:
                    result = results.get(change['operation_id'], {})
                    if result.get('ok'):
                        version = int(result.get('version', change['base_version'] + 1))
                        cursor.execute("UPDATE shifts SET sync_version = ? WHERE sync_id = ?", (version, change['sync_id']))
                        cursor.execute("DELETE FROM shift_sync_outbox WHERE operation_id = ?", (change['operation_id'],))
                        cursor.execute('''
                            UPDATE shift_sync_outbox SET base_version = ?
                            WHERE sync_id = ? AND state = 'pending'
                            AND id = (SELECT MIN(id) FROM shift_sync_outbox WHERE sync_id = ? AND state = 'pending')
                        ''', (version, change['sync_id'], change['sync_id']))
                    elif result.get('conflict'):
                        conflicts += 1
                        reason_labels = {
                            "operation_id_reused": "operation_id смены повторно использован с другими данными",
                            "deleted": "смена уже удалена на сервере",
                            "stale_version": "на сервере есть более новая версия смены",
                            "missing_record": "серверная смена отсутствует для указанной версии",
                        }
                        reason = reason_labels.get(
                            result.get("reason"), "сервер отклонил изменение смены"
                        )
                        sync_conflict_reasons[reason] = sync_conflict_reasons.get(reason, 0) + 1
                        cursor.execute("UPDATE shift_sync_outbox SET state = 'conflict' WHERE operation_id = ?", (change['operation_id'],))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        catalog_response = requests.get(
            f"{CLOUD_URL}/api/expense-objects?include_inactive=1",
            headers=headers,
            timeout=4,
        )
        catalog_response.raise_for_status()
        catalog_result = catalog_response.json()
        if not isinstance(catalog_result, dict) or catalog_result.get("ok") is not True:
            raise RuntimeError("Сервер не вернул справочник объектов для синхронизации.")
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            catalog_stats = merge_server_object_catalog(cursor, catalog_result.get("objects"))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        conflicts += catalog_stats["conflicts"]
        for reason, count in catalog_stats["conflict_reasons"].items():
            sync_conflict_reasons[reason] = sync_conflict_reasons.get(reason, 0) + count

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, COALESCE(salary_rate, 0.0) AS salary_rate, is_active FROM employees ORDER BY name")
        employees = [
            {
                "name": row["name"],
                "salary_rate": float(row["salary_rate"]),
                "is_active": int(row["is_active"]),
            }
            for row in cursor.fetchall()
        ]
        cursor.execute("SELECT operation_id, employee_name, employee_key, revision FROM employee_sync_outbox ORDER BY created_at, operation_id")
        deleted_employees = [
            {
                "operation_id": row["operation_id"], "name": row["employee_name"],
                "employee_key": row["employee_key"] or employee_sync_key(row["employee_name"]),
                "revision": int(row["revision"] or 0),
            }
            for row in cursor.fetchall()
        ]
        cursor.execute("SELECT operation_id, employee_name, employee_key, salary_rate, revision FROM employee_restore_outbox ORDER BY created_at, operation_id")
        restored_employees = [
            {
                "operation_id": row["operation_id"], "name": row["employee_name"],
                "employee_key": row["employee_key"],
                "salary_rate": float(row["salary_rate"]) if row["salary_rate"] is not None else None,
                "revision": int(row["revision"] or 0),
            }
            for row in cursor.fetchall()
        ]
        cursor.execute("SELECT name FROM companies ORDER BY name")
        companies = [row[0] for row in cursor.fetchall()]
        cursor.execute("SELECT operation_id, payload_json FROM object_sync_outbox ORDER BY id")
        object_events = [json.loads(row["payload_json"]) for row in cursor.fetchall()]
        conn.close()
        metadata_payload = {
            "employees": employees, "deleted_employees": deleted_employees,
            "restored_employees": restored_employees,
            "companies": companies, "objects": [], "object_events": object_events, "shifts": [],
        }
        if self.__dict__.get("_manual_sync_requested", False):
            print(
                f"[SYNC] POST {CLOUD_URL}/sync-desktop-data employees="
                f"{json.dumps(employees, ensure_ascii=False)}",
                flush=True,
            )
        metadata_response = requests.post(
            f"{CLOUD_URL}/sync-desktop-data", headers=headers,
            json=metadata_payload, timeout=4
        )
        try:
            metadata_response.raise_for_status()
        except requests.HTTPError as error:
            try:
                reason = metadata_response.json().get("error")
            except (ValueError, AttributeError):
                reason = None
            raise RuntimeError(f"Сервер отклонил изменение справочника: {reason or error}") from error
        metadata_result = metadata_response.json()
        if not isinstance(metadata_result, dict) or metadata_result.get("ok") is not True:
            raise RuntimeError("Сервер не подтвердил синхронизацию справочников.")
        expected_delete_ids = {item["operation_id"] for item in deleted_employees}
        acknowledged_delete_ids = set(metadata_result.get("deleted_employee_operations", []))
        if not expected_delete_ids.issubset(acknowledged_delete_ids):
            raise RuntimeError("Сервер не подтвердил удаление сотрудника; удаление оставлено в очереди.")
        expected_restore_ids = {item["operation_id"] for item in restored_employees}
        acknowledged_restore_ids = set(metadata_result.get("restored_employee_operations", []))
        if not expected_restore_ids.issubset(acknowledged_restore_ids):
            raise RuntimeError("Сервер не подтвердил восстановление сотрудника; операция оставлена в очереди.")
        employee_events = [*restored_employees, *deleted_employees]
        employee_event_results = metadata_result.get("employee_event_results", [])
        if not isinstance(employee_event_results, list):
            raise RuntimeError("Ответ сервера по событиям сотрудников имеет неверный формат.")
        employee_results_by_id = {
            item.get("operation_id"): item
            for item in employee_event_results if isinstance(item, dict)
        }
        expected_employee_ids = {item["operation_id"] for item in employee_events}
        if not expected_employee_ids.issubset(employee_results_by_id):
            raise RuntimeError("Сервер не подтвердил состояние сотрудника; события оставлены в очереди.")
        if employee_events:
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                for event in employee_events:
                    result = employee_results_by_id[event["operation_id"]]
                    if result.get("stale") and not result.get("equivalent"):
                        conflicts += 1
                        reason = "revision сотрудника на сервере опережает локальную"
                        sync_conflict_reasons[reason] = sync_conflict_reasons.get(reason, 0) + 1
                    apply_employee_sync_ack(cursor, event, result)
                    if event in restored_employees:
                        cursor.execute(
                            "DELETE FROM employee_restore_outbox WHERE operation_id = ? AND employee_key = ?",
                            (event["operation_id"], event["employee_key"]),
                        )
                    else:
                        cursor.execute(
                            "DELETE FROM employee_sync_outbox WHERE operation_id = ? AND employee_name = ?",
                            (event["operation_id"], event["name"]),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        expected_object_operation_ids = {event["operation_id"] for event in object_events}
        object_results = metadata_result.get("object_sync_operations", [])
        if not isinstance(object_results, list):
            raise RuntimeError("Ответ сервера по событиям объектов имеет неверный формат.")
        object_results_by_id = {
            item.get("operation_id"): item for item in object_results if isinstance(item, dict)
        }
        if not expected_object_operation_ids.issubset(object_results_by_id):
            raise RuntimeError("Сервер не подтвердил все изменения объектов; события оставлены в очереди.")
        if object_events:
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                for event in object_events:
                    result = object_results_by_id[event["operation_id"]]
                    if result.get("stale"):
                        if not result.get("equivalent"):
                            conflicts += 1
                            reason = "revision или состояние объекта изменились на сервере"
                            sync_conflict_reasons[reason] = sync_conflict_reasons.get(reason, 0) + 1
                        state = result.get("object")
                        if isinstance(state, dict):
                            cursor.execute("""
                                UPDATE objects
                                SET server_id = ?, name = ?, markning = ?, rate = ?, transport_rate = ?,
                                    is_active = ?, sync_version = ?
                                WHERE id = ? AND sync_id = ? AND sync_version = ?
                            """, (
                                result.get("server_object_id"), state.get("name"), state.get("markning") or "",
                                float(state.get("rate") or 0.0), float(state.get("transport_rate") or 0.0),
                                int(bool(state.get("is_active"))), int(result.get("revision", event["revision"])),
                                int(event["object_id"]), event["sync_id"], int(event["revision"]),
                            ))
                        else:
                            cursor.execute("""
                                UPDATE objects SET server_id = NULL, is_active = 0, sync_version = ?
                                WHERE id = ? AND sync_id = ? AND sync_version = ?
                            """, (
                                int(result.get("revision", event["revision"])),
                                int(event["object_id"]), event["sync_id"], int(event["revision"]),
                            ))
                    elif event["operation"] == "link_legacy" and isinstance(result.get("object"), dict):
                        state = result["object"]
                        cursor.execute("""
                            UPDATE objects
                            SET server_id = ?, rate = ?, transport_rate = ?, is_active = ?
                            WHERE id = ? AND sync_id = ? AND sync_version = ?
                        """, (
                            result.get("server_object_id"), float(state.get("rate") or 0.0),
                            float(state.get("transport_rate") or 0.0), int(bool(state.get("is_active"))),
                            int(event["object_id"]), event["sync_id"], int(event["revision"]),
                        ))
                    else:
                        cursor.execute(
                            "UPDATE objects SET server_id = ? WHERE id = ? AND sync_id = ?",
                            (result.get("server_object_id"), int(event["object_id"]), event["sync_id"]),
                        )
                    cursor.execute("DELETE FROM object_sync_outbox WHERE operation_id = ?", (event["operation_id"],))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        employee_catalog_response = requests.get(
            f"{CLOUD_URL}/api/sync/employee-catalog",
            headers=headers,
            timeout=4,
        )
        employee_catalog_response.raise_for_status()
        employee_catalog_result = employee_catalog_response.json()
        if not isinstance(employee_catalog_result, dict) or employee_catalog_result.get("ok") is not True:
            raise RuntimeError("Сервер не вернул справочник сотрудников для синхронизации.")
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            employee_catalog_stats = merge_server_employee_catalog(
                cursor, employee_catalog_result.get("employees")
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        expense_sync_result = self._sync_expense_document_outbox()
        self._expense_read_dirty = getattr(self, "_expense_read_dirty", False) or bool(expense_sync_result.get("synced") or expense_sync_result.get("conflicts"))
        conflicts += expense_sync_result["conflicts"]
        if expense_sync_result["conflicts"]:
            sync_conflict_reasons["конфликт расходных документов"] = expense_sync_result["conflicts"]
        conflict_details = "; ".join(
            f"{reason}: {count}" for reason, count in sync_conflict_reasons.items()
        )
        unresolved_legacy_details = "; ".join(
            f"{reason}: {count}"
            for reason, count in catalog_stats["unresolved_reasons"].items()
        )
        return {
            "conflicts": conflicts,
            "linked_legacy": catalog_stats["linked"],
            "unresolved_legacy": catalog_stats["unresolved_legacy"],
            "imported_employees": employee_catalog_stats["imported"],
            "unresolved_employees": employee_catalog_stats["unresolved"],
            "conflict_details": conflict_details,
            "unresolved_legacy_details": unresolved_legacy_details,
        }

    def _on_sync_completed(
        self, success, conflicts, linked_legacy, unresolved_legacy,
        imported_employees, unresolved_employees, conflict_details,
        unresolved_legacy_details, error,
    ):
        self._sync_worker = None
        if success:
            self.load_shifts_history()
        if getattr(self, "tabs", None) and self.tabs.currentIndex() == 3 and hasattr(self, "table_supplier_documents"):
            if getattr(self, "_async_expense_reads", False) and (getattr(self, "_expense_read_dirty", False) or self._manual_sync_requested):
                self._expense_read_dirty = False
                self._invalidate_expense_reads()
                self._schedule_expense_read((self.exp_date_from.text().strip(), self.exp_date_to.text().strip()))
                self.exp_api_status_lbl.setText("Изменения синхронизированы; обновление реестра в фоне.")
            else:
                self.load_expense_documents()
                if self.exp_document_tabs.currentIndex() == 1:
                    self.load_object_expense_overview()
        if getattr(self, "_incoming_viewer", None) is not None:
            self._schedule_expense_read((None, None))
        if getattr(self, "_object_edit_sync_pending", False):
            if not success or conflicts:
                self._object_edit_sync_pending = False
                if not self._manual_sync_requested:
                    reason = conflict_details if conflicts else error
                    message = (
                        "Сервер вернул конфликт; проверьте актуальное состояние объекта."
                        if success else "Изменение сохранено локально и остаётся в очереди синхронизации."
                    )
                    QMessageBox.warning(self, "Изменение объекта", f"{message}\nПричина: {reason}")
            else:
                conn = get_db_connection()
                pending = conn.execute("SELECT 1 FROM object_sync_outbox LIMIT 1").fetchone()
                conn.close()
                self._object_edit_sync_pending = pending is not None
        if self._manual_sync_requested:
            self._manual_sync_requested = False
            if success and conflicts:
                QMessageBox.warning(
                    self, "Конфликт синхронизации",
                    f"Есть конфликтующих изменений: {conflicts}. "
                    f"Причины: {conflict_details or 'сервер отклонил часть изменений; проверьте версии и состояние записей'}. "
                    f"Связано legacy-объектов: {linked_legacy}; "
                    f"не сопоставлено: {unresolved_legacy}; "
                    f"импортировано сотрудников: {imported_employees}; "
                    f"не сопоставлено сотрудников: {unresolved_employees}.",
                )
            elif success and (unresolved_legacy or unresolved_employees):
                QMessageBox.warning(
                    self, "Legacy-объекты",
                    f"Автоматически не сопоставлено legacy-объектов: {unresolved_legacy}. "
                    f"Причины: {unresolved_legacy_details or 'нет безопасного точного соответствия'}. "
                    f"Не сопоставлено сотрудников: {unresolved_employees}.",
                )
            elif success and (linked_legacy or imported_employees):
                QMessageBox.information(
                    self, "Синхронизация",
                    f"Синхронизация завершена. Связано legacy-объектов: {linked_legacy}; "
                    f"импортировано сотрудников: {imported_employees}.",
                )
            elif success:
                QMessageBox.information(self, "Синхронизация", "Синхронизация завершена.")
            else:
                server_rejection = str(error).startswith("Сервер отклонил синхронизацию (HTTP ")
                title = "Ошибка синхронизации" if server_rejection else "Нет связи"
                message = (
                    f"Изменения сохранены локально; сервер отклонил их:\n{error}"
                    if server_rejection
                    else f"Изменения сохранены локально и будут отправлены после восстановления связи.\n{error}"
                )
                QMessageBox.warning(self, title, message)

    def sync_with_cloud(self):
        self._manual_sync_requested = True
        self.background_sync_with_cloud()

    def background_sync_with_cloud(self):
        if ISOLATED_CANDIDATE:
            self.statusBar().showMessage("Сохранено локально. Синхронизация в кандидате отключена.")
            self._manual_sync_requested = False
            return
        if self._sync_worker and self._sync_worker.isRunning():
            return
        self._sync_worker = SyncWorker(self)
        self._sync_worker.completed.connect(self._on_sync_completed)
        self._sync_worker.start()

    def setup_salary_tab(self):
        layout = QVBoxLayout(self.tab_salary)
        total_card = QFrame()
        total_card.setObjectName("card")
        total_card.setStyleSheet("QFrame#card { background-color: #242933; border: 1px solid #4299e1; border-radius: 8px; margin-bottom: 5px; }")
        t_layout = QHBoxLayout(total_card)
        t_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        
        self.sal_summary_hours_lbl = QLabel("<b>Рабочие часы: 0.0</b>")
        self.sal_summary_vacation_lbl = QLabel("<b>Отпуск: 0.0 tim</b>")
        self.sal_summary_ob_lbl = QLabel("<b>OB: 0 tim / 0.00 SEK</b>")
        self.sal_summary_net_lbl = QLabel("<b>Nettolön: 0.00 SEK</b>")
        self.sal_summary_employer_lbl = QLabel("<b>Arbetskostnad: 0.00 SEK</b>")

        t_layout.addWidget(self.sal_summary_hours_lbl)
        t_layout.addWidget(self.sal_summary_vacation_lbl)
        t_layout.addWidget(self.sal_summary_ob_lbl)
        t_layout.addWidget(self.sal_summary_net_lbl)
        t_layout.addWidget(self.sal_summary_employer_lbl)
        layout.addWidget(total_card)

        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        period_layout = QHBoxLayout()

        period_layout.addWidget(QLabel("<b>Период:</b>"))
        self.sal_date_from = create_date_field("2026-10-01")
        self.sal_date_to = create_date_field("2026-10-31")
        calc_btn = QPushButton("🔄 Рассчитать зарплату")
        calc_btn.clicked.connect(self.calculate_salary)

        period_layout.addWidget(self.sal_date_from)
        period_layout.addWidget(QLabel("по"))
        period_layout.addWidget(self.sal_date_to)
        period_layout.addWidget(calc_btn)

        export_salary_pdf_btn = QPushButton("🖨 Экспорт зарплат в PDF")
        export_salary_pdf_btn.setObjectName("secondaryAction")
        export_salary_pdf_btn.clicked.connect(self.export_salary_to_pdf)
        period_layout.addWidget(export_salary_pdf_btn)
        period_layout.addStretch()
        card_layout.addLayout(period_layout)

        settings_layout = QHBoxLayout()
        settings_layout.addWidget(QLabel("Semesterersättning %"))
        self.sal_vacation_percent = QDoubleSpinBox()
        self.sal_vacation_percent.setRange(0, 100)
        self.sal_vacation_percent.setDecimals(2)
        self.sal_vacation_percent.setValue(13.0)
        self.sal_vacation_percent.setSuffix(" %")
        settings_layout.addWidget(self.sal_vacation_percent)

        settings_layout.addWidget(QLabel("Preliminär skatt %"))
        self.sal_tax_percent = QDoubleSpinBox()
        self.sal_tax_percent.setRange(0, 100)
        self.sal_tax_percent.setDecimals(2)
        self.sal_tax_percent.setValue(30.06)
        self.sal_tax_percent.setSuffix(" %")
        settings_layout.addWidget(self.sal_tax_percent)

        settings_layout.addWidget(QLabel("Arbetsgivaravgifter %"))
        self.sal_employer_fee_percent = QDoubleSpinBox()
        self.sal_employer_fee_percent.setRange(0, 100)
        self.sal_employer_fee_percent.setDecimals(2)
        self.sal_employer_fee_percent.setValue(31.42)
        self.sal_employer_fee_percent.setSuffix(" %")
        settings_layout.addWidget(self.sal_employer_fee_percent)

        settings_layout.addWidget(QLabel("ATF/KLS %"))
        self.sal_atf_percent = QDoubleSpinBox()
        self.sal_atf_percent.setRange(0, 100)
        self.sal_atf_percent.setDecimals(2)
        self.sal_atf_percent.setValue(0.0)
        self.sal_atf_percent.setSuffix(" %")
        self.sal_atf_percent.setToolTip("Задайте внутреннюю долю оплаты ATF/KLS; 0% означает, что автоматическая доплата не задана.")
        settings_layout.addWidget(self.sal_atf_percent)

        settings_layout.addWidget(QLabel("Sjuklön %"))
        self.sal_sick_percent = QDoubleSpinBox()
        self.sal_sick_percent.setRange(0, 100)
        self.sal_sick_percent.setDecimals(2)
        self.sal_sick_percent.setValue(0.0)
        self.sal_sick_percent.setSuffix(" %")
        self.sal_sick_percent.setToolTip("Задайте внутреннюю долю оплаты больничных; 0% означает, что автоматическая доплата не задана.")
        settings_layout.addWidget(self.sal_sick_percent)
        settings_layout.addStretch()
        card_layout.addLayout(settings_layout)
        for rate_control in (
            self.sal_vacation_percent, self.sal_tax_percent,
            self.sal_employer_fee_percent, self.sal_atf_percent,
            self.sal_sick_percent,
        ):
            rate_control.valueChanged.connect(lambda _value: self.calculate_salary())

        layout.addWidget(card)
        
        self.table_salary = QTableWidget()
        self.table_salary.setColumnCount(17)
        self.table_salary.setHorizontalHeaderLabels([
            "Anställd", "Arbete\ntim", "ATF\ntim", "Sjukfrånvaro\ntim", "Timlön\n(SEK)",
            "OB timmar /\nspecifikation", "Сверх нормы, ч", "Grundlön + OB\n(SEK)", "Semesterersättning\n(SEK)",
            "ATF/KLS\n(SEK)", "Sjuklön\n(SEK)", "Skatt\n(SEK)", "Nettolön\n(SEK)",
            "Arbetsgivaravgifter\n(SEK)", "Total kostnad\n(SEK)", "Semestertimmar\ntim",
            "Röda dagar\ntim",
        ])
        self.table_salary.horizontalHeaderItem(6).setToolTip(
            "Информационно: работа + OB минус месячная норма "
            "(8 часов за будний день без официальных шведских праздников). "
            "АТФ, больничные, отпуск и праздничные нерабочие часы не учитываются. "
            "Для всех сотрудников применяется 100% ставки на полный месяц."
        )
        salary_header = self.table_salary.horizontalHeader()
        salary_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        salary_header.setMinimumHeight(46)
        for column, width in {
            0: 140, 1: 75, 2: 75, 3: 75, 4: 85, 5: 200, 6: 95,
            7: 110, 8: 120, 9: 90, 10: 90, 11: 95, 12: 100,
            13: 120, 14: 110, 15: 105, 16: 95,
        }.items():
            self.table_salary.setColumnWidth(column, width)
        self.table_salary.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table_salary.setWordWrap(True)
        self.table_salary.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table_salary.setAlternatingRowColors(True)
        self.table_salary.cellChanged.connect(self.on_salary_cell_changed)
        self.table_salary.installEventFilter(self)
        layout.addWidget(self.table_salary)
        QTimer.singleShot(0, self.resize_salary_table_columns)

    def eventFilter(self, watched, event):
        if getattr(self, "table_salary", None) is watched and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self.resize_salary_table_columns)
        return False

    def resize_salary_table_columns(self):
        if not hasattr(self, "table_salary"):
            return
        self.table_salary.schedule_layout()

    def load_salary_table_template(self):
        try:
            self.table_salary.cellChanged.disconnect(self.on_salary_cell_changed)
        except TypeError:
            pass

        d_from = self.sal_date_from.text().strip()
        d_to = self.sal_date_to.text().strip()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, COALESCE(salary_rate, 0.0) FROM employees ORDER BY name")
        emp_rows = cursor.fetchall()
        
        rates_map = {str(row[0]).strip().lower(): row[1] for row in emp_rows}
        orig_names = {str(row[0]).strip().lower(): row[0] for row in emp_rows}

        has_ob_schema = ob_schema_ready(cursor)
        ob_details_by_employee = collect_invoice_ob_details(
            cursor, None, None, d_from, d_to
        ) if has_ob_schema else {}
        if has_ob_schema:
            cursor.execute("""
                SELECT employee,
                    SUM(CASE WHEN entry_type = 'work' THEN hours ELSE 0 END),
                    SUM(CASE WHEN entry_type = 'atf' THEN hours ELSE 0 END),
                    SUM(CASE WHEN entry_type = 'sick' THEN hours ELSE 0 END),
                    SUM(CASE WHEN entry_type = 'vacation' THEN hours ELSE 0 END),
                    SUM(CASE WHEN entry_type = 'holiday' THEN hours ELSE 0 END),
                    SUM(CASE WHEN entry_type = 'ob' THEN hours ELSE 0 END),
                    COALESCE((
                        SELECT SUM(segment.amount)
                        FROM ob_segments segment
                        JOIN shifts ob_shift ON ob_shift.sync_id = segment.shift_id
                        WHERE ob_shift.employee = shifts.employee
                          AND ob_shift.date >= ? AND ob_shift.date <= ?
                          AND ob_shift.entry_type = 'ob'
                    ), 0)
                FROM shifts
                WHERE date >= ? AND date <= ?
                GROUP BY employee
            """, (d_from, d_to, d_from, d_to))
        else:
            cursor.execute("""
                SELECT employee,
                    SUM(CASE WHEN entry_type = 'work' THEN hours ELSE 0 END),
                    SUM(CASE WHEN entry_type = 'atf' THEN hours ELSE 0 END),
                    SUM(CASE WHEN entry_type = 'sick' THEN hours ELSE 0 END),
                    SUM(CASE WHEN entry_type = 'vacation' THEN hours ELSE 0 END),
                    SUM(CASE WHEN entry_type = 'holiday' THEN hours ELSE 0 END),
                    SUM(CASE WHEN entry_type = 'ob' THEN hours ELSE 0 END), 0
                FROM shifts
                WHERE date >= ? AND date <= ?
                GROUP BY employee
            """, (d_from, d_to))
        shift_rows = cursor.fetchall()
        conn.close()

        sorted_keys = sorted(list(rates_map.keys()))
        hours_map = {
            str(row[0]).strip().lower(): (
                row[1], row[2], row[3], row[4], row[5], row[6], row[7]
            )
            for row in shift_rows
        }

        self.table_salary.setRowCount(0)
        visible_row_idx = 0
        sum_hrs_all = 0.0
        sum_atf_all = 0.0
        sum_sick_all = 0.0
        sum_vacation_all = 0.0
        sum_ob_hours_all = 0.0
        sum_ob_amount_all = 0.0
        sum_net_all = 0.0
        sum_employer_cost_all = 0.0
        monthly_norm = swedish_workday_norm(d_from, d_to)

        for row_idx, key in enumerate(sorted_keys):
            emp_display_name = orig_names.get(key, key.title())
            hrs, atf_hours, sick_hours, vacation_hours, holiday_hours, ob_hours, ob_amount = hours_map.get(
                key, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            )
            salary_rate = rates_map.get(key, 0.0)
            sum_hrs_all += hrs
            sum_atf_all += atf_hours
            sum_sick_all += sick_hours
            sum_vacation_all += vacation_hours
            sum_ob_hours_all += ob_hours
            sum_ob_amount_all += ob_amount

            payroll = calculate_payroll_components(
                hrs, atf_hours, sick_hours, salary_rate, ob_amount,
                self.sal_vacation_percent.value(), self.sal_tax_percent.value(),
                self.sal_employer_fee_percent.value(), self.sal_atf_percent.value(),
                self.sal_sick_percent.value(),
            )
            sum_net_all += payroll["net_pay"]
            sum_employer_cost_all += payroll["employer_total_cost"]
            overtime_hours = max(
                0.0, hrs + ob_hours - monthly_norm
            )

            has_activity = any((
                hrs, atf_hours, sick_hours, vacation_hours, holiday_hours, ob_hours, ob_amount,
                *payroll.values(),
            ))
            if not has_activity:
                continue

            row_idx = visible_row_idx
            visible_row_idx += 1
            self.table_salary.setRowCount(visible_row_idx)
            item_emp = QTableWidgetItem(str(emp_display_name))
            item_emp.setFlags(item_emp.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table_salary.setItem(row_idx, 0, item_emp)

            item_hrs = QTableWidgetItem(str(hrs))
            item_hrs.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item_hrs.setFlags(item_hrs.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table_salary.setItem(row_idx, 1, item_hrs)

            for column, hours_value in ((2, atf_hours), (3, sick_hours)):
                item = QTableWidgetItem(f"{hours_value:.1f}")
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table_salary.setItem(row_idx, column, item)
            item_rate = QTableWidgetItem(f"{salary_rate:.2f}")
            item_rate.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table_salary.setItem(row_idx, 4, item_rate)

            ob_details = ob_details_by_employee.get(emp_display_name, {})
            ob_spec = QTableWidgetItem(
                f"{ob_hours:g} tim" + (f"\n{ob_details['display']}" if ob_details else "")
            )
            ob_spec.setData(Qt.ItemDataRole.UserRole, ob_hours)
            ob_spec.setData(Qt.ItemDataRole.UserRole + 1, ob_amount)
            if ob_details:
                ob_spec.setToolTip(ob_details["tooltip"])
            ob_spec.setFlags(ob_spec.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table_salary.setItem(row_idx, 5, ob_spec)

            overtime_item = QTableWidgetItem(f"{overtime_hours:.1f}")
            overtime_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            overtime_item.setFlags(overtime_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table_salary.setItem(row_idx, 6, overtime_item)

            payroll_values = (
                payroll["base_salary"], payroll["vacation_pay"], payroll["atf_pay"],
                payroll["sick_pay"], payroll["tax"], payroll["net_pay"],
                payroll["employer_fee"], payroll["employer_total_cost"],
            )
            for col_i, val in enumerate(payroll_values, start=7):
                item_res = QTableWidgetItem(f"{val:.2f}")
                item_res.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                item_res.setFlags(item_res.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table_salary.setItem(row_idx, col_i, item_res)
            for col_i, hours_value in ((15, vacation_hours), (16, holiday_hours)):
                item = QTableWidgetItem(f"{hours_value:.1f}")
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table_salary.setItem(row_idx, col_i, item)

        self.sal_summary_hours_lbl.setText(f"<b>Рабочие часы: {sum_hrs_all:.1f}</b>")
        self.sal_summary_vacation_lbl.setText(f"<b>Отпуск: {sum_vacation_all:.1f} tim</b>")
        self.sal_summary_ob_lbl.setText(f"<b>OB: {sum_ob_hours_all:g} tim / {sum_ob_amount_all:,.2f} SEK</b>")
        self.sal_summary_net_lbl.setText(f"<b>Nettolön: {sum_net_all:,.2f} SEK</b>")
        self.sal_summary_employer_lbl.setText(f"<b>Arbetskostnad: {sum_employer_cost_all:,.2f} SEK</b>")
        self.table_salary.cellChanged.connect(self.on_salary_cell_changed)

    def on_salary_cell_changed(self, row, col):
        if col == 4:
            emp_item = self.table_salary.item(row, 0)
            rate_item = self.table_salary.item(row, 4)
            if not emp_item or not rate_item:
                return
            emp_name = emp_item.text().strip()
            new_rate = parse_float(rate_item.text(), 0.0)

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO employees (name, salary_rate) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET salary_rate = ?
            ''', (emp_name, new_rate, new_rate))
            conn.commit()
            conn.close()
            
            self.calculate_salary_row(row)
            self.background_sync_with_cloud()

    def calculate_salary_row(self, row_idx):
        hrs_item = self.table_salary.item(row_idx, 1)
        atf_item = self.table_salary.item(row_idx, 2)
        sick_item = self.table_salary.item(row_idx, 3)
        rate_item = self.table_salary.item(row_idx, 4)
        ob_item = self.table_salary.item(row_idx, 5)
        if not hrs_item or not atf_item or not sick_item or not rate_item or not ob_item:
            return
        hrs = parse_float(hrs_item.text(), 0.0)
        atf_hours = parse_float(atf_item.text(), 0.0)
        sick_hours = parse_float(sick_item.text(), 0.0)
        salary_rate = parse_float(rate_item.text(), 0.0)
        ob_hours = float(ob_item.data(Qt.ItemDataRole.UserRole) or 0.0)
        ob_amount = float(ob_item.data(Qt.ItemDataRole.UserRole + 1) or 0.0)
        payroll = calculate_payroll_components(
            hrs, atf_hours, sick_hours, salary_rate, ob_amount,
            self.sal_vacation_percent.value(), self.sal_tax_percent.value(),
            self.sal_employer_fee_percent.value(), self.sal_atf_percent.value(),
            self.sal_sick_percent.value(),
        )

        try:
            self.table_salary.cellChanged.disconnect(self.on_salary_cell_changed)
        except TypeError:
            pass

        row_values = (
            payroll["base_salary"], payroll["vacation_pay"], payroll["atf_pay"],
            payroll["sick_pay"], payroll["tax"], payroll["net_pay"],
            payroll["employer_fee"], payroll["employer_total_cost"],
        )
        for c_idx, val in enumerate(row_values, start=7):
            it = QTableWidgetItem(f"{val:.2f}")
            it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table_salary.setItem(row_idx, c_idx, it)

        self.table_salary.cellChanged.connect(self.on_salary_cell_changed)
        
        sum_hrs = sum_atf = sum_sick = sum_vacation = sum_ob_hrs = sum_ob_amount = 0.0
        sum_net = sum_employer_total = 0.0
        for r in range(self.table_salary.rowCount()):
            h_it = self.table_salary.item(r, 1)
            atf_it = self.table_salary.item(r, 2)
            sick_it = self.table_salary.item(r, 3)
            vacation_it = self.table_salary.item(r, 15)
            ob_it = self.table_salary.item(r, 5)
            n_it = self.table_salary.item(r, 12)
            employer_it = self.table_salary.item(r, 14)
            if h_it: sum_hrs += parse_float(h_it.text(), 0.0)
            if atf_it: sum_atf += parse_float(atf_it.text(), 0.0)
            if sick_it: sum_sick += parse_float(sick_it.text(), 0.0)
            if vacation_it: sum_vacation += parse_float(vacation_it.text(), 0.0)
            if n_it: sum_net += parse_float(n_it.text(), 0.0)
            if employer_it: sum_employer_total += parse_float(employer_it.text(), 0.0)
            if ob_it:
                sum_ob_hrs += float(ob_it.data(Qt.ItemDataRole.UserRole) or 0.0)
                sum_ob_amount += float(ob_it.data(Qt.ItemDataRole.UserRole + 1) or 0.0)
            
        self.sal_summary_hours_lbl.setText(f"<b>Рабочие часы: {sum_hrs:.1f}</b>")
        self.sal_summary_vacation_lbl.setText(f"<b>Отпуск: {sum_vacation:.1f} tim</b>")
        self.sal_summary_ob_lbl.setText(f"<b>OB: {sum_ob_hrs:g} tim / {sum_ob_amount:,.2f} SEK</b>")
        self.sal_summary_net_lbl.setText(f"<b>Nettolön: {sum_net:,.2f} SEK</b>")
        self.sal_summary_employer_lbl.setText(f"<b>Arbetskostnad: {sum_employer_total:,.2f} SEK</b>")

    def calculate_salary(self):
        self.load_salary_table_template()

    def export_salary_to_pdf(self):
        self.load_salary_table_template()
        d_from = self.sal_date_from.text().strip()
        d_to = self.sal_date_to.text().strip()
        if self.table_salary.rowCount() == 0:
            QMessageBox.warning(self, "Löneunderlag", "Нет сотрудников или данных для выбранного периода.")
            return

        os.makedirs(REPORTS_DIR, exist_ok=True)
        base_filename = f"Loneunderlag_{d_from}_till_{d_to}.pdf"
        filepath = os.path.join(REPORTS_DIR, base_filename)
        try:
            styles = getSampleStyleSheet()
            title_style = ParagraphStyle(
                "SalaryReportTitle", parent=styles["Heading1"],
                fontName=DEFAULT_FONT_BOLD, fontSize=16, spaceAfter=6,
            )
            meta_style = ParagraphStyle(
                "SalaryReportMeta", parent=styles["Normal"],
                fontName=DEFAULT_FONT, fontSize=8, leading=11, spaceAfter=3,
            )
            header_style = ParagraphStyle(
                "SalaryReportHeader", parent=styles["Normal"],
                fontName=DEFAULT_FONT_BOLD, fontSize=7, leading=8, wordWrap="CJK",
            )
            cell_style = ParagraphStyle(
                "SalaryReportCell", parent=styles["Normal"],
                fontName=DEFAULT_FONT, fontSize=7, leading=9, wordWrap="CJK",
            )
            labels = [
                "Anställd", "Arbete tim", "ATF tim", "Sjukfrånvaro tim", "Timlön (SEK)",
                "OB timmar / specifikation", "Сверх нормы, ч", "Grundlön + OB (SEK)",
                "Semesterersättning (SEK)", "ATF/KLS (SEK)", "Sjuklön (SEK)",
                "Skatt (SEK)", "Nettolön (SEK)", "Arbetsgivaravgifter (SEK)",
                "Total kostnad (SEK)", "Semestertimmar", "Röda dagar",
            ]
            table_data = [[Paragraph(escape(label), header_style) for label in labels]]
            totals = [0.0] * 17
            for row_idx in range(self.table_salary.rowCount()):
                row_values = []
                for column in range(self.table_salary.columnCount()):
                    item = self.table_salary.item(row_idx, column)
                    text = item.text() if item else ""
                    if column in (0, 5):
                        if column == 0:
                            row_values.append(Paragraph(escape(text), cell_style))
                        else:
                            row_values.append(Paragraph(escape(text).replace("\n", "<br/>"), cell_style))
                    else:
                        numeric_value = parse_float(text, 0.0)
                        row_values.append(f"{numeric_value:.2f}" if column >= 4 else f"{numeric_value:g}")
                        if column in (1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16):
                            totals[column] += numeric_value
                table_data.append(row_values)

            total_ob_hours = sum(
                float(self.table_salary.item(row, 5).data(Qt.ItemDataRole.UserRole) or 0.0)
                for row in range(self.table_salary.rowCount())
                if self.table_salary.item(row, 5)
            )
            total_ob_amount = sum(
                float(self.table_salary.item(row, 5).data(Qt.ItemDataRole.UserRole + 1) or 0.0)
                for row in range(self.table_salary.rowCount())
                if self.table_salary.item(row, 5)
            )
            totals_row = ["Totalt:"] + [f"{totals[column]:.2f}" for column in range(1, 17)]
            totals_row[4] = ""
            totals_row[5] = f"OB totalt: {total_ob_hours:g} tim / {total_ob_amount:,.2f} SEK"
            table_data.append([
                Paragraph(escape(str(value)), cell_style) if index in (0, 5) else value
                for index, value in enumerate(totals_row)
            ])

            table = LongTable(
                table_data,
                colWidths=[
                    75, 30, 24, 30, 36, 150, 34, 45, 45, 38, 38, 38,
                    40, 45, 45, 34, 34,
                ],
                repeatRows=1,
            )
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2b6cb0")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("FONTNAME", (0, 0), (-1, -1), DEFAULT_FONT),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("GRID", (0, 0), (-1, -2), 0.35, colors.HexColor("#b8c2cc")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.HexColor("#f7f9fb"), colors.white]),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e8edf2")),
                ("FONTNAME", (0, -1), (-1, -1), DEFAULT_FONT_BOLD),
                ("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#2b6cb0")),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ]))
            document = SimpleDocTemplate(
                filepath, pagesize=landscape(A4),
                rightMargin=25, leftMargin=25, topMargin=25, bottomMargin=25,
                title=f"Löneunderlag {d_from}–{d_to}",
            )
            story = [
                Paragraph("Löneunderlag", title_style),
                Paragraph(f"Period: {escape(d_from)} – {escape(d_to)}", meta_style),
                Paragraph(
                    "Semesterersättning: " + f"{self.sal_vacation_percent.value():.2f}%" +
                    " · Preliminär skatt: " + f"{self.sal_tax_percent.value():.2f}%" +
                    " · Arbetsgivaravgifter: " + f"{self.sal_employer_fee_percent.value():.2f}%" +
                    " · ATF/KLS: " + f"{self.sal_atf_percent.value():.2f}%" +
                    " · Sjuklön: " + f"{self.sal_sick_percent.value():.2f}%",
                    meta_style,
                ),
                Spacer(1, 6), table,
            ]
            document.build(story)
            QMessageBox.information(self, "Löneunderlag", f"PDF sparad:\n{base_filename}")
            if sys.platform == "win32":
                os.startfile(filepath)
            elif sys.platform == "darwin":
                subprocess.run(["open", filepath])
            else:
                subprocess.run(["xdg-open", filepath])
        except Exception as error:
            QMessageBox.critical(self, "Löneunderlag", f"Kunde inte skapa PDF: {error}")

    def setup_balance_tab(self):
        layout = QVBoxLayout(self.tab_balance)
        layout.setSpacing(10)

        total_card = QFrame()
        total_card.setObjectName("card")
        total_card.setStyleSheet("QFrame#card { background-color: #242933; border: 1px solid #4299e1; border-radius: 8px; }")
        
        total_layout = QVBoxLayout(total_card)
        total_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        total_title = QLabel("💰 ОБЩИЙ ИТОГ (БАЛАНС ПО ВСЕМ СОТРУДНИКАМ):")
        total_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        total_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #63b3ed; border: none;")
        
        self.total_sum_label = QLabel("0.00 kr")
        self.total_sum_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.total_sum_label.setStyleSheet("font-size: 26px; font-weight: bold; color: #ffffff; border: none;")
        
        total_layout.addWidget(total_title)
        total_layout.addWidget(self.total_sum_label)
        layout.addWidget(total_card)

        summary_card = QFrame()
        summary_card.setObjectName("card")
        summary_layout = QVBoxLayout(summary_card)
        summary_layout.addWidget(QLabel("<b>📌 Текущий баланс (долг) по сотрудникам:</b>"))
        
        self.table_bal_summary = QTableWidget()
        self.table_bal_summary.setColumnCount(2)
        self.table_bal_summary.setHorizontalHeaderLabels(["Сотрудник", "Баланс / Долг (SEK)"])
        self.table_bal_summary.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_bal_summary.setAlternatingRowColors(True)
        self.table_bal_summary.setMinimumHeight(180)
        summary_layout.addWidget(self.table_bal_summary)
        layout.addWidget(summary_card)

        card = QFrame()
        card.setObjectName("card")
        card_layout = QFormLayout(card)
        card_layout.setSpacing(8)

        self.bal_date = create_date_field(QDate.currentDate().toString("yyyy-MM-dd"))
        self.bal_emp_cb = QComboBox()
        self.bal_emp_cb.setEditable(True)
        self.bal_desc = QLineEdit()
        self.bal_desc.setPlaceholderText("Например: Выдан аванс / Зарплата")
        self.bal_amount = QLineEdit()
        self.bal_amount.setPlaceholderText("Сумма (+ долг, - выплата)")

        card_layout.addRow("Дата:", self.bal_date)
        card_layout.addRow("Сотрудник:", self.bal_emp_cb)
        card_layout.addRow("Описание:", self.bal_desc)
        card_layout.addRow("Изменение (SEK):", self.bal_amount)

        self.add_balance_button = QPushButton("Добавить операцию")
        self.add_balance_button.clicked.connect(self.add_balance_entry)
        card_layout.addRow(self.add_balance_button)
        layout.addWidget(card)

        layout.addWidget(QLabel("<b>История операций баланса:</b>"))
        self.table_balance = QTableWidget()
        self.table_balance.setColumnCount(5)
        self.table_balance.setHorizontalHeaderLabels(["Дата", "Сотрудник", "Описание", "Изменение (SEK)", "Действие"])
        self.table_balance.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table_balance.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table_balance.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table_balance.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table_balance.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table_balance.setAlternatingRowColors(True)
        layout.addWidget(self.table_balance)
        delete_button = QPushButton("Удалить выбранную операцию")
        delete_button.setObjectName("danger")
        delete_button.clicked.connect(self.delete_balance_entry)
        layout.addWidget(delete_button)

    def add_balance_entry(self):
        date = self.bal_date.text().strip()
        emp = self.bal_emp_cb.currentText()
        desc = self.bal_desc.text().strip()
        amt = parse_float(self.bal_amount.text(), None)
        if amt is None:
            QMessageBox.warning(self, "Баланс", "Введите корректную сумму.")
            return
        if self.__dict__.get("_editing_balance_id") is not None:
            return self.update_balance_entry()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO balance (date, employee, description, change_amount) VALUES (?, ?, ?, ?)', (date, emp, desc, amt))
        conn.commit()
        conn.close()
        self.bal_desc.clear()
        self.bal_amount.clear()
        self.load_balance_table()
        return True
    def load_balance_table(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT employee, SUM(change_amount) as total_bal
            FROM balance
            GROUP BY employee
            HAVING SUM(change_amount) != 0
            ORDER BY SUM(change_amount) DESC
        ''')
        summary_rows = cursor.fetchall()
        self.table_bal_summary.setRowCount(len(summary_rows))
        grand_total = 0.0
        for r_idx, row in enumerate(summary_rows):
            emp, total = row[0], row[1]
            grand_total += total
            self.table_bal_summary.setItem(r_idx, 0, QTableWidgetItem(str(emp)))
            self.table_bal_summary.setItem(r_idx, 1, QTableWidgetItem(f"{total:,.2f} kr"))

        self.total_sum_label.setText(f"{grand_total:,.2f} kr")
        cursor.execute("SELECT id, date, employee, description, change_amount FROM balance ORDER BY id DESC")
        rows = cursor.fetchall()
        conn.close()
        self.table_balance.setRowCount(len(rows))
        for row_idx, row in enumerate(rows):
            balance_id, date, employee, description, amount = row
            date_item = QTableWidgetItem(str(date))
            date_item.setData(Qt.ItemDataRole.UserRole, int(balance_id))
            self.table_balance.setItem(row_idx, 0, date_item)
            self.table_balance.setItem(row_idx, 1, QTableWidgetItem(str(employee)))
            self.table_balance.setItem(row_idx, 2, QTableWidgetItem(str(description)))
            self.table_balance.setItem(row_idx, 3, QTableWidgetItem(f"{float(amount):,.2f} SEK"))
            edit_button = QPushButton("Изменить")
            edit_button.clicked.connect(lambda _checked=False, row_id=int(balance_id): self.edit_balance_entry(row_id))
            self.table_balance.setCellWidget(row_idx, 4, edit_button)
    def _api_load_expense_document_filters(self, *_):
        object_selected = self.exp_filter_object.currentData() if hasattr(self, "exp_filter_object") else None
        supplier_selected = self.exp_filter_supplier.currentData() if hasattr(self, "exp_filter_supplier") else None
        try:
            object_result = request_expense_api("GET", "/api/expense-objects")
            document_result = request_expense_api("GET", "/api/expense-documents")
            objects = object_result["objects"]
            suppliers = document_result["suppliers"]
            self._expense_objects = objects
            self._expense_suppliers = suppliers
            self.exp_api_status_lbl.setText("Общий реестр доступен. Локальные документы не переносились.")
        except Exception as error:
            self.exp_api_status_lbl.setText(f"Общий реестр недоступен: {error}")
            return

        self.exp_filter_object.blockSignals(True)
        self.exp_filter_object.clear()
        self.exp_filter_object.addItem("Все объекты", None)
        for row in objects:
            label = str(row["name"])
            if row.get("markning"):
                label += f" ({row['markning']})"
            if row.get("company"):
                label += f" · {row['company']}"
            self.exp_filter_object.addItem(label, int(row["id"]))
        object_index = self.exp_filter_object.findData(object_selected)
        self.exp_filter_object.setCurrentIndex(max(0, object_index))
        self.exp_filter_object.blockSignals(False)

        self.exp_filter_supplier.blockSignals(True)
        self.exp_filter_supplier.clear()
        self.exp_filter_supplier.addItem("Все поставщики", None)
        for supplier_name in suppliers:
            self.exp_filter_supplier.addItem(str(supplier_name), str(supplier_name))
        supplier_index = self.exp_filter_supplier.findData(supplier_selected)
        self.exp_filter_supplier.setCurrentIndex(max(0, supplier_index))
        self.exp_filter_supplier.blockSignals(False)

    def _api_load_expense_documents(self, *_):
        if not hasattr(self, "table_supplier_documents"):
            return
        date_from = self.exp_date_from.text().strip()
        date_to = self.exp_date_to.text().strip()
        self.exp_period_label.setText(f"Архив за {date_from} — {date_to}; фильтр по дате документа")
        if date_from > date_to:
            for label in (self.exp_docs_net_lbl, self.exp_docs_vat_lbl, self.exp_docs_gross_lbl, self.exp_docs_unallocated_lbl):
                label.setText("0.00")
            self.exp_docs_review_lbl.setText("0")
            self.table_supplier_documents.setRowCount(0)
            return

        params = {"date_from": date_from, "date_to": date_to}
        object_id = self.exp_filter_object.currentData()
        if object_id is not None:
            params["object_id"] = int(object_id)
        supplier_name = self.exp_filter_supplier.currentData()
        try:
            result = request_expense_api("GET", "/api/expense-documents", params=params)
            rows = result["documents"]
        except Exception as error:
            self.exp_api_status_lbl.setText(f"Не удалось загрузить общий архив: {error}")
            self.table_supplier_documents.setRowCount(0)
            return
        if supplier_name:
            rows = [row for row in rows if row["supplier_name"] == supplier_name]

        net_totals: dict[str, Decimal] = {}
        vat_totals: dict[str, Decimal] = {}
        gross_totals: dict[str, Decimal] = {}
        unallocated_totals: dict[str, Decimal] = {}
        review_count = 0
        for row in rows:
            currency = str(row["currency_code"] or "UNKNOWN")
            if row["verification_status"] != "confirmed":
                review_count += 1
            else:
                for field, totals in (("net_amount", net_totals), ("vat_amount", vat_totals), ("gross_amount", gross_totals)):
                    amount = parse_decimal(row[field])
                    if amount is not None:
                        totals[currency] = totals.get(currency, Decimal(0)) + amount
            for field, totals in (("unallocated_gross", unallocated_totals),):
                amount = parse_decimal(row[field])
                if amount is not None:
                    totals[currency] = totals.get(currency, Decimal(0)) + amount

        self.exp_docs_net_lbl.setText(self._format_currency_totals(net_totals))
        self.exp_docs_vat_lbl.setText(self._format_currency_totals(vat_totals))
        self.exp_docs_gross_lbl.setText(self._format_currency_totals(gross_totals))
        self.exp_docs_unallocated_lbl.setText(self._format_currency_totals(unallocated_totals))
        self.exp_docs_review_lbl.setText(str(review_count))

        self.table_supplier_documents.setRowCount(len(rows))
        verification_labels = {
            "draft": "Черновик", "requires_review": "Требует проверки", "confirmed": "Подтверждён",
        }
        for row_index, row in enumerate(rows):
            object_names = sorted({str(item["object_name"]) for item in row["allocations"]})
            object_label = ", ".join(object_names) if object_names else "Без объекта"
            values = (
                row["id"], row["document_date"] or "—", row["supplier_name"] or "Не указан",
                row["invoice_number"] or "—", object_label,
                f"{row['net_amount']} {row['currency_code']}" if row["net_amount"] else "—",
                f"{row['vat_amount']} {row['currency_code']}" if row["vat_amount"] else "—",
                f"{row['gross_amount']} {row['currency_code']}" if row["gross_amount"] else "—",
                f"{row['unallocated_gross']} {row['currency_code']}" if row["unallocated_gross"] else "0.00",
                verification_labels.get(str(row["verification_status"]), str(row["verification_status"])),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column in (2, 3, 4, 9):
                    item.setToolTip(str(value))
                self.table_supplier_documents.setItem(row_index, column, item)
            self.table_supplier_documents.item(row_index, 0).setData(Qt.ItemDataRole.UserRole, row)
        self.exp_docs_warning_lbl.clear()
        self.exp_docs_warning_lbl.hide()

    def _api_upload_expense_document(self, *_):
        path, _ = QFileDialog.getOpenFileName(
            self, "Загрузить счёт или чек", "",
            "Документы (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp)",
        )
        if not path:
            return
        try:
            extraction = extract_local_document(path)
            fields = parse_document_text(extraction.text)
            page_texts = extraction.pages or (extraction.text,)
            allocation_proposal = parse_project_order_allocations([
                {"source": Path(path).name, "page": page + 1, "text": page_text}
                for page, page_text in enumerate(page_texts)
            ])
        except Exception as error:
            QMessageBox.warning(self, "Документ не прочитан", str(error))
            return
        fields["raw_text"] = extraction.text
        fields["expense_category"] = fields.get("expense_category") or "Прочие накладные расходы"
        fields["ocr_method"] = extraction.method
        fields["ocr_confidence"] = extraction.confidence
        fields["order_proposals"] = allocation_proposal["orders"]
        fields["order_proposal_warnings"] = allocation_proposal["warnings"]
        fields["order_proposal_requires_review"] = allocation_proposal["requires_review"]
        fields["client_id"] = str(uuid.uuid4())
        if extraction.confidence is not None and extraction.confidence < 0.65:
            fields["review_reasons"].append("низкая уверенность OCR; проверьте все поля")
        suggestions = match_objects(extraction.text, self._expense_object_candidates())
        self._api_expense_document_review_dialog(None, path, fields, suggestions, None)

    def _download_expense_original(self, document):
        if not has_sync_credentials():
            raise RuntimeError("Не задан ключ доступа к общему реестру документов: выполните вход.")
        document_id = int(document["id"])
        suffix = Path(document.get("original_name") or "document.pdf").suffix.casefold()
        if suffix not in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}:
            suffix = ".pdf"
        response = requests.get(
            f"{CLOUD_URL.rstrip('/')}/api/expense-documents/{document_id}/original",
            headers=sync_headers(), timeout=30, stream=True,
        )
        response.raise_for_status()
        path = Path(self._expense_temp_directory.name) / f"{document_id}{suffix}"
        with path.open("wb") as destination:
            shutil.copyfileobj(response.raw, destination)
        return str(path)

    def _api_expense_document_review_dialog(
        self, document_id, source_path, fields, suggestions, existing_allocations,
        source_paths=None, preview_paths=None,
    ):
        non_invoice_type = fields.get("non_invoice_document_type") or non_invoice_document_type(
            str(fields.get("raw_text") or fields.get("recognized_text") or "")
        )
        new_paths = list(source_paths if source_paths is not None else ([source_path] if source_path else []))
        paths = list(preview_paths or new_paths)
        dialog = QDialog(self)
        dialog.setWindowTitle("Фактура и распределение" if document_id is None else f"Фактура #{document_id}")
        dialog.resize(1120, 760)
        dialog.setWindowFlags(
            dialog.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        dialog.setSizeGripEnabled(True)
        layout = QVBoxLayout(dialog)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)
        is_transfer_receipt = (
            fields.get("document_type") == "transfer_receipt"
            or str(fields.get("expense_category") or "").strip().casefold() == "квитанция перевода"
        )
        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 4, 0)
        preview_controls = QHBoxLayout()
        preview_view = None
        preview_pixmap = QPixmap()
        if source_path:
            try:
                preview_pixmap = load_document_preview_pixmap(source_path)
            except Exception as error:
                preview_error = str(error)
            else:
                preview_error = "Предпросмотр оригинала недоступен."
        else:
            preview_error = "Оригинал не загружен."

        if not preview_pixmap.isNull():
            preview_view = ZoomableDocumentView(preview_pixmap, preview_panel)
            preview_view.setMinimumWidth(280)
            preview_layout.addWidget(preview_view, 1)
            for label, callback in (
                ("−", lambda: preview_view.zoom(1 / 1.2)),
                ("По ширине", preview_view.fit_width),
                ("100%", preview_view.show_original_size),
                ("+", lambda: preview_view.zoom(1.2)),
            ):
                button = QPushButton(label)
                button.clicked.connect(callback)
                preview_controls.addWidget(button)

            def open_large_preview():
                DocumentGalleryDialog(paths, Path(source_path).name, dialog).exec()

            open_large_button = QPushButton("Открыть отдельно")
            open_large_button.setToolTip("Открыть оригинал в отдельном изменяемом окне")
            open_large_button.clicked.connect(open_large_preview)
            preview_controls.addWidget(open_large_button)
            preview_controls.addStretch()
            preview_layout.insertLayout(0, preview_controls)
        else:
            preview_message = QLabel(preview_error)
            preview_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview_message.setWordWrap(True)
            preview_layout.addWidget(preview_message, 1)
        splitter.addWidget(preview_panel)

        form_widget = QWidget()
        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setWidget(form_widget)
        form = QFormLayout(form_widget)
        supplier = QComboBox()
        supplier.setEditable(True)
        supplier.addItems(getattr(self, "_expense_suppliers", []))
        supplier.setCurrentText(str(fields.get("supplier_name") or ""))
        invoice_number = QLineEdit(str(fields.get("invoice_number") or ""))
        document_date = QLineEdit(str(fields.get("document_date") or ""))
        document_date.setPlaceholderText("YYYY-MM-DD")
        currency = QComboBox()
        currency.setEditable(True)
        currency.addItems(["SEK", "EUR", "NOK", "DKK", "USD", "GBP", "CHF", "PLN", "UNKNOWN"])
        currency.setCurrentText(str(fields.get("currency_code") or "SEK"))
        net_amount = QLineEdit(str(fields.get("net_amount") or ""))
        vat_amount = QLineEdit(str(fields.get("vat_amount") or ""))
        gross_amount = QLineEdit(str(fields.get("gross_amount") or ""))
        category = QComboBox()
        category.setEditable(True)
        category.addItems(["Материалы", "Услуги", "Аренда", "Транспорт", "Инструменты", "Прочие расходы", "Квитанция перевода"])
        category.setCurrentText(str(fields.get("expense_category") or "Прочие расходы"))
        document_comment = QTextEdit(str(fields.get("document_comment") or ""))
        document_comment.setMaximumHeight(80)
        form.addRow("Поставщик:", supplier)
        form.addRow("Номер документа:", invoice_number)
        form.addRow("Дата документа:", document_date)
        form.addRow("Валюта:", currency)
        form.addRow("Сумма без НДС:", net_amount)
        form.addRow("НДС:", vat_amount)
        form.addRow("Сумма с НДС:", gross_amount)
        form.addRow("Категория:", category)
        form.addRow("Комментарий:", document_comment)
        if is_transfer_receipt:
            transfer_note = QLabel(
                "Распознана квитанция перевода. Дата и сумма взяты из текста; "
                "Lunar не подставлен как поставщик, НДС не рассчитывался. Запись требует проверки."
            )
            transfer_note.setWordWrap(True)
            form.addRow("Проверка документа:", transfer_note)
        if non_invoice_type:
            note = QLabel(
                "Подтверждение заказа нельзя подтвердить как расход. Загрузите фактуру или чек."
                if non_invoice_type == "order_confirmation"
                else "Решение о транспортном налоге не является фактурой; получатель не считается поставщиком."
            )
            note.setWordWrap(True)
            form.addRow("Тип документа:", note)

        recognized_text = QTextEdit(str(fields.get("raw_text") or fields.get("recognized_text") or ""))
        recognized_text.setReadOnly(True)
        recognized_text.setMaximumHeight(120)
        form.addRow("Текст распознавания:", recognized_text)
        reasons = "; ".join(fields.get("review_reasons") or [])
        if reasons:
            reason_label = QLabel(reasons)
            reason_label.setWordWrap(True)
            form.addRow("Проверьте:", reason_label)

        objects = list(getattr(self, "_expense_objects", []))
        order_proposals = [dict(item) for item in fields.get("order_proposals") or []]
        order_table = QTableWidget()
        order_table.setObjectName("expenseOrderProposals")
        order_table.setColumnCount(7)
        order_table.setHorizontalHeaderLabels([
            "Proj.nr.", "Order ID", "Объект", "Net", "VAT заказа", "Gross", "Источник",
        ])
        order_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        order_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        order_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        try:
            supplier_links = request_expense_api(
                "GET", "/api/expense-project-code-links",
                params={"supplier_name": supplier.currentText().strip()},
            ).get("links", [])
        except Exception:
            supplier_links = []
        mapped_objects = {
            str(link.get("project_code") or "").casefold(): int(link["object_id"])
            for link in supplier_links if link.get("object_id") is not None
        }

        def add_order_proposal(proposal):
            signals_blocked = order_table.blockSignals(True)
            row = order_table.rowCount()
            order_table.insertRow(row)
            order_table.setItem(row, 0, QTableWidgetItem(str(proposal.get("project_code") or "")))
            order_table.setItem(row, 1, QTableWidgetItem(str(proposal.get("order_id") or "")))
            object_combo = QComboBox()
            object_combo.addItem("Выберите объект", None)
            for object_row in objects:
                label = str(object_row["name"])
                if object_row.get("markning"):
                    label += f" ({object_row['markning']})"
                if object_row.get("company"):
                    label += f" · {object_row['company']}"
                object_combo.addItem(label, int(object_row["id"]))
            selected_object = proposal.get("object_id") or mapped_objects.get(
                str(proposal.get("project_code") or "").casefold()
            )
            if selected_object is None:
                exact_matches = [
                    int(item["id"]) for item in objects
                    if str(item.get("markning") or "").strip().casefold()
                    == str(proposal.get("project_code") or "").strip().casefold()
                    and str(proposal.get("project_code") or "").strip()
                ]
                if len(exact_matches) == 1:
                    selected_object = exact_matches[0]
            if selected_object is not None:
                selected_index = object_combo.findData(int(selected_object))
                if selected_index >= 0:
                    object_combo.setCurrentIndex(selected_index)
            order_table.setCellWidget(row, 2, object_combo)
            order_table.setItem(row, 3, QTableWidgetItem(str(proposal.get("net_amount") or "")))
            order_table.setItem(row, 4, QTableWidgetItem(str(proposal.get("vat_amount") or "")))
            gross_item = QTableWidgetItem("")
            gross_item.setFlags(gross_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            order_table.setItem(row, 5, gross_item)
            sources = proposal.get("sources") or []
            page = int(sources[0].get("page") or 1) if sources else 1
            source_pages = ", ".join(dict.fromkeys(str(source.get("page")) for source in sources))
            attachment_index = int(sources[0].get("attachment_index") or 0) if sources else 0
            source_button = QPushButton(f"Стр. {source_pages}" if sources else "Нет страницы")
            source_button.setEnabled(bool(paths and sources and 0 <= attachment_index < len(paths)))
            if sources:
                source_button.setToolTip("\n".join(
                    f"{source.get('source')}, стр. {source.get('page')}, строка {source.get('line')}: {source.get('text') or ''}"
                    for source in sources
                ))
            if paths and sources and 0 <= attachment_index < len(paths):
                source_button.clicked.connect(
                    lambda _checked=False, page_number=page, file_index=attachment_index: DocumentGalleryDialog(
                        paths, f"Источник · страница {page_number}", dialog,
                        initial_page=page_number - 1, initial_attachment=file_index,
                    ).exec()
                )
            order_table.setCellWidget(row, 6, source_button)
            order_table.blockSignals(signals_blocked)

        for proposal in order_proposals:
            add_order_proposal(proposal)
        proposal_status = QLabel()
        proposal_status.setWordWrap(True)
        proposal_warnings = list(fields.get("order_proposal_warnings") or [])

        def update_order_proposal_totals(*args):
            if len(args) == 2 and args[1] == 5:
                return
            net_total = Decimal(0)
            vat_total = Decimal(0)
            gross_total = Decimal(0)
            missing_vat = []
            for row in range(order_table.rowCount()):
                net_item = order_table.item(row, 3)
                vat_item = order_table.item(row, 4)
                net_value = parse_decimal(net_item.text() if net_item else None)
                vat_value = parse_decimal(vat_item.text() if vat_item else None)
                gross_item = order_table.item(row, 5)
                gross_value = net_value + vat_value if net_value is not None and vat_value is not None else None
                gross_item.setText(decimal_text(gross_value) or "")
                if net_value is not None:
                    net_total += net_value
                if vat_value is not None:
                    vat_total += vat_value
                else:
                    missing_vat.append(str(order_table.item(row, 1).text() or row + 1))
                if gross_value is not None:
                    gross_total += gross_value
            messages = list(proposal_warnings)
            if missing_vat:
                messages.append("VAT не указан для заказов: " + ", ".join(missing_vat))
            if order_table.rowCount():
                messages.append(
                    f"Итого предложения: net {decimal_text(net_total)}, VAT {decimal_text(vat_total)}, "
                    f"gross {decimal_text(gross_total)}"
                )
            proposal_status.setText("\n".join(messages))

        order_table.cellChanged.connect(update_order_proposal_totals)
        update_order_proposal_totals()
        if order_proposals:
            form.addRow("Распределение по заказам:", order_table)
            form.addRow("Проверка заказов:", proposal_status)
        orders_reviewed = QCheckBox("Строки заказов и VAT сверены с оригиналом")
        orders_reviewed.setObjectName("expenseOrdersReviewed")
        order_buttons = QHBoxLayout()
        add_order_button = QPushButton("Добавить заказ")
        add_order_button.setObjectName("expenseAddOrder")
        remove_order_button = QPushButton("Удалить заказ")
        remove_order_button.setObjectName("expenseRemoveOrder")
        order_buttons.addWidget(add_order_button)
        order_buttons.addWidget(remove_order_button)
        order_controls = QWidget()
        order_controls.setLayout(order_buttons)
        if order_proposals:
            form.addRow(order_controls)
            form.addRow(orders_reviewed)

        def add_empty_order():
            proposal = {"project_code": "", "order_id": "", "sources": [], "ambiguous": False}
            order_proposals.append(proposal)
            add_order_proposal(proposal)
            update_order_proposal_totals()

        def remove_order():
            row = order_table.currentRow()
            if row >= 0:
                order_table.removeRow(row)
                order_proposals.pop(row)
                update_order_proposal_totals()

        add_order_button.clicked.connect(add_empty_order)
        remove_order_button.clicked.connect(remove_order)

        allocation_table = QTableWidget()
        allocation_table.setColumnCount(2)
        allocation_table.setHorizontalHeaderLabels(["Объект", "Сумма с НДС"])
        allocation_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        def add_allocation(allocation=None):
            allocation = allocation or {}
            row = allocation_table.rowCount()
            allocation_table.insertRow(row)
            object_combo = QComboBox()
            object_combo.addItem("Выберите объект", None)
            for object_row in objects:
                label = str(object_row["name"])
                if object_row.get("markning"):
                    label += f" ({object_row['markning']})"
                if object_row.get("company"):
                    label += f" · {object_row['company']}"
                object_combo.addItem(label, int(object_row["id"]))
            selected_object = allocation.get("object_id")
            if selected_object is not None:
                object_combo.setCurrentIndex(max(0, object_combo.findData(int(selected_object))))
            allocation_table.setCellWidget(row, 0, object_combo)
            allocation_table.setItem(row, 1, QTableWidgetItem(str(allocation.get("gross_amount") or "")))

        for allocation in existing_allocations or []:
            add_allocation(allocation)
        allocation_buttons = QHBoxLayout()
        add_button = QPushButton("Добавить объект")
        remove_button = QPushButton("Удалить строку")
        allocation_buttons.addWidget(add_button)
        allocation_buttons.addWidget(remove_button)
        allocation_box = QVBoxLayout()
        allocation_box.addWidget(allocation_table)
        allocation_box.addLayout(allocation_buttons)
        allocation_container = QWidget()
        allocation_container.setLayout(allocation_box)
        form.addRow("Распределение:", allocation_container)

        total_label = QLabel()
        allocated_label = QLabel()
        unallocated_label = QLabel()
        form.addRow("Итого по документу:", total_label)
        form.addRow("Распределено:", allocated_label)
        form.addRow("Не распределено:", unallocated_label)

        def update_allocation_totals(*_):
            total = parse_decimal(gross_amount.text()) or Decimal(0)
            allocated = Decimal(0)
            for row in range(allocation_table.rowCount()):
                amount_item = allocation_table.item(row, 1)
                allocated += parse_decimal(amount_item.text() if amount_item else None) or Decimal(0)
            total_label.setText(decimal_text(total) or "—")
            allocated_label.setText(decimal_text(allocated) or "0.00")
            unallocated_label.setText(decimal_text(total - allocated) or "—")

        add_button.clicked.connect(lambda: add_allocation())
        remove_button.clicked.connect(
            lambda: allocation_table.removeRow(allocation_table.currentRow())
            if allocation_table.currentRow() >= 0 else None
        )
        allocation_table.cellChanged.connect(update_allocation_totals)
        gross_amount.textChanged.connect(update_allocation_totals)
        update_allocation_totals()
        splitter.addWidget(form_scroll)
        splitter.setSizes([450, 650])
        layout.addWidget(splitter, 1)

        actions = QHBoxLayout()
        save_draft = QPushButton("Сохранить черновик")
        save_review = QPushButton("Сохранить: требует проверки")
        confirm = QPushButton("Подтвердить")
        confirm.setObjectName("expenseOrdersConfirm")
        cancel = QPushButton("Отмена")
        if is_transfer_receipt:
            save_draft.setEnabled(False)
            save_draft.setToolTip("Квитанцию перевода следует сохранить со статусом «Требует проверки».")
            save_review.setText("Сохранить квитанцию: требует проверки")
            confirm.setEnabled(False)
            confirm.setToolTip("Квитанцию перевода нельзя подтверждать как фактуру.")
        if non_invoice_type:
            confirm.setEnabled(False)
            confirm.setToolTip(
                "Подтверждение заказа нельзя подтвердить как расход."
                if non_invoice_type == "order_confirmation"
                else "Решение о транспортном налоге нельзя подтвердить как фактуру."
            )
        maximize_button = QPushButton("На весь экран")
        maximize_button.clicked.connect(
            lambda: dialog.showNormal() if dialog.isMaximized() else dialog.showMaximized()
        )
        actions.insertWidget(0, maximize_button)
        for button in (save_draft, save_review, confirm, cancel):
            actions.addWidget(button)
        layout.addLayout(actions)

        def save_document(status):
            if is_transfer_receipt and status != "requires_review":
                QMessageBox.warning(
                    dialog, "Требуется проверка",
                    "Квитанция перевода не является фактурой и сохраняется только со статусом «Требует проверки».",
                )
                return
            if status == "confirmed" and non_invoice_type:
                QMessageBox.warning(
                    dialog, "Тип документа",
                    "Подтверждение заказа нельзя подтвердить как расход. Загрузите фактуру или чек."
                    if non_invoice_type == "order_confirmation"
                    else "Решение о транспортном налоге нельзя подтвердить как фактуру.",
                )
                return
            net_value = parse_decimal(net_amount.text())
            vat_value = parse_decimal(vat_amount.text())
            gross_value = parse_decimal(gross_amount.text())
            date_value = document_date.text().strip() or None
            if date_value:
                try:
                    datetime.strptime(date_value, "%Y-%m-%d")
                except ValueError:
                    QMessageBox.warning(dialog, "Дата", "Дата документа должна быть в формате YYYY-MM-DD.")
                    return
            if status == "confirmed":
                if not supplier.currentText().strip() or not date_value or None in (net_value, vat_value, gross_value):
                    QMessageBox.warning(dialog, "Документ", "Для подтверждения заполните поставщика, дату и все три суммы.")
                    return
                if currency.currentText().strip().upper() == "UNKNOWN":
                    QMessageBox.warning(dialog, "Валюта", "Для подтверждения выберите валюту документа.")
                    return
                rounded_net = Decimal(decimal_text(net_value))
                rounded_vat = Decimal(decimal_text(vat_value))
                rounded_gross = Decimal(decimal_text(gross_value))
                if rounded_net + rounded_vat != rounded_gross:
                    QMessageBox.warning(dialog, "Суммы", "Нетто и НДС не совпадают с итогом документа.")
                    return

            proposal_rows = []
            for row in range(order_table.rowCount()):
                proposal = dict(order_proposals[row])
                object_combo = order_table.cellWidget(row, 2)
                for column in (3, 4):
                    raw_value = order_table.item(row, column).text().strip()
                    if raw_value and parse_decimal(raw_value) is None:
                        QMessageBox.warning(dialog, "Заказы", f"Проверьте net/VAT в строке {row + 1}.")
                        return
                proposal.update({
                    "project_code": order_table.item(row, 0).text().strip(),
                    "order_id": order_table.item(row, 1).text().strip(),
                    "object_id": object_combo.currentData() if object_combo else None,
                    "net_amount": decimal_text(parse_decimal(order_table.item(row, 3).text())),
                    "vat_amount": decimal_text(parse_decimal(order_table.item(row, 4).text())),
                    "gross_amount": decimal_text(parse_decimal(order_table.item(row, 5).text())),
                    "ambiguous": bool(proposal.get("ambiguous")) and not orders_reviewed.isChecked(),
                })
                proposal_rows.append(proposal)

            allocation_rows = []
            manual_row_count = 0 if status == "confirmed" and fields.get("order_proposals") else allocation_table.rowCount()
            for row in range(manual_row_count):
                combo = allocation_table.cellWidget(row, 0)
                amount_item = allocation_table.item(row, 1)
                raw_amount = amount_item.text().strip() if amount_item else ""
                amount = parse_decimal(raw_amount)
                if not raw_amount:
                    continue
                if amount is None:
                    QMessageBox.warning(dialog, "Распределение", f"Проверьте сумму в строке {row + 1}.")
                    return
                object_id = combo.currentData() if combo else None
                if object_id is None:
                    QMessageBox.warning(dialog, "Объект", f"Выберите объект в строке {row + 1}.")
                    return
                allocation_rows.append({"object_id": int(object_id), "gross_amount": amount})
            if status == "confirmed" and fields.get("order_proposals"):
                if not orders_reviewed.isChecked():
                    QMessageBox.warning(dialog, "Заказы", "Сверьте строки заказов и VAT с оригиналом.")
                    return
                try:
                    allocation_rows = build_project_order_allocations(
                        proposal_rows, net_value, vat_value, gross_value,
                    )
                except ValueError as error:
                    QMessageBox.warning(dialog, "Распределение по заказам", str(error))
                    return
            if allocation_rows:
                valid, message = validate_allocations(gross_value or Decimal(0), allocation_rows)
                if not valid:
                    QMessageBox.warning(dialog, "Распределение", message)
                    return

            payload = {
                "client_id": fields.get("client_id") or str(uuid.uuid4()),
                "supplier_name": supplier.currentText().strip(),
                "invoice_number": invoice_number.text().strip(),
                "document_date": date_value,
                "currency_code": currency.currentText().strip().upper() or "SEK",
                "net_amount": decimal_text(net_value),
                "vat_amount": decimal_text(vat_value),
                "gross_amount": decimal_text(gross_value),
                "expense_category": category.currentText().strip() or "Прочие расходы",
                "document_comment": document_comment.toPlainText().strip(),
                "verification_status": status,
                "recognized_text": fields.get("raw_text") or fields.get("recognized_text") or "",
                "allocations": allocation_rows,
                "order_proposals": proposal_rows,
            }
            if document_id is not None and fields.get("sync_version") is not None:
                payload["expected_version"] = int(fields["sync_version"])
            fields["client_id"] = payload["client_id"]
            staged_paths = []
            try:
                if new_paths:
                    staged_paths = self._store_expense_originals(new_paths, payload["client_id"])
                self.queue_expense_document_operation(
                    payload["client_id"], "upsert", payload,
                    server_document_id=document_id,
                    server_version=fields.get("sync_version") or fields.get("server_version"),
                    attachment_paths=staged_paths if new_paths else fields.get("local_attachment_paths"),
                )
            except Exception as error:
                for path in staged_paths:
                    Path(path).unlink(missing_ok=True)
                QMessageBox.warning(dialog, "Документ не сохранён", str(error))
                return
            dialog.accept()
            self.load_expense_documents()
            self.background_sync_with_cloud()
            QMessageBox.information(dialog, "Документ", "Документ сохранён в очереди синхронизации.")

        save_draft.clicked.connect(lambda: save_document("draft"))
        save_review.clicked.connect(lambda: save_document("requires_review"))
        confirm.clicked.connect(lambda: save_document("confirmed"))
        cancel.clicked.connect(dialog.reject)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _legacy_expense_document_review_dialog(self, document_id, source_path, fields, suggestions, existing_allocations):
        non_invoice_type = fields.get("non_invoice_document_type") or non_invoice_document_type(
            str(fields.get("raw_text") or fields.get("recognized_text") or "")
        )
        dialog = QDialog(self)
        dialog.setWindowTitle("Проверка документа" if source_path else "Ручная запись расхода")
        dialog.resize(1380, 900)
        root_layout = QVBoxLayout(dialog)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        preview_tabs = QTabWidget()
        preview_widget = QLabel("Оригинал не загружен" if not source_path else "Загрузка предпросмотра…")
        preview_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_widget.setMinimumWidth(420)
        if source_path:
            try:
                suffix = Path(source_path).suffix.casefold()
                if suffix == ".pdf":
                    import pypdfium2 as pdfium
                    image = pdfium.PdfDocument(str(source_path))[0].render(scale=1.35).to_pil().convert("RGB")
                    qimage = QImage(image.tobytes(), image.width, image.height, image.width * 3, QImage.Format.Format_RGB888).copy()
                    pixmap = QPixmap.fromImage(qimage)
                else:
                    pixmap = QPixmap(str(source_path))
                if not pixmap.isNull():
                    preview_widget.setPixmap(pixmap.scaled(650, 760, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            except Exception as error:
                preview_widget.setText(f"Предпросмотр недоступен: {error}")
        preview_scroll = QScrollArea()
        preview_scroll.setWidgetResizable(True)
        preview_scroll.setWidget(preview_widget)
        preview_tabs.addTab(preview_scroll, "Оригинал")
        recognized_text = QTextEdit()
        recognized_text.setReadOnly(True)
        recognized_text.setPlainText(str(fields.get("raw_text") or ""))
        preview_tabs.addTab(recognized_text, "Текст распознавания")
        if non_invoice_type:
            classification_note = QLabel(
                "Подтверждение заказа нельзя подтвердить как расход. Загрузите фактуру или чек."
                if non_invoice_type == "order_confirmation"
                else "Решение о транспортном налоге нельзя подтвердить как фактуру."
            )
            classification_note.setWordWrap(True)
            root_layout.addWidget(classification_note)
        splitter.addWidget(preview_tabs)

        form_widget = QWidget()
        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setWidget(form_widget)
        form = QFormLayout(form_widget)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        conn = get_db_connection()
        known_suppliers = conn.execute("SELECT name FROM expense_suppliers ORDER BY name COLLATE NOCASE").fetchall()
        object_rows = conn.execute("SELECT id, name, markning, is_active FROM objects ORDER BY name").fetchall()
        conn.close()
        supplier = QComboBox()
        supplier.setEditable(True)
        supplier.addItems([str(row[0]) for row in known_suppliers])
        supplier.setCurrentText(str(fields.get("supplier_name") or ""))
        org_number = QLineEdit(str(fields.get("supplier_org_number") or ""))
        vat_number = QLineEdit(str(fields.get("supplier_vat_number") or ""))
        document_type = QComboBox()
        for label, value in (("Счёт", "invoice"), ("Чек", "receipt"), ("Кредитный счёт", "credit_note"), ("Ручная запись", "manual")):
            document_type.addItem(label, value)
        type_index = document_type.findData(str(fields.get("document_type") or "invoice"))
        document_type.setCurrentIndex(max(0, type_index))
        doc_date = QLineEdit(str(fields.get("document_date") or ""))
        doc_date.setPlaceholderText("YYYY-MM-DD")
        due_date = QLineEdit(str(fields.get("due_date") or ""))
        due_date.setPlaceholderText("YYYY-MM-DD; необязательно")
        invoice_number = QLineEdit(str(fields.get("invoice_number") or ""))
        payment_reference = QLineEdit(str(fields.get("payment_reference") or ""))
        currency = QComboBox()
        currency.setEditable(True)
        currency.addItems(["UNKNOWN", "SEK", "EUR", "NOK", "DKK", "USD", "GBP", "CHF", "PLN"])
        currency.setCurrentText(str(fields.get("currency_code") or "UNKNOWN"))
        net_amount = QLineEdit(str(fields.get("net_amount") or ""))
        vat_amount = QLineEdit(str(fields.get("vat_amount") or ""))
        gross_amount = QLineEdit(str(fields.get("gross_amount") or ""))
        amount_due = QLineEdit(str(fields.get("amount_due") or ""))
        category = QComboBox()
        category.setEditable(True)
        category.addItems([
            "Материалы", "Услуги", "Аренда", "Транспорт", "Инструменты",
            "Связь", "Прочие накладные расходы",
        ])
        category.setCurrentText(str(fields.get("expense_category") or "Прочие накладные расходы"))
        document_comment = QTextEdit(str(fields.get("document_comment") or ""))
        document_comment.setMaximumHeight(90)
        document_comment.setPlaceholderText("Необязательный комментарий пользователя")
        reverse_charge = QCheckBox("В документе указано reverse charge / omvänd skattskyldighet")
        reverse_charge.setChecked(bool(fields.get("reverse_charge")))
        reverse_charge_reviewed = QCheckBox("Я сверил reverse-charge с оригиналом")
        reverse_charge_reviewed.setEnabled(reverse_charge.isChecked())
        reverse_charge.toggled.connect(reverse_charge_reviewed.setEnabled)
        payment_status = QComboBox()
        payment_status.addItem("Неизвестно", "unknown")
        payment_status.addItem("Не оплачен", "unpaid")
        current_payment_status = str(fields.get("payment_status") or "unknown")
        if current_payment_status in {"partially_paid", "paid"}:
            payment_status.addItem(
                "Частично оплачен (из платежей)" if current_payment_status == "partially_paid" else "Оплачен (из платежей)",
                current_payment_status,
            )
            payment_status.setEnabled(False)
        else:
            payment_status.setCurrentIndex(max(0, payment_status.findData(current_payment_status)))
        for label, widget in (
            ("Поставщик:", supplier), ("Org.nr:", org_number), ("VAT-номер:", vat_number),
            ("Тип:", document_type), ("Fakturadatum:", doc_date), ("Förfallodatum:", due_date),
            ("Fakturanummer:", invoice_number), ("Платёжная ссылка OCR:", payment_reference),
            ("Валюта:", currency), ("Netto / Belopp exkl. moms:", net_amount),
            ("Moms (в документе):", vat_amount), ("Общая сумма документа:", gross_amount),
            ("Сумма к оплате, если отличается:", amount_due), ("Категория расхода:", category),
            ("Комментарий:", document_comment),
        ):
            form.addRow(label, widget)
        form.addRow(reverse_charge)
        form.addRow(reverse_charge_reviewed)
        form.addRow("Состояние оплаты:", payment_status)
        reasons_label = QLabel(
            "Сигналы распознавания: "
            + ("; ".join(fields.get("review_reasons") or []) or "явных проблем не найдено; всё равно проверьте оригинал")
            + f"\nМетод: {fields.get('ocr_method', 'распознанный текст / ручной ввод')}; "
            + f"уверенность: {fields.get('ocr_confidence') if fields.get('ocr_confidence') is not None else 'не рассчитана'}"
        )
        reasons_label.setWordWrap(True)
        form.addRow(reasons_label)

        vat_table = QTableWidget()
        vat_table.setColumnCount(3)
        vat_table.setHorizontalHeaderLabels(["Ставка %", "База", "Moms"])
        for vat_line in fields.get("vat_lines") or []:
            row = vat_table.rowCount()
            vat_table.insertRow(row)
            for column, key in enumerate(("rate", "taxable_base", "amount")):
                vat_table.setItem(row, column, QTableWidgetItem(str(vat_line.get(key) or "")))
        vat_buttons = QHBoxLayout()
        add_vat_button = QPushButton("Добавить ставку НДС")
        remove_vat_button = QPushButton("Удалить строку НДС")
        vat_buttons.addWidget(add_vat_button)
        vat_buttons.addWidget(remove_vat_button)
        vat_box = QVBoxLayout()
        vat_box.addWidget(vat_table)
        vat_box.addLayout(vat_buttons)
        vat_container = QWidget()
        vat_container.setLayout(vat_box)
        form.addRow("Разбивка НДС:", vat_container)
        add_vat_button.clicked.connect(lambda: vat_table.insertRow(vat_table.rowCount()))
        remove_vat_button.clicked.connect(lambda: vat_table.removeRow(vat_table.currentRow()) if vat_table.currentRow() >= 0 else None)

        suggestion_text = "; ".join(f"{item['name']}: {item['reason']}" for item in suggestions)
        suggestion_label = QLabel(
            "Предложение объекта: " + (suggestion_text if suggestion_text else "совпадение не найдено; объект выберите вручную")
        )
        suggestion_label.setWordWrap(True)
        form.addRow(suggestion_label)

        allocation_table = QTableWidget()
        allocation_table.setColumnCount(2)
        allocation_table.setHorizontalHeaderLabels(["Объект", "Распределённая сумма документа"])
        allocations = existing_allocations if existing_allocations is not None else []
        if not allocations and fields.get("gross_amount"):
            suggestion_id = suggestions[0]["object_id"] if len(suggestions) == 1 else None
            allocations = [{"object_id": suggestion_id, "gross_amount": fields.get("gross_amount"), "note": suggestions[0]["reason"] if len(suggestions) == 1 else ""}]

        def add_allocation(allocation=None):
            allocation = allocation or {}
            row = allocation_table.rowCount()
            allocation_table.insertRow(row)
            object_combo = QComboBox()
            object_combo.addItem("Без объекта", None)
            selected_object = allocation.get("object_id")
            for object_row in object_rows:
                object_id = int(object_row["id"])
                if not object_row["is_active"] and object_id != selected_object:
                    continue
                title = str(object_row["name"])
                if object_row["markning"]:
                    title += f" ({object_row['markning']})"
                if not object_row["is_active"]:
                    title += " [архив]"
                object_combo.addItem(title, object_id)
            object_combo.setCurrentIndex(max(0, object_combo.findData(int(selected_object)) if selected_object is not None else 0))
            allocation_table.setCellWidget(row, 0, object_combo)
            allocation_table.setItem(row, 1, QTableWidgetItem(str(allocation.get("gross_amount") or "")))
        for allocation in allocations:
            add_allocation(allocation)
        allocation_buttons = QHBoxLayout()
        add_allocation_button = QPushButton("Добавить объект")
        remove_allocation_button = QPushButton("Удалить строку")
        add_alias_button = QPushButton("Добавить альтернативное название")
        allocation_buttons.addWidget(add_allocation_button)
        allocation_buttons.addWidget(remove_allocation_button)
        allocation_buttons.addWidget(add_alias_button)
        allocation_box = QVBoxLayout()
        allocation_box.addWidget(allocation_table)
        allocation_box.addLayout(allocation_buttons)
        allocation_container = QWidget()
        allocation_container.setLayout(allocation_box)
        form.addRow("Распределение:", allocation_container)
        add_allocation_button.clicked.connect(lambda: add_allocation())
        remove_allocation_button.clicked.connect(lambda: allocation_table.removeRow(allocation_table.currentRow()) if allocation_table.currentRow() >= 0 else None)

        def add_object_alias():
            row = allocation_table.currentRow()
            combo = allocation_table.cellWidget(row, 0) if row >= 0 else None
            object_id = combo.currentData() if combo else None
            if object_id is None:
                QMessageBox.information(dialog, "Объект", "Выберите объект в строке распределения.")
                return
            alias, accepted = QInputDialog.getText(dialog, "Альтернативное название", "Название / адрес / код:")
            if not accepted or not alias.strip():
                return
            conn = get_db_connection()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO expense_object_aliases (object_id, alias) VALUES (?, ?)",
                    (int(object_id), alias.strip()),
                )
                conn.commit()
            finally:
                conn.close()
            QMessageBox.information(dialog, "Объект", "Альтернативное название сохранено.")

        add_alias_button.clicked.connect(add_object_alias)

        splitter.addWidget(form_scroll)
        splitter.setSizes([620, 700])
        root_layout.addWidget(splitter, 1)
        action_row = QHBoxLayout()
        save_draft = QPushButton("Сохранить черновик")
        save_review = QPushButton("Сохранить: требует проверки")
        confirm = QPushButton("Подтвердить документ")
        if non_invoice_type:
            confirm.setEnabled(False)
            confirm.setToolTip(
                "Подтверждение заказа нельзя подтвердить как расход."
                if non_invoice_type == "order_confirmation"
                else "Решение о транспортном налоге нельзя подтвердить как фактуру."
            )
        cancel = QPushButton("Отмена")
        for button in (save_draft, save_review, confirm, cancel):
            action_row.addWidget(button)
        root_layout.addLayout(action_row)

        def save_document(status):
            if status == "confirmed" and non_invoice_type:
                QMessageBox.warning(
                    dialog, "Тип документа",
                    "Подтверждение заказа нельзя подтвердить как расход. Загрузите фактуру или чек."
                    if non_invoice_type == "order_confirmation"
                    else "Решение о транспортном налоге нельзя подтвердить как фактуру.",
                )
                return
            net_value = parse_decimal(net_amount.text())
            vat_value = parse_decimal(vat_amount.text())
            gross_value = parse_decimal(gross_amount.text())
            due_value = parse_decimal(amount_due.text())
            vat_lines = []
            vat_sum = Decimal(0)
            for row in range(vat_table.rowCount()):
                rate_item = vat_table.item(row, 0)
                base_item = vat_table.item(row, 1)
                value_item = vat_table.item(row, 2)
                if not any((rate_item, base_item, value_item)):
                    continue
                row_vat = parse_decimal(value_item.text() if value_item else None)
                if row_vat is None:
                    QMessageBox.warning(dialog, "НДС", f"Проверьте сумму НДС в строке {row + 1}.")
                    return
                vat_sum += row_vat
                vat_lines.append({
                    "rate": rate_item.text().strip() if rate_item else "",
                    "taxable_base": parse_decimal(base_item.text() if base_item else None),
                    "amount": row_vat,
                })
            if vat_lines:
                vat_value = vat_sum
                vat_amount.setText(decimal_text(vat_sum) or "")

            doc_date_value = doc_date.text().strip()
            due_date_value = due_date.text().strip() or None
            date_error = None
            for label, value in (("Fakturadatum", doc_date_value), ("Förfallodatum", due_date_value)):
                if value:
                    try:
                        datetime.strptime(value, "%Y-%m-%d")
                    except ValueError:
                        date_error = f"{label} должен быть в формате YYYY-MM-DD."
                        break
            if date_error:
                QMessageBox.warning(dialog, "Дата", date_error)
                return

            allocation_rows = []
            for row in range(allocation_table.rowCount()):
                combo = allocation_table.cellWidget(row, 0)
                amount_item = allocation_table.item(row, 1)
                value = parse_decimal(amount_item.text() if amount_item else None)
                if value is None:
                    if status == "confirmed":
                        QMessageBox.warning(dialog, "Распределение", f"Укажите сумму распределения в строке {row + 1}.")
                        return
                    continue
                allocation_rows.append({
                    "object_id": combo.currentData() if combo else None,
                    "gross_amount": value,
                    "note": suggestion_text if combo and combo.currentData() is not None else "",
                })
            if status == "confirmed" and (not allocation_rows or any(item["object_id"] is None for item in allocation_rows)):
                QMessageBox.warning(dialog, "Объект", "Для подтверждения выберите объект в каждой строке распределения. Черновик можно сохранить без объекта.")
                return

            reasons = []
            supplier_name_value = supplier.currentText().strip()
            if not supplier_name_value:
                reasons.append("поставщик не указан")
            if not doc_date_value:
                reasons.append("дата документа не указана")
            if document_type.currentData() == "invoice" and not invoice_number.text().strip():
                reasons.append("номер счёта не указан")
            currency_value = currency.currentText().strip().upper() or "UNKNOWN"
            if currency_value == "UNKNOWN":
                reasons.append("валюта не подтверждена")
            if net_value is None:
                reasons.append("нетто не указано")
            if vat_value is None:
                reasons.append("НДС неизвестен; не подставлен по умолчанию")
            if gross_value is None:
                reasons.append("общая сумма не указана")
            if net_value is not None and vat_value is not None and gross_value is not None and abs(net_value + vat_value - gross_value) > Decimal("0.02"):
                reasons.append("нетто + НДС не сходятся с итогом")
            if reverse_charge.isChecked() and not reverse_charge_reviewed.isChecked():
                reasons.append("reverse charge требует ручной проверки")
            if status == "confirmed" and reasons:
                QMessageBox.warning(dialog, "Нельзя подтвердить", "Проверьте документ:\n• " + "\n• ".join(reasons))
                return

            values = dict(fields)
            values.update({
                "supplier_name": supplier_name_value or None,
                "supplier_org_number": org_number.text().strip() or None,
                "supplier_vat_number": vat_number.text().strip() or None,
                "document_type": document_type.currentData(),
                "document_date": doc_date_value or None,
                "due_date": due_date_value,
                "invoice_number": invoice_number.text().strip() or None,
                "payment_reference": payment_reference.text().strip() or None,
                "currency_code": currency_value,
                "net_amount": net_value,
                "vat_amount": vat_value,
                "gross_amount": gross_value,
                "amount_due": due_value,
                "expense_category": category.currentText().strip() or "Прочее",
                "document_comment": document_comment.toPlainText().strip(),
                "reverse_charge": reverse_charge.isChecked(),
                "vat_lines": vat_lines,
                "review_reasons": reasons,
                "payment_status": payment_status.currentData(),
            })
            if status == "confirmed" and allocation_rows:
                valid, message = validate_allocations(gross_value, allocation_rows)
                if not valid:
                    QMessageBox.warning(dialog, "Распределение", message)
                    return

            allow_duplicate = False
            try:
                conn = get_db_connection()
                file_hash = None
                if source_path:
                    from expense_documents import sha256_file
                    file_hash = sha256_file(source_path)
                duplicates = find_document_duplicates(conn, values, file_hash)
                if document_id is not None:
                    duplicates = [item for item in duplicates if int(item["id"]) != int(document_id)]
                if duplicates:
                    answer = QMessageBox.question(
                        dialog, "Возможный дубль",
                        "Найден документ с тем же файлом или поставщиком/номером/датой/суммой. "
                        "Сохранить копию всё равно?\n" + "\n".join(
                            f"#{item['id']} {item['supplier_name']} {item['invoice_number']} {item['gross_amount']} {item['currency_code']}"
                            for item in duplicates[:5]
                        ),
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if answer != QMessageBox.StandardButton.Yes:
                        conn.close()
                        return
                    allow_duplicate = True

                if document_id is None:
                    from expense_documents import save_supplier_document
                    saved_id = save_supplier_document(
                        conn, document_storage_root(), values, allocation_rows, source_path,
                        status, allow_duplicate,
                    )
                else:
                    saved_id = self._api_update_supplier_document_record(
                        conn, document_id, source_path, values, allocation_rows, status, allow_duplicate
                    )
                conn.close()
            except Exception as error:
                if "conn" in locals():
                    conn.close()
                QMessageBox.warning(dialog, "Документ не сохранён", str(error))
                return
            dialog.accept()
            self._api_load_expense_document_filters()
            self._api_load_expense_documents()
            QMessageBox.information(self, "Документ", f"Запись #{saved_id} сохранена со статусом: {status}.")

        save_draft.clicked.connect(lambda: save_document("draft"))
        save_review.clicked.connect(lambda: save_document("requires_review"))
        confirm.clicked.connect(lambda: save_document("confirmed"))
        cancel.clicked.connect(dialog.reject)
        dialog.exec()

    def _api_update_supplier_document_record(self, conn, document_id, source_path, fields, allocations, status, allow_duplicate):
        existing = conn.execute(
            "SELECT stored_path, file_sha256 FROM supplier_documents WHERE id = ?", (int(document_id),)
        ).fetchone()
        if existing is None:
            raise ValueError("Документ не найден.")
        if status == "confirmed":
            net, vat, gross = (parse_decimal(fields.get(key)) for key in ("net_amount", "vat_amount", "gross_amount"))
            if None in (net, vat, gross) or abs(net + vat - gross) > Decimal("0.02"):
                raise ValueError("Нетто + НДС не сходятся с итоговой суммой.")
        file_hash = existing["file_sha256"]
        if source_path:
            from expense_documents import sha256_file
            file_hash = sha256_file(source_path)
        duplicates = find_document_duplicates(conn, fields, file_hash, exclude_document_id=document_id)
        if duplicates and not allow_duplicate:
            raise ValueError("Найден возможный дубль документа; сохранение отменено.")
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("BEGIN IMMEDIATE")
        supplier_name = str(fields.get("supplier_name") or "").strip()
        supplier_org = str(fields.get("supplier_org_number") or "").strip() or None
        supplier_vat = str(fields.get("supplier_vat_number") or "").strip() or None
        supplier_id = None
        if supplier_name:
            found = conn.execute(
                "SELECT id FROM expense_suppliers WHERE name = ? COLLATE NOCASE AND COALESCE(org_number, '') = COALESCE(?, '') ORDER BY id LIMIT 1",
                (supplier_name, supplier_org),
            ).fetchone()
            if found:
                supplier_id = int(found[0])
            else:
                supplier_id = int(conn.execute(
                    "INSERT INTO expense_suppliers (name, org_number, vat_number, created_at) VALUES (?, ?, ?, ?)",
                    (supplier_name, supplier_org, supplier_vat, datetime.now(timezone.utc).isoformat()),
                ).lastrowid)
        object_ids = [int(item["object_id"]) for item in allocations if item.get("object_id") is not None]
        if object_ids:
            placeholders = ",".join("?" for _ in object_ids)
            object_names = [str(item[0]) for item in conn.execute(
                f"SELECT name FROM objects WHERE id IN ({placeholders}) ORDER BY name", object_ids
            ).fetchall()]
        else:
            object_names = []
        conn.execute("""
            UPDATE supplier_documents SET supplier_id=?, supplier_name=?, supplier_org_number=?, supplier_vat_number=?,
                document_type=?, invoice_number=?, payment_reference=?, document_date=?, due_date=?, currency_code=?,
                net_amount=?, vat_amount=?, gross_amount=?, amount_due=?, expense_category=?, is_reverse_charge=?,
                document_comment=?, verification_status=?, payment_status=?, recognized_text=?, review_reasons=?, updated_at=? WHERE id=?
        """, (
            supplier_id, supplier_name or None, supplier_org, supplier_vat, fields["document_type"],
            fields.get("invoice_number"), fields.get("payment_reference"), fields.get("document_date"), fields.get("due_date"),
            fields["currency_code"], decimal_text(fields.get("net_amount")), decimal_text(fields.get("vat_amount")),
            decimal_text(fields.get("gross_amount")), decimal_text(fields.get("amount_due")), fields["expense_category"],
            int(bool(fields.get("reverse_charge"))), str(fields.get("document_comment") or ""), status,
            fields.get("payment_status") or "unknown",
            fields.get("raw_text") or "", "\n".join(fields.get("review_reasons") or []), now, int(document_id),
        ))
        conn.execute("DELETE FROM supplier_document_vat_lines WHERE document_id=?", (int(document_id),))
        conn.execute("DELETE FROM supplier_document_allocations WHERE document_id=?", (int(document_id),))
        for line in fields.get("vat_lines") or []:
            conn.execute(
                "INSERT INTO supplier_document_vat_lines (document_id,vat_rate,taxable_base,vat_amount,note) VALUES (?,?,?,?,?)",
                (int(document_id), str(line.get("rate") or ""), decimal_text(line.get("taxable_base")), decimal_text(line.get("amount")) or "0.00", str(line.get("note") or "")),
            )
        for allocation in allocations:
            conn.execute(
                "INSERT INTO supplier_document_allocations (document_id,object_id,allocated_net,allocated_vat,allocated_gross,allocation_note) VALUES (?,?,?,?,?,?)",
                (int(document_id), allocation.get("object_id"), decimal_text(allocation.get("net_amount")), decimal_text(allocation.get("vat_amount")), decimal_text(allocation.get("gross_amount")) or "0.00", str(allocation.get("note") or "")),
            )
        stored_path = existing["stored_path"]
        destination = None
        if stored_path and Path(stored_path).is_file():
            try:
                destination, _ = store_original_file(
                    stored_path, document_storage_root(), int(document_id), object_names
                )
                if destination != Path(stored_path):
                    conn.execute(
                        "UPDATE supplier_documents SET stored_path = ?, updated_at = ? WHERE id = ?",
                        (str(destination), now, int(document_id)),
                    )
            except Exception:
                conn.rollback()
                if destination and destination != Path(stored_path):
                    destination.unlink(missing_ok=True)
                raise
        try:
            conn.commit()
        except Exception:
            conn.rollback()
            if destination and stored_path and destination != Path(stored_path):
                destination.unlink(missing_ok=True)
            raise
        if destination and stored_path and destination != Path(stored_path):
            old_path = Path(stored_path)
            if old_path.is_relative_to(document_storage_root()):
                old_path.unlink(missing_ok=True)
        return int(document_id)

    def apply_expense_period_preset(self, *_):
        if self._updating_expense_dates:
            return
        preset = self.exp_period_preset.currentData()
        today = QDate.currentDate()
        if preset == "custom":
            self.load_expense_documents()
            return
        if preset == "this_month":
            date_from = QDate(today.year(), today.month(), 1)
            date_to = date_from.addMonths(1).addDays(-1)
        elif preset == "last_month":
            date_from = QDate(today.year(), today.month(), 1).addMonths(-1)
            date_to = date_from.addMonths(1).addDays(-1)
        elif preset == "last_30":
            date_from = today.addDays(-29)
            date_to = today
        else:
            date_from = QDate(today.year(), 1, 1)
            date_to = QDate(today.year(), 12, 31)
        self._updating_expense_dates = True
        self.exp_date_from.setDate(date_from)
        self.exp_date_to.setDate(date_to)
        self._updating_expense_dates = False
        self.load_expense_documents()

    def on_expense_date_changed(self, *_):
        if not self._updating_expense_dates:
            custom_index = self.exp_period_preset.findData("custom")
            if custom_index >= 0 and self.exp_period_preset.currentIndex() != custom_index:
                self._updating_expense_dates = True
                self.exp_period_preset.setCurrentIndex(custom_index)
                self._updating_expense_dates = False
            self.load_expense_documents()

    def open_add_expense_dialog(self):
        if self.exp_date_from.date() > self.exp_date_to.date():
            QMessageBox.warning(self, "Расход", "Сначала выберите корректный период.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Добавить расход")
        dialog.setMinimumWidth(480)
        form = QFormLayout(dialog)
        today = QDate.currentDate()
        period_start = self.exp_date_from.date()
        period_end = self.exp_date_to.date()
        default_date = today if period_start <= today <= period_end else period_end
        expense_date = create_date_field(default_date.toString("yyyy-MM-dd"))
        expense_date.setMinimumDate(period_start)
        expense_date.setMaximumDate(period_end)
        category = QComboBox()
        category.addItems([
            "Инструменты и расходники",
            "Аренда склада / офиса",
            "Лизинг и топливо транспорта (Ford Transit)",
            "Бухгалтерия и страховка",
            "Прочие накладные расходы",
        ])
        description = QLineEdit()
        description.setPlaceholderText("Комментарий к расходу")
        amount = QLineEdit()
        amount.setPlaceholderText("Сумма в SEK")
        form.addRow("Дата:", expense_date)
        form.addRow("Категория:", category)
        form.addRow("Комментарий:", description)
        form.addRow("Сумма (SEK):", amount)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Добавить")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            expense_amount = parse_float(amount.text(), None)
            if expense_amount is None or expense_amount < 0:
                QMessageBox.warning(self, "Расход", "Введите корректную неотрицательную сумму в SEK.")
                return
            self.add_company_expense(
                expense_date.text(), category.currentText(), description.text().strip(), expense_amount
            )

    def apply_expense_journal_filters(self, *_):
        if not hasattr(self, "table_expenses"):
            return
        selected_category = self.exp_filter_category.currentText()
        search_text = self.exp_filter_search.text().strip().casefold()
        for row in range(self.table_expenses.rowCount()):
            category_item = self.table_expenses.item(row, 2)
            description_item = self.table_expenses.item(row, 3)
            matches_category = (
                selected_category == "Все категории"
                or (category_item and category_item.text() == selected_category)
            )
            searchable = " ".join(
                item.text() for item in (category_item, description_item) if item
            ).casefold()
            self.table_expenses.setRowHidden(
                row, not matches_category or bool(search_text and search_text not in searchable)
            )

    def show_expense_details(self, *_):
        row = self.table_expenses.currentRow()
        if row < 0:
            return
        date_item = self.table_expenses.item(row, 1)
        category_item = self.table_expenses.item(row, 2)
        description_item = self.table_expenses.item(row, 3)
        amount_item = self.table_expenses.item(row, 4)
        status_item = self.table_expenses.item(row, 5)
        details = [
            f"ID: {self.table_expenses.item(row, 0).text() if self.table_expenses.item(row, 0) else ''}",
            f"Дата: {date_item.text() if date_item else ''}",
            f"Категория: {category_item.text() if category_item else ''}",
            f"Сумма: {amount_item.text() if amount_item else ''}",
            f"Статус: {status_item.text() if status_item else ''}",
            "",
            description_item.text() if description_item else "",
        ]
        QMessageBox.information(self, "Подробности записи", "\n".join(details))

    def auto_calculate_taxes_and_fora(self):
        d_from = self.exp_date_from.text().strip()
        d_to = self.exp_date_to.text().strip()
        if d_from > d_to:
            QMessageBox.warning(self, "Расчёт", "Сначала выберите корректный период.")
            return
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, COALESCE(salary_rate, 0.0) FROM employees")
        rates = {str(row[0]).strip().lower(): row[1] for row in cursor.fetchall()}

        cursor.execute("""
            SELECT employee, SUM(CASE WHEN entry_type = 'work' THEN hours ELSE 0 END) as total_hours
            FROM shifts
            WHERE date >= ? AND date <= ?
            GROUP BY employee
        """, (d_from, d_to))
        shift_rows = cursor.fetchall()

        if not shift_rows:
            conn.close()
            QMessageBox.warning(self, "Внимание", "За выбранный период нет отработанных смен для расчета отчислений!")
            return

        total_payroll = 0.0
        basis_lines = []
        missing_rates = []
        for row in shift_rows:
            emp_key = str(row[0]).strip().lower()
            employee_name = str(row[0])
            work_hours = float(row[1] or 0.0)
            rate = float(rates.get(emp_key, 0.0) or 0.0)
            if work_hours <= 0:
                continue
            if rate <= 0:
                missing_rates.append(employee_name)
                continue
            employee_base = work_hours * rate
            total_payroll += employee_base
            basis_lines.append(
                f"{employee_name}: {work_hours:.2f} ч × {rate:.2f} SEK/ч "
                f"(текущая ставка профиля) = {employee_base:.2f} SEK"
            )

        if missing_rates:
            conn.close()
            QMessageBox.warning(
                self, "Оценка не рассчитана",
                "Не найдена положительная текущая зарплатная ставка для: "
                + ", ".join(missing_rates)
                + ". Условную сумму не показываю и не сохраняю.",
            )
            return

        if total_payroll <= 0:
            conn.close()
            QMessageBox.warning(self, "Внимание", "Фонд оплаты труда за период равен нулю. Проверьте ставки сотрудников!")
            return

        fora_rate = 0.052
        employer_rate = 0.3142
        fora_amount = total_payroll * fora_rate
        employer_amount = total_payroll * employer_rate
        payroll_basis = (
            f"Источник часов: только обычная Работа; OB исключён. "
            f"Источник ставки: employees.salary_rate (текущий профиль, не историческая ставка). "
            f"База оценки: {total_payroll:.2f} SEK. "
            + "; ".join(basis_lines)
        )
        estimate_disclaimer = "Оценка по текущей логике; процент и формула не подтверждены как обязательство"
        calculations = [
            {
                "category": "FORA и страхование рабочих (Kollektivavtal)",
                "prefix": f"Авторасчет FORA за период {d_from} - {d_to}",
                "description": (
                    f"Авторасчет FORA за период {d_from} - {d_to}; Основа: {payroll_basis}; "
                    f"Ставка: {fora_rate * 100:.2f}%; {estimate_disclaimer}"
                ),
                "amount": fora_amount, "rate": fora_rate, "label": "FORA",
            },
            {
                "category": "Налоги компании (Arbetsgivareavgift 31.42%)",
                "prefix": f"Авторасчет социального налога за период {d_from} - {d_to}",
                "description": (
                    f"Авторасчет социального налога за период {d_from} - {d_to}; Основа: {payroll_basis}; "
                    f"Ставка: {employer_rate * 100:.2f}%; {estimate_disclaimer}"
                ),
                "amount": employer_amount, "rate": employer_rate, "label": "Arbetsgivaravgift",
            },
        ]

        review_warnings = []
        pending_calculations = []
        for calculation in calculations:
            cursor.execute(
                "SELECT id, amount, description FROM company_expenses WHERE category = ? AND description LIKE ? ORDER BY id",
                (calculation["category"], calculation["prefix"] + "%"),
            )
            existing = cursor.fetchall()
            if len(existing) > 1:
                review_warnings.append(
                    f"{calculation['label']}: найдено записей: {len(existing)}. Старые строки не меняются; проверьте их вручную."
                )
            elif existing:
                saved = existing[0]
                if abs(float(saved["amount"] or 0.0) - calculation["amount"]) > 0.01:
                    review_warnings.append(
                        f"{calculation['label']}: сохранено {float(saved['amount']):,.2f} SEK, "
                        f"текущий preview {calculation['amount']:,.2f} SEK. Автоизменение запрещено, проверьте вручную."
                    )
                else:
                    calculation["already_saved"] = True
                    saved_description = str(saved["description"] or "")
                    if not all(marker in saved_description for marker in (
                        "Основа:", "Ставка:", "employees.salary_rate", estimate_disclaimer
                    )):
                        review_warnings.append(
                            f"{calculation['label']}: источник исторической базы не подтверждён. "
                            "Требует проверки; сумма оставлена без изменений."
                        )
            else:
                pending_calculations.append(calculation)

        conn.close()
        preview_lines = [
            f"Период: {d_from} — {d_to}",
            "Это условная оценка, не подтверждённое налоговое обязательство. "
            "База использует текущие ставки профиля, а не исторические ставки смен. OB исключён.",
            f"Расчётная база: {total_payroll:,.2f} SEK (обычные часы × текущая ставка)",
            *basis_lines,
            "",
            f"FORA: {total_payroll:,.2f} × {fora_rate * 100:.2f}% = {fora_amount:,.2f} SEK",
            f"Arbetsgivaravgift: {total_payroll:,.2f} × {employer_rate * 100:.2f}% = {employer_amount:,.2f} SEK",
            f"Оценка начислений (не оплата): {fora_amount + employer_amount:,.2f} SEK",
        ]
        if review_warnings:
            preview_lines.extend(["", "Проверки:", *review_warnings])
        if not pending_calculations:
            ending = (
                "За период уже найдены начисления; повторные начисления не добавлены. "
                "При отсутствии сохранённой базы требуется ручная проверка."
                if review_warnings else
                "Оценки за этот период уже сохранены; повторно не добавлялись."
            )
            QMessageBox.information(
                self, "Предварительный расчёт",
                "\n".join(preview_lines + ["", ending]),
            )
            return
        if review_warnings:
            QMessageBox.warning(
                self, "Нужна проверка", "\n".join(preview_lines + ["", "Ничего не сохранено. Проверьте исторические записи вручную."]),
            )
            return

        confirmation = QMessageBox(self)
        confirmation.setWindowTitle("Предварительный расчёт начислений")
        confirmation.setIcon(QMessageBox.Icon.Information)
        confirmation.setText("\n".join(preview_lines))
        save_button = confirmation.addButton("Сохранить оценки", QMessageBox.ButtonRole.AcceptRole)
        confirmation.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        confirmation.exec()
        if confirmation.clickedButton() != save_button:
            return

        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            inserted = 0
            for calculation in pending_calculations:
                cursor.execute(
                    "SELECT id, amount FROM company_expenses WHERE category = ? AND description LIKE ? ORDER BY id",
                    (calculation["category"], calculation["prefix"] + "%"),
                )
                concurrent_existing = cursor.fetchall()
                if concurrent_existing:
                    if (len(concurrent_existing) == 1 and
                            abs(float(concurrent_existing[0]["amount"] or 0.0) - calculation["amount"]) <= 0.01):
                        continue
                    raise RuntimeError(
                        f"Период {d_from}–{d_to} уже имеет начисления {calculation['label']}; проверьте журнал."
                    )
                cursor.execute(
                    "INSERT INTO company_expenses (date, category, description, amount) VALUES (?, ?, ?, ?)",
                    (d_to, calculation["category"], calculation["description"], calculation["amount"]),
                )
                inserted += 1
            conn.commit()
        except Exception as error:
            if conn:
                conn.rollback()
            QMessageBox.warning(self, "Начисления не сохранены", str(error))
            return
        finally:
            if conn:
                conn.close()

        self.load_expenses_table()
        if inserted:
            self.background_sync_with_cloud()
        QMessageBox.information(
              self, "Оценка расчёта",
              (f"Сохранено оценок: {inserted}. Это не подтверждение обязанности или оплаты." if inserted else
               "Оценки за этот период уже сохранены; повторные строки не добавлены.")
        )

    def add_company_expense(self, expense_date, category, description, amount):
        expense_qdate = QDate.fromString(str(expense_date), "yyyy-MM-dd")
        if amount is None or amount < 0:
            QMessageBox.warning(self, "Расход", "Введите корректную неотрицательную сумму в SEK.")
            return
        if not expense_qdate.isValid() or not self.exp_date_from.date() <= expense_qdate <= self.exp_date_to.date():
            QMessageBox.warning(self, "Расход", "Дата расхода должна входить в выбранный период.")
            return
        if str(description).startswith("Авторасчет"):
            QMessageBox.warning(self, "Расход", "Комментарий зарезервирован для автооценок.")
            return
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO company_expenses (date, category, description, amount) VALUES (?, ?, ?, ?)",
            (expense_date, category, description, amount),
        )
        conn.commit()
        conn.close()
        self.load_expenses_table()
        self.background_sync_with_cloud()

    def load_expenses_table(self):
        date_from = self.exp_date_from.text().strip()
        date_to = self.exp_date_to.text().strip()
        self.exp_period_label.setText(f"Выбранный период: {date_from} — {date_to}")
        if date_from > date_to:
            self.exp_warning_lbl.setText("Период некорректен: дата начала позже даты окончания.")
            self.exp_warning_lbl.show()
            self.exp_summary_expenses_lbl.setText("0.00 SEK")
            self.exp_summary_fora_lbl.setText("0.00 SEK")
            self.exp_summary_employer_lbl.setText("0.00 SEK")
            self.exp_summary_review_lbl.setText("0 шт.")
            self.table_expenses.setRowCount(0)
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        accrual_pattern = f"Авторасчет%за период {date_from} - {date_to}%"
        cursor.execute("""
            SELECT id, date, category, description, amount
            FROM company_expenses
            WHERE description LIKE ?
               OR ((description IS NULL OR description NOT LIKE 'Авторасчет%') AND date >= ? AND date <= ?)
            ORDER BY date DESC, id DESC
        """, (accrual_pattern, date_from, date_to))
        rows = cursor.fetchall()
        conn.close()

        ordinary_expenses = employer_estimate = fora_estimate = 0.0
        auto_counts = {}
        for row in rows:
            category = str(row["category"] or "")
            description = str(row["description"] or "")
            is_auto = description.startswith("Авторасчет")
            if not is_auto:
                ordinary_expenses += float(row["amount"] or 0.0)
            else:
                auto_key = (category, description.split(";", 1)[0])
                auto_counts[auto_key] = auto_counts.get(auto_key, 0) + 1

        estimate_fora = estimate_employer = 0.0
        review_rows = []
        statuses = {}
        for row in rows:
            expense_id = int(row["id"])
            category = str(row["category"] or "")
            description = str(row["description"] or "")
            if not description.startswith("Авторасчет"):
                statuses[expense_id] = "Расход внесён вручную"
                continue

            auto_key = (category, description.split(";", 1)[0])
            category_key = category.casefold()
            is_fora = "fora" in category_key
            is_employer = "arbetsgivareavgift" in category_key or "arbetsgivaravgift" in category_key
            provenance_ok = all(marker in description for marker in (
                "Основа:", "Ставка:", "employees.salary_rate",
                "формула не подтверждены как обязательство",
            ))
            reason = None
            if auto_counts[auto_key] > 1:
                reason = "дубли периода"
            elif not provenance_ok:
                reason = "нет подтверждённого источника базы/ставки"
            elif not (is_fora or is_employer):
                reason = "неизвестная категория оценки"

            if reason:
                statuses[expense_id] = f"Требует проверки: {reason}; исключено из оценок"
                review_rows.append(expense_id)
            else:
                statuses[expense_id] = "Условная оценка; не подтверждённое обязательство"
                if is_fora:
                    estimate_fora += float(row["amount"] or 0.0)
                else:
                    estimate_employer += float(row["amount"] or 0.0)

        self.exp_summary_expenses_lbl.setText(f"{ordinary_expenses:,.2f} SEK")
        self.exp_summary_fora_lbl.setText(f"{estimate_fora:,.2f} SEK")
        self.exp_summary_employer_lbl.setText(f"{estimate_employer:,.2f} SEK")
        self.exp_summary_review_lbl.setText(f"{len(review_rows)} шт.")

        categories = sorted({str(row["category"] or "") for row in rows if row["category"]})
        selected_category = self.exp_filter_category.currentText()
        self.exp_filter_category.blockSignals(True)
        self.exp_filter_category.clear()
        self.exp_filter_category.addItem("Все категории")
        self.exp_filter_category.addItems(categories)
        category_index = self.exp_filter_category.findText(selected_category)
        if category_index >= 0:
            self.exp_filter_category.setCurrentIndex(category_index)
        self.exp_filter_category.blockSignals(False)

        self.table_expenses.setRowCount(len(rows))
        for row_idx, row in enumerate(rows):
            expense_id, date, category, description, amount = row
            category = str(category or "")
            description = str(description or "")
            status = statuses[int(expense_id)]
            for column, value in enumerate((expense_id, date, category, description, f"{float(amount):,.2f} SEK", status)):
                item = QTableWidgetItem(str(value))
                if column in (2, 3, 5):
                    item.setToolTip(str(value))
                self.table_expenses.setItem(row_idx, column, item)

        if review_rows:
            self.exp_warning_lbl.setText(
                f"Требуют проверки: {len(review_rows)} старых/дублирующихся автооценок исключены из карточек. "
                "Их суммы не считаются подтверждёнными и не складываются с ручными расходами."
            )
            self.exp_warning_lbl.show()
        else:
            self.exp_warning_lbl.clear()
            self.exp_warning_lbl.hide()
        self.apply_expense_journal_filters()

    def delete_company_expense(self, index=None):
        selected = self.table_expenses.currentRow()
        if selected < 0:
            return
        id_item = self.table_expenses.item(selected, 0)
        date_item = self.table_expenses.item(selected, 1)
        category_item = self.table_expenses.item(selected, 2)
        description_item = self.table_expenses.item(selected, 3)
        amount_item = self.table_expenses.item(selected, 4)
        if not all((id_item, date_item, category_item, amount_item)):
            return
        confirmation_text = (
            f"Удалить выбранную запись #{id_item.text()}?\n\n"
            f"Дата: {date_item.text()}\n"
            f"Категория: {category_item.text()}\n"
            f"Сумма: {amount_item.text()}"
        )
        if QMessageBox.question(
            self, "Удаление расхода", confirmation_text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        conn = get_db_connection()
        cursor = conn.execute("DELETE FROM company_expenses WHERE id = ?", (int(id_item.text()),))
        conn.commit()
        deleted = cursor.rowcount == 1
        conn.close()
        if deleted:
            self.load_expenses_table()
            self.background_sync_with_cloud()

    def edit_balance_entry(self, balance_id=None, *_):
        if balance_id is None:
            selected = self.table_balance.currentRow()
            item = self.table_balance.item(selected, 0) if selected >= 0 else None
            balance_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if balance_id is None:
            QMessageBox.information(self, "Баланс", "Выберите операцию для изменения.")
            return
        conn = get_db_connection()
        row = conn.execute(
            "SELECT id, date, employee, description, change_amount FROM balance WHERE id = ?",
            (int(balance_id),),
        ).fetchone()
        conn.close()
        if row is None:
            QMessageBox.warning(self, "Баланс", "Операция с указанным ID не найдена.")
            self.load_balance_table()
            return
        self._editing_balance_id = int(row["id"])
        self.bal_date.setText(str(row["date"]))
        self.bal_emp_cb.setCurrentText(str(row["employee"]))
        self.bal_desc.setText(str(row["description"]))
        self.bal_amount.setText(str(row["change_amount"]))
        self.add_balance_button.setText("Сохранить изменения")

    def update_balance_entry(self, balance_id=None, date=None, employee=None, description=None, amount=None):
        balance_id = int(balance_id or self.__dict__.get("_editing_balance_id", 0) or 0)
        if not balance_id:
            QMessageBox.warning(self, "Баланс", "Сначала выберите операцию для изменения.")
            return False
        date = self.bal_date.text().strip() if date is None else str(date)
        employee = self.bal_emp_cb.currentText().strip() if employee is None else str(employee).strip()
        description = self.bal_desc.text().strip() if description is None else str(description).strip()
        amount = parse_float(self.bal_amount.text(), None) if amount is None else parse_float(amount, None)
        if amount is None or not employee:
            QMessageBox.warning(self, "Баланс", "Введите сотрудника/контакт и корректную сумму.")
            return False
        conn = get_db_connection()
        cursor = conn.execute(
            "UPDATE balance SET date = ?, employee = ?, description = ?, change_amount = ? WHERE id = ?",
            (date, employee, description, amount, balance_id),
        )
        conn.commit()
        updated = cursor.rowcount == 1
        conn.close()
        if not updated:
            QMessageBox.warning(self, "Баланс", "Операция с указанным ID не найдена.")
            self.cancel_balance_edit()
            self.load_balance_table()
            return False
        self.cancel_balance_edit()
        self.load_balance_table()
        QMessageBox.information(self, "Баланс", f"Операция #{balance_id} обновлена.")
        return True

    def cancel_balance_edit(self):
        self.__dict__["_editing_balance_id"] = None
        self.bal_date.setDate(QDate.currentDate())
        self.bal_emp_cb.setCurrentText("")
        self.bal_desc.clear()
        self.bal_amount.clear()
        self.add_balance_button.setText("Добавить операцию")

    def delete_balance_entry(self):
        selected = self.table_balance.currentRow()
        item = self.table_balance.item(selected, 0) if selected >= 0 else None
        balance_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if balance_id is None:
            QMessageBox.information(self, "Баланс", "Выберите операцию для удаления.")
            return False
        confirmation = QMessageBox.question(
            self, "Удаление операции",
            f"Удалить операцию #{balance_id}?\nДата: {item.text()}\n"
            f"Сотрудник/контакт: {self.table_balance.item(selected, 1).text()}\n"
            f"Сумма: {self.table_balance.item(selected, 3).text()}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return False
        conn = get_db_connection()
        cursor = conn.execute("DELETE FROM balance WHERE id = ?", (int(balance_id),))
        conn.commit()
        deleted = cursor.rowcount == 1
        conn.close()
        if deleted:
            self.load_balance_table()
        return deleted

    def edit_company_expense(self, *_):
        selected = self.table_expenses.currentRow()
        id_item = self.table_expenses.item(selected, 0) if selected >= 0 else None
        if id_item is None:
            QMessageBox.information(self, "Расход", "Выберите запись в журнале.")
            return
        expense_id = int(id_item.text())
        conn = get_db_connection()
        row = conn.execute(
            "SELECT id, date, category, description, amount FROM company_expenses WHERE id = ?",
            (expense_id,),
        ).fetchone()
        conn.close()
        if row is None:
            QMessageBox.warning(self, "Расход", "Запись уже отсутствует. Обновите журнал.")
            self.load_expenses_table()
            return

        old_description = str(row["description"] or "")
        is_calculation = old_description.startswith("Авторасчет")
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Изменить запись #{expense_id}")
        dialog.setMinimumWidth(480)
        form = QFormLayout(dialog)
        expense_date = create_date_field(str(row["date"]))
        category = QComboBox()
        category.setEditable(True)
        category.addItems([
            "Инструменты и расходники", "Аренда склада / офиса",
            "Лизинг и топливо транспорта (Ford Transit)",
            "Бухгалтерия и страховка", "Прочие накладные расходы",
            str(row["category"]),
        ])
        category.setCurrentText(str(row["category"]))
        description = QLineEdit(old_description)
        amount = QLineEdit(str(row["amount"]))
        amount.setPlaceholderText("Сумма в SEK")
        if is_calculation:
            category.setEnabled(False)
            description.setReadOnly(True)
            note = QLabel(
                "У расчётной записи можно исправить дату/сумму. Категория и основание сохранены "
                "для аудита и защиты от повторного расчёта. Ставки не подтверждают налоговое обязательство."
            )
            note.setWordWrap(True)
            form.addRow(note)
        else:
            expense_date.setMinimumDate(self.exp_date_from.date())
            expense_date.setMaximumDate(self.exp_date_to.date())
        form.addRow("Дата:", expense_date)
        form.addRow("Категория:", category)
        form.addRow("Комментарий / основание:", description)
        form.addRow("Сумма (SEK):", amount)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить изменения")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        updated_amount = parse_float(amount.text(), None)
        if updated_amount is None or updated_amount < 0:
            QMessageBox.warning(self, "Расход", "Введите корректную неотрицательную сумму в SEK.")
            return
        if is_calculation:
            updated_category = str(row["category"])
            updated_description = old_description
        else:
            updated_category = category.currentText().strip()
            updated_description = description.text().strip()
        self.update_company_expense(
            expense_id, expense_date.text(), updated_category, updated_description, updated_amount
        )

    def update_company_expense(self, expense_id, expense_date, category, description, amount):
        if not str(description).startswith("Авторасчет"):
            if not self.exp_date_from.date() <= create_date_field(expense_date).date() <= self.exp_date_to.date():
                QMessageBox.warning(self, "Расход", "Дата ручного расхода должна входить в выбранный период.")
                return False
        conn = get_db_connection()
        cursor = conn.execute(
            "UPDATE company_expenses SET date = ?, category = ?, description = ?, amount = ? WHERE id = ?",
            (expense_date, category, description, amount, int(expense_id)),
        )
        conn.commit()
        updated = cursor.rowcount == 1
        conn.close()
        if not updated:
            QMessageBox.warning(self, "Расход", "Запись не найдена. Обновите журнал.")
            return False
        self.load_expenses_table()
        self.background_sync_with_cloud()
        return True

    def _create_object_summary_frame(self, heading):
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.addWidget(QLabel(f"<b>{heading}</b>"))
        row = QHBoxLayout()
        labels = {}
        for key, title in (
            ("gross", "Подтверждено с НДС"),
            ("paid", "Оплачено по записям"),
            ("remaining", "Остаток по записям"),
            ("materials", "Материалы"),
            ("other", "Прочие расходы"),
            ("drafts", "Черновики"),
            ("reviews", "Требуют проверки"),
        ):
            cell = QVBoxLayout()
            cell.addWidget(QLabel(title))
            value = QLabel("—")
            value.setWordWrap(True)
            value.setStyleSheet("font-weight: bold; color: #63b3ed; border: none;")
            cell.addWidget(value)
            row.addLayout(cell, 1)
            labels[key] = value
        layout.addLayout(row)
        return frame, labels

    def _set_object_expenses_period_enabled(self, enabled):
        self.object_expenses_period_controls.setVisible(enabled)
        self.object_expenses_period_frame.setVisible(enabled)
        self.load_object_expense_overview()

    def load_object_expense_objects(self, *_):
        selected_id = self.object_expenses_object.currentData()
        conn = get_db_connection()
        objects = conn.execute("SELECT id, name, markning FROM objects ORDER BY name").fetchall()
        conn.close()
        self.object_expenses_object.blockSignals(True)
        self.object_expenses_object.clear()
        for row in objects:
            label = str(row["name"])
            if row["markning"]:
                label += f" ({row['markning']})"
            self.object_expenses_object.addItem(label, int(row["id"]))
        if selected_id is not None:
            selected_index = self.object_expenses_object.findData(selected_id)
            if selected_index >= 0:
                self.object_expenses_object.setCurrentIndex(selected_index)
        self.object_expenses_object.blockSignals(False)
        if not objects:
            self.object_expenses_selected_label.setText("В справочнике пока нет объектов.")
            self.object_expenses_table.setRowCount(0)
            return
        self.load_object_expense_overview()

    def _format_object_currency_totals(self, totals, key):
        entries = []
        for currency, values in sorted(totals.items()):
            amount = parse_decimal(values.get(key)) or Decimal(0)
            if amount:
                entries.append(f"{currency} {amount:,.2f}")
        return " · ".join(entries) if entries else "0.00"

    def _format_currency_totals(self, totals):
        if not totals:
            return "0.00"
        return " | ".join(
            f"{currency} {amount:,.2f}"
            for currency, amount in sorted(totals.items())
        )

    def _expense_object_candidates(self):
        conn = get_db_connection()
        objects = conn.execute("SELECT id, name, markning, company FROM objects ORDER BY name").fetchall()
        candidates = []
        for row in objects:
            aliases = conn.execute(
                "SELECT alias FROM expense_object_aliases WHERE object_id = ? ORDER BY alias",
                (row["id"],),
            ).fetchall()
            candidates.append({
                "id": int(row["id"]), "name": str(row["name"]),
                "markning": str(row["markning"] or ""), "company": str(row["company"] or ""),
                "aliases": [str(alias[0]) for alias in aliases],
            })
        conn.close()
        return candidates

    def _update_supplier_document_record(self, conn, document_id, source_path, fields, allocations, status, allow_duplicate):
        existing = conn.execute(
            "SELECT stored_path, file_sha256 FROM supplier_documents WHERE id = ?", (int(document_id),)
        ).fetchone()
        if existing is None:
            raise ValueError("Документ не найден.")
        if status == "confirmed":
            net, vat, gross = (parse_decimal(fields.get(key)) for key in ("net_amount", "vat_amount", "gross_amount"))
            if None in (net, vat, gross) or abs(net + vat - gross) > Decimal("0.02"):
                raise ValueError("Нетто + НДС не сходятся с итоговой суммой.")
        file_hash = existing["file_sha256"]
        if source_path:
            from expense_documents import sha256_file
            file_hash = sha256_file(source_path)
        duplicates = find_document_duplicates(conn, fields, file_hash, exclude_document_id=document_id)
        if duplicates and not allow_duplicate:
            raise ValueError("Найден возможный дубль документа; сохранение отменено.")
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("BEGIN IMMEDIATE")
        supplier_name = str(fields.get("supplier_name") or "").strip()
        supplier_org = str(fields.get("supplier_org_number") or "").strip() or None
        supplier_vat = str(fields.get("supplier_vat_number") or "").strip() or None
        supplier_id = None
        if supplier_name:
            found = conn.execute(
                "SELECT id FROM expense_suppliers WHERE name = ? COLLATE NOCASE AND COALESCE(org_number, '') = COALESCE(?, '') ORDER BY id LIMIT 1",
                (supplier_name, supplier_org),
            ).fetchone()
            if found:
                supplier_id = int(found[0])
            else:
                supplier_id = int(conn.execute(
                    "INSERT INTO expense_suppliers (name, org_number, vat_number, created_at) VALUES (?, ?, ?, ?)",
                    (supplier_name, supplier_org, supplier_vat, datetime.now(timezone.utc).isoformat()),
                ).lastrowid)
        object_ids = [int(item["object_id"]) for item in allocations if item.get("object_id") is not None]
        if object_ids:
            placeholders = ",".join("?" for _ in object_ids)
            object_names = [str(item[0]) for item in conn.execute(
                f"SELECT name FROM objects WHERE id IN ({placeholders}) ORDER BY name", object_ids
            ).fetchall()]
        else:
            object_names = []
        conn.execute("""
            UPDATE supplier_documents SET supplier_id=?, supplier_name=?, supplier_org_number=?, supplier_vat_number=?,
                document_type=?, invoice_number=?, payment_reference=?, document_date=?, due_date=?, currency_code=?,
                net_amount=?, vat_amount=?, gross_amount=?, amount_due=?, expense_category=?, is_reverse_charge=?,
                document_comment=?, verification_status=?, payment_status=?, recognized_text=?, review_reasons=?, updated_at=? WHERE id=?
        """, (
            supplier_id, supplier_name or None, supplier_org, supplier_vat, fields["document_type"],
            fields.get("invoice_number"), fields.get("payment_reference"), fields.get("document_date"), fields.get("due_date"),
            fields["currency_code"], decimal_text(fields.get("net_amount")), decimal_text(fields.get("vat_amount")),
            decimal_text(fields.get("gross_amount")), decimal_text(fields.get("amount_due")), fields["expense_category"],
            int(bool(fields.get("reverse_charge"))), str(fields.get("document_comment") or ""), status,
            fields.get("payment_status") or "unknown",
            fields.get("raw_text") or "", "\n".join(fields.get("review_reasons") or []), now, int(document_id),
        ))
        conn.execute("DELETE FROM supplier_document_vat_lines WHERE document_id=?", (int(document_id),))
        conn.execute("DELETE FROM supplier_document_allocations WHERE document_id=?", (int(document_id),))
        for line in fields.get("vat_lines") or []:
            conn.execute(
                "INSERT INTO supplier_document_vat_lines (document_id,vat_rate,taxable_base,vat_amount,note) VALUES (?,?,?,?,?)",
                (int(document_id), str(line.get("rate") or ""), decimal_text(line.get("taxable_base")), decimal_text(line.get("amount")) or "0.00", str(line.get("note") or "")),
            )
        for allocation in allocations:
            conn.execute(
                "INSERT INTO supplier_document_allocations (document_id,object_id,allocated_net,allocated_vat,allocated_gross,allocation_note) VALUES (?,?,?,?,?,?)",
                (int(document_id), allocation.get("object_id"), decimal_text(allocation.get("net_amount")), decimal_text(allocation.get("vat_amount")), decimal_text(allocation.get("gross_amount")) or "0.00", str(allocation.get("note") or "")),
            )
        stored_path = existing["stored_path"]
        destination = None
        if stored_path and Path(stored_path).is_file():
            try:
                destination, _ = store_original_file(
                    stored_path, document_storage_root(), int(document_id), object_names
                )
                if destination != Path(stored_path):
                    conn.execute(
                        "UPDATE supplier_documents SET stored_path = ?, updated_at = ? WHERE id = ?",
                        (str(destination), now, int(document_id)),
                    )
            except Exception:
                conn.rollback()
                if destination and destination != Path(stored_path):
                    destination.unlink(missing_ok=True)
                raise
        try:
            conn.commit()
        except Exception:
            conn.rollback()
            if destination and stored_path and destination != Path(stored_path):
                destination.unlink(missing_ok=True)
            raise
        if destination and stored_path and destination != Path(stored_path):
            old_path = Path(stored_path)
            if old_path.is_relative_to(document_storage_root()):
                old_path.unlink(missing_ok=True)
        return int(document_id)

    def record_selected_document_payment(self, *_):
        row = self.table_supplier_documents.currentRow()
        id_item = self.table_supplier_documents.item(row, 0) if row >= 0 else None
        if id_item is None:
            QMessageBox.information(self, "Оплата", "Выберите документ.")
            return
        document_id = int(id_item.text())
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Платёж по документу #{document_id}")
        form = QFormLayout(dialog)
        paid_date = create_date_field(QDate.currentDate().toString("yyyy-MM-dd"))
        amount = QLineEdit()
        reference = QLineEdit()
        conn = get_db_connection()
        document = conn.execute(
            "SELECT currency_code, amount_due, gross_amount FROM supplier_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        paid = sum((parse_decimal(item[0]) or Decimal(0) for item in conn.execute(
            "SELECT amount FROM supplier_document_payments WHERE document_id = ?", (document_id,)
        ).fetchall()), Decimal(0))
        conn.close()
        if document:
            due = parse_decimal(document["amount_due"]) or parse_decimal(document["gross_amount"])
            if due is not None:
                amount.setText(decimal_text(max(Decimal(0), due - paid)) or "")
                amount.setPlaceholderText(str(document["currency_code"]))
        form.addRow("Дата оплаты:", paid_date)
        form.addRow("Сумма:", amount)
        form.addRow("Платёжная ссылка / комментарий:", reference)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        conn = get_db_connection()
        try:
            from expense_documents import record_document_payment
            status = record_document_payment(conn, document_id, paid_date.text(), amount.text(), reference.text())
        except Exception as error:
            conn.close()
            QMessageBox.warning(self, "Оплата не записана", str(error))
            return
        conn.close()
        QMessageBox.information(self, "Оплата", f"Платёж сохранён. Состояние: {status}.")
        self.load_expense_documents()
    def _api_load_object_expense_overview(self, *_):
        if not hasattr(self, "object_expenses_table"):
            return
        object_id = self.object_expenses_object.currentData()
        if object_id is None:
            self.object_expenses_selected_label.setText("Выберите объект проекта.")
            self.object_expenses_table.setRowCount(0)
            return

        period_enabled = self.object_expenses_period_enabled.isChecked()
        date_from = self.object_expenses_date_from.text().strip() if period_enabled else ""
        date_to = self.object_expenses_date_to.text().strip() if period_enabled else ""
        if period_enabled and date_from > date_to:
            self.object_expenses_selected_label.setText("Некорректный период; проверьте даты.")
            self.object_expenses_table.setRowCount(0)
            return

        try:
            lifetime = request_expense_api(
                "GET", f"/api/expense-objects/{int(object_id)}/expenses"
            )
            selected = lifetime
            if period_enabled:
                selected = request_expense_api(
                    "GET", f"/api/expense-objects/{int(object_id)}/expenses",
                    params={"date_from": date_from, "date_to": date_to},
                )
        except Exception as error:
            self.object_expenses_selected_label.setText(f"Объектный реестр недоступен: {error}")
            self.object_expenses_table.setRowCount(0)
            return

        object_info = lifetime["object"]
        object_name = object_info["name"]
        self.object_expenses_selected_label.setText(
            f"{object_name} · {date_from} — {date_to}" if period_enabled else f"{object_name} · весь срок проекта"
        )

        def apply_summary(labels, source):
            totals = source["summary"]
            for key in ("gross", "net", "vat", "materials", "other"):
                labels[key].setText(self._format_object_currency_totals(totals, key))
            labels["drafts"].setText(str(source["draft_count"]))
            labels["reviews"].setText(str(source["review_count"]))

        apply_summary(self.object_expenses_lifetime_labels, lifetime)
        if period_enabled:
            apply_summary(self.object_expenses_period_labels, selected)

        rows = selected["documents"]
        self.object_expenses_table.setRowCount(len(rows))
        for row_index, document in enumerate(rows):
            currency = document["currency_code"]
            values = (
                document["id"], document["document_date"] or "—",
                document["supplier_name"] or "Не указан", document["invoice_number"] or "—",
                document["expense_category"],
                f"{currency} {document['object_net']}" if document["object_net"] is not None else "—",
                f"{currency} {document['object_vat']}" if document["object_vat"] is not None else "—",
                f"{currency} {document['object_gross'] or '0.00'}",
                f"{currency} {document['gross_amount']}" if document["gross_amount"] else "—",
                f"{currency} {document['unallocated_gross']}" if document["unallocated_gross"] else "0.00",
                document["verification_status"],
            )
            for column, value in enumerate(values):
                self.object_expenses_table.setItem(row_index, column, QTableWidgetItem(str(value)))
            self.object_expenses_table.item(row_index, 0).setData(Qt.ItemDataRole.UserRole, document["id"])

    def open_object_expense_document(self, row, _column):
        id_item = self.object_expenses_table.item(row, 0)
        if id_item is None:
            return
        document_id = int(id_item.data(Qt.ItemDataRole.UserRole) or id_item.text())
        try:
            document = request_expense_api("GET", f"/api/expense-documents/{document_id}")["document"]
            source_path = self._download_expense_original(document) if document["has_original"] else None
        except Exception as error:
            QMessageBox.warning(self, "Документ", f"Не удалось открыть основание: {error}")
            return
        fields = dict(document)
        fields["raw_text"] = document.get("recognized_text") or ""
        suggestions = match_objects(fields["raw_text"], self._expense_object_candidates())
        self._expense_document_review_dialog(
            document_id, source_path, fields, suggestions, document["allocations"]
        )

    def setup_expenses_tab(self):
        layout = QVBoxLayout(self.tab_expenses)
        self.exp_document_tabs = QTabWidget(self.tab_expenses)
        self.exp_documents_page = QWidget()
        page_layout = QVBoxLayout(self.exp_documents_page)

        period_row = QHBoxLayout()
        today = QDate.currentDate()
        month_start = QDate(today.year(), today.month(), 1)
        month_end = month_start.addMonths(1).addDays(-1)
        self.exp_date_from = create_date_field(month_start.toString("yyyy-MM-dd"))
        self.exp_date_to = create_date_field(month_end.toString("yyyy-MM-dd"))
        period_row.addWidget(QLabel("Период:"))
        period_row.addWidget(self.exp_date_from)
        period_row.addWidget(QLabel("по"))
        period_row.addWidget(self.exp_date_to)
        self.exp_filter_object = QComboBox()
        period_row.addWidget(QLabel("Объект:"))
        period_row.addWidget(self.exp_filter_object, 1)
        self.exp_filter_unallocated = QCheckBox("Только нераспределённые")
        period_row.addWidget(self.exp_filter_unallocated)
        page_layout.addLayout(period_row)

        totals_row = QHBoxLayout()
        for attribute, label in (
            ("exp_docs_gross_lbl", "Сумма с НДС"),
            ("exp_docs_vat_lbl", "Известный НДС"),
            ("exp_docs_net_lbl", "Сумма без НДС"),
            ("exp_docs_unallocated_lbl", "Осталось распределить"),
            ("exp_docs_review_lbl", "Требуют проверки"),
        ):
            value = QLabel("—")
            value.setWordWrap(True)
            value.setToolTip(label)
            value.setStyleSheet("font-weight: bold; color: #63b3ed; border: none;")
            setattr(self, attribute, value)
            totals_row.addWidget(QLabel(f"{label}:"))
            totals_row.addWidget(value)
        page_layout.addLayout(totals_row)

        self.exp_api_status_lbl = QLabel("")
        self.exp_api_status_lbl.setWordWrap(True)
        page_layout.addWidget(self.exp_api_status_lbl)

        self.table_supplier_documents = QTableWidget(0, 7)
        self.table_supplier_documents.setHorizontalHeaderLabels([
            "ID", "Дата", "Поставщик", "Объект / объекты", "Сумма с НДС",
            "НДС", "Распределение / статус",
        ])
        self.table_supplier_documents.setColumnHidden(0, True)
        document_header = self.table_supplier_documents.horizontalHeader()
        for column, width in {1: 95, 2: 155, 4: 125, 5: 105, 6: 205}.items():
            document_header.resizeSection(column, width)
        document_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table_supplier_documents.setAlternatingRowColors(True)
        self.table_supplier_documents.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table_supplier_documents.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table_supplier_documents.cellDoubleClicked.connect(self.open_selected_document_original)
        page_layout.addWidget(self.table_supplier_documents, 1)

        actions = QHBoxLayout()
        upload_button = QPushButton("Загрузить PDF / фото")
        upload_button.clicked.connect(self.upload_expense_document)
        manual_button = QPushButton("Добавить вручную")
        manual_button.clicked.connect(self.open_manual_document_dialog)
        from incoming_documents import enabled as incoming_enabled
        mail_button = QPushButton("Входящие документы")
        mail_button.setVisible(incoming_enabled())
        mail_button.clicked.connect(self.open_incoming_documents)
        edit_button = QPushButton("Изменить")
        edit_button.clicked.connect(self.review_selected_document)
        reprocess_button = QPushButton("Распознать сохранённый оригинал")
        reprocess_button.clicked.connect(self.reprocess_selected_document)
        vat_button = QPushButton("НДС по позициям")
        vat_button.clicked.connect(lambda: self.calculate_allocation_vat(self.table_supplier_documents))
        delete_button = QPushButton("Удалить")
        delete_button.setObjectName("danger")
        delete_button.clicked.connect(self.delete_selected_expense_document)
        open_button = QPushButton("Открыть оригинал")
        open_button.clicked.connect(self.open_selected_document_original)
        for button in (upload_button, mail_button, manual_button, edit_button, reprocess_button, vat_button, delete_button, open_button):
            actions.addWidget(button)
        page_layout.addLayout(actions)

        self.exp_document_tabs.addTab(self.exp_documents_page, "Все документы")
        self.exp_document_tabs.addTab(self.tab_object_expenses, "По объектам")
        self.exp_document_tabs.currentChanged.connect(self._on_expense_view_changed)
        layout.addWidget(self.exp_document_tabs, 1)
        self.exp_date_from.dateChanged.connect(self.load_expense_documents)
        self.exp_date_to.dateChanged.connect(self.load_expense_documents)
        self.exp_filter_object.currentIndexChanged.connect(self.load_expense_documents)
        self.exp_filter_unallocated.toggled.connect(self.load_expense_documents)

    def open_mail_queue(self):
        QMessageBox.information(self, "Почта отключена", "Почтовое получение в интеграционный кандидат не включено.")
        return

    def incoming_register_snapshot(self):
        cached = getattr(self, "_expense_read_cache", {})
        if (None, None) not in cached or not getattr(self, "_expense_online", False):
            raise ValueError("Для проверки дублей нужен актуальный снимок обычного реестра. Передача остановлена.")
        documents = list(cached[(None, None)])
        conn = get_db_connection()
        try:
            documents.extend(json.loads(row[0]) for row in conn.execute(
                "SELECT payload_json FROM expense_document_outbox WHERE operation='upsert'"))
        finally:
            conn.close()
        return documents

    def refresh_incoming_capabilities(self):
        self._incoming_costs_supported = False
        result = request_expense_api("GET", "/api/expense-documents/capabilities")
        self._incoming_costs_supported = "object_costs_v1" in result.get("features", [])

    def incoming_delivery_pending(self, client_id):
        conn = get_db_connection()
        try:
            return conn.execute("SELECT state,last_error FROM expense_document_outbox WHERE client_id=?",
                                (client_id,)).fetchone()
        finally:
            conn.close()

    def refresh_incoming_register(self):
        result = request_expense_api("GET", "/api/expense-documents")
        self._expense_read_cache[(None, None)] = list(result["documents"])
        self._expense_online = True
        self._expense_read_dirty = False

    def incoming_after_queue(self):
        self._expense_read_dirty = True
        self.load_expense_documents()
        self.background_sync_with_cloud()

    def open_incoming_documents(self):
        from incoming_documents import enabled, open_incoming
        if not enabled():
            QMessageBox.information(self, "Входящие документы", "Функция выключена; фоновых запусков нет.")
            return
        viewer = getattr(self, "_incoming_viewer", None)
        if viewer is None:
            try:
                profile = hashlib.sha256(str(Path(DB_FILE).resolve()).casefold().encode("utf-8")).hexdigest()[:16]
                parent = desktop_auth.app_data_dir() / "incoming-documents"
                parent.mkdir(parents=True, exist_ok=True)
                viewer = open_incoming(self, parent / profile)
            except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
                logging.exception("Не удалось открыть входящие документы")
                QMessageBox.warning(self, "Входящие документы", str(error))
                return
            self._incoming_viewer = viewer
            viewer.finished.connect(lambda: setattr(self, "_incoming_viewer", None))
        viewer.show()
        viewer.raise_()
        viewer.activateWindow()

    def closeEvent(self, event):
        incoming = getattr(self, "_incoming_viewer", None)
        if incoming is not None and not incoming.close():
            event.ignore()
            return
        viewer = getattr(self, "_mail_viewer", None)
        if viewer is not None and not viewer.close():
            event.ignore()
            return
        super().closeEvent(event)

    def _on_expense_view_changed(self, index):
        self.load_expense_document_filters()
        if index == 0:
            self.load_expense_documents()
        else:
            self.load_object_expense_overview()

    def setup_object_expenses_tab(self):
        layout = QVBoxLayout(self.tab_object_expenses)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Объект:"))
        self.object_expenses_object = QComboBox()
        self.object_expenses_object.setMinimumWidth(320)
        controls.addWidget(self.object_expenses_object, 1)
        self.object_expenses_period_enabled = QCheckBox("За период")
        controls.addWidget(self.object_expenses_period_enabled)
        self.object_expenses_date_from = create_date_field(self.exp_date_from.text())
        self.object_expenses_date_to = create_date_field(self.exp_date_to.text())
        controls.addWidget(self.object_expenses_date_from)
        controls.addWidget(QLabel("по"))
        controls.addWidget(self.object_expenses_date_to)
        layout.addLayout(controls)

        self.object_expenses_selected_label = QLabel("Выберите объект или общие расходы фирмы.")
        self.object_expenses_selected_label.setWordWrap(True)
        layout.addWidget(self.object_expenses_selected_label)
        summary = QHBoxLayout()
        self.object_expenses_total_lbl = QLabel("Итого с НДС: —")
        self.object_expenses_vat_lbl = QLabel("Известный НДС: —")
        self.object_expenses_net_lbl = QLabel("Сумма без НДС: —")
        self.object_expenses_unallocated_lbl = QLabel("Не распределено: —")
        for label in (
            self.object_expenses_total_lbl, self.object_expenses_vat_lbl,
            self.object_expenses_net_lbl, self.object_expenses_unallocated_lbl,
        ):
            label.setWordWrap(True)
            summary.addWidget(label)
        layout.addLayout(summary)

        self.object_expenses_table = QTableWidget(0, 7)
        self.object_expenses_table.setHorizontalHeaderLabels([
            "ID", "Дата", "Поставщик", "Документ", "Итого с НДС",
            "НДС", "Статус распределения",
        ])
        self.object_expenses_table.setColumnHidden(0, True)
        object_header = self.object_expenses_table.horizontalHeader()
        for column, width in {1: 95, 3: 165, 4: 125, 5: 105, 6: 185}.items():
            object_header.resizeSection(column, width)
        object_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.object_expenses_table.setAlternatingRowColors(True)
        self.object_expenses_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.object_expenses_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.object_expenses_table.cellDoubleClicked.connect(self.open_selected_object_expense_original)
        layout.addWidget(self.object_expenses_table, 1)

        actions = QHBoxLayout()
        edit_button = QPushButton("Изменить документ")
        edit_button.clicked.connect(self.review_selected_object_expense)
        vat_button = QPushButton("НДС по позициям")
        vat_button.clicked.connect(lambda: self.calculate_allocation_vat(self.object_expenses_table))
        delete_button = QPushButton("Удалить документ")
        delete_button.setObjectName("danger")
        delete_button.clicked.connect(self.delete_selected_object_expense)
        open_button = QPushButton("Открыть оригинал")
        open_button.clicked.connect(self.open_selected_object_expense_original)
        for button in (edit_button, vat_button, delete_button, open_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.object_expenses_object.currentIndexChanged.connect(self.load_object_expense_overview)
        self.object_expenses_period_enabled.toggled.connect(self.load_object_expense_overview)
        self.object_expenses_date_from.dateChanged.connect(self.load_object_expense_overview)
        self.object_expenses_date_to.dateChanged.connect(self.load_object_expense_overview)
        self.load_expense_document_filters()

    def _invalidate_expense_reads(self):
        if getattr(self, "_async_expense_reads", False):
            self._expense_read_attempts.clear()
            self._expense_refresh_again = self._expense_read_busy

    def _schedule_expense_read(self, key):
        self._expense_requested_key = key
        if self._expense_read_busy or time.monotonic() - self._expense_read_attempts.get(key, -1000) < 30:
            return
        self._expense_read_busy = True
        self._expense_read_attempts[key] = time.monotonic()
        task = ExpenseReadTask(key)
        self._expense_read_task = task
        task.signals.completed.connect(self._on_expense_read_completed)
        QThreadPool.globalInstance().start(task)

    def _on_expense_read_completed(self, key, objects, documents, error):
        refresh_again = getattr(self, "_expense_refresh_again", False)
        self._expense_refresh_again = False
        self._expense_read_busy = False
        self._expense_read_task = None
        self._expense_online = not error
        self._expense_read_error = error
        self._expense_read_attempts[key] = time.monotonic()
        if not error:
            incoming_changed = (self._expense_read_cache.get(key) != list(documents)
                                or self._expense_objects != list(objects))
            self._expense_read_cache[key] = list(documents)
            self._expense_objects = list(objects)
            incoming = getattr(self, "_incoming_viewer", None)
            if incoming is not None and incoming_changed:
                incoming.refresh_server_review()
        if refresh_again:
            self._expense_read_attempts.clear()
        if getattr(self, "tabs", None) and self.tabs.currentIndex() == 3:
            self.load_expense_document_filters()
            self.load_expense_documents()
            if self.exp_document_tabs.currentIndex() == 1:
                self.load_object_expense_overview()
        requested = getattr(self, "_expense_requested_key", key)
        if requested != key or refresh_again:
            self._schedule_expense_read(requested)

    def incoming_allocation_objects(self):
        objects = list(getattr(self, "_expense_objects", []))
        known = {item["id"] for item in objects}
        conn = get_db_connection()
        try:
            objects.extend(dict(row) for row in conn.execute("""
                SELECT server_id AS id,sync_id,name,markning,company,is_active FROM objects
                WHERE server_id IS NOT NULL AND is_active=0 ORDER BY company,name,id
            """) if row["id"] not in known)
        finally:
            conn.close()
        return objects

    def _expense_server_objects(self):
        if getattr(self, "_async_expense_reads", False):
            if not hasattr(self, "_expense_objects"):
                conn = get_db_connection()
                try:
                    self._expense_objects = [dict(row) for row in conn.execute("""
                        SELECT server_id AS id,sync_id,name,markning,company,is_active FROM objects
                        WHERE server_id IS NOT NULL AND is_active=1 ORDER BY company,name,id
                    """)]
                finally:
                    conn.close()
            return list(getattr(self, "_expense_objects", [])), self._expense_online
        try:
            result = request_expense_api("GET", "/api/expense-objects")
            objects = result.get("objects", [])
            self._expense_objects = objects
            return objects, True
        except Exception as error:
            cached = getattr(self, "_expense_objects", None)
            if cached is None:
                conn = get_db_connection()
                try:
                    cached = [dict(row) for row in conn.execute("""
                        SELECT server_id AS id, sync_id, name, markning, company,
                               is_active FROM objects
                        WHERE server_id IS NOT NULL AND is_active = 1
                        ORDER BY company, name, id
                    """).fetchall()]
                finally:
                    conn.close()
                self._expense_objects = cached
            self.exp_api_status_lbl.setText(
                f"Сервер недоступен; используется кэш объектов. Расходы останутся в очереди. {error}"
            )
            return cached, False

    def load_expense_document_filters(self, *_):
        objects, online = self._expense_server_objects()
        for combo, include_all in (
            (getattr(self, "exp_filter_object", None), True),
            (getattr(self, "object_expenses_object", None), False),
        ):
            if combo is None:
                continue
            selected_id = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            if include_all:
                combo.addItem("Все объекты", None)
            else:
                combo.addItem("Выберите объект", None)
            combo.addItem("Общие расходы фирмы", "company")
            for item in objects:
                combo.addItem(
                    f"{item['name']} ({item.get('markning') or 'без кода'}) · {item.get('company') or ''}",
                    int(item["id"]),
                )
            index = combo.findData(selected_id)
            if index >= 0:
                combo.setCurrentIndex(index)
            elif not include_all and combo.count() > 1:
                combo.setCurrentIndex(1)
            combo.blockSignals(False)
        if online:
            self.exp_api_status_lbl.setText("Общий реестр доступен.")

    def _expense_document_snapshot(self, date_from=None, date_to=None, fresh=False):
        params = {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        online = True
        if getattr(self, "_async_expense_reads", False) and not fresh:
            key = (date_from, date_to)
            documents = list(self._expense_read_cache.get(key, []))
            online = self._expense_online
            self._schedule_expense_read(key)
        else:
            try:
                result = request_expense_api("GET", "/api/expense-documents", params=params)
                documents = list(result.get("documents", []))
            except Exception as error:
                documents = []
                online = False
                self.exp_api_status_lbl.setText(
                    f"Нет связи с общим реестром; показана локальная очередь. {error}"
                )

        conn = get_db_connection()
        try:
            queued = conn.execute("""
                  SELECT client_id, operation, server_document_id, server_version, revision,
                       payload_json, attachment_paths_json, state, last_error
                FROM expense_document_outbox
                WHERE state IN ('pending', 'conflict')
                ORDER BY updated_at DESC
            """).fetchall()
        finally:
            conn.close()
        object_names = {int(item["id"]): str(item["name"]) for item in getattr(self, "_expense_objects", [])}
        pending_by_client = {}
        deleted_clients = set()
        for row in queued:
            if row["operation"] == "delete":
                deleted_clients.add(row["client_id"])
                continue
            payload = json.loads(row["payload_json"])
            allocations = []
            for allocation in payload.get("allocations", []):
                object_id = allocation.get("object_id")
                allocations.append({
                    **allocation,
                    "object_id": int(object_id) if object_id is not None else None,
                    "object_name": object_names.get(int(object_id)) if object_id is not None else
                        ("Общие расходы фирмы" if allocation.get("destination", "company") == "company" else "Не выбрано"),
                    "company_expense": object_id is None and allocation.get("destination", "company") == "company",
                })
            gross = parse_decimal(payload.get("gross_amount"))
            vat = parse_decimal(payload.get("vat_amount"))
            allocated_gross = sum(
                (parse_decimal(item.get("gross_amount")) or Decimal(0) for item in allocations), Decimal(0)
            ) if all(item.get("gross_amount") is not None for item in allocations) else None
            net_basis = payload.get("allocation_basis") == "net"
            net = parse_decimal(payload.get("net_amount")) if net_basis else (gross - vat if gross is not None and vat is not None else None)
            allocated_net_values = [parse_decimal(item.get("net_amount")) for item in allocations]
            allocated_net = sum(allocated_net_values, Decimal(0)) if all(value is not None for value in allocated_net_values) else None
            allocated_vat_values = [parse_decimal(item.get("vat_amount")) for item in allocations]
            allocated_vat = (
                sum(allocated_vat_values, Decimal(0))
                if vat is not None and all(value is not None for value in allocated_vat_values)
                else None
            )
            pending_by_client[row["client_id"]] = {
                **payload,
                "id": row["server_document_id"] or f"pending:{row['client_id']}",
                "client_id": row["client_id"],
                "server_document_id": row["server_document_id"],
                "server_version": row["server_version"],
                "allocations": allocations,
                "net_amount": decimal_text(net),
                "allocated_net": decimal_text(allocated_net),
                "unallocated_net": decimal_text(net - allocated_net) if net is not None and allocated_net is not None else None,
                "allocated_gross": decimal_text(allocated_gross),
                "unallocated_gross": decimal_text(gross - allocated_gross) if gross is not None and allocated_gross is not None else None,
                "allocated_vat": decimal_text(allocated_vat),
                "unallocated_vat": decimal_text(vat - allocated_vat) if vat is not None and allocated_vat is not None else None,
                "fully_allocated": (net is not None and net == allocated_net) if net_basis else gross is not None and gross == allocated_gross,
                "sync_state": row["state"],
                "sync_error": row["last_error"],
                "local_attachment_paths": json.loads(row["attachment_paths_json"] or "[]"),
                "attachments": [],
            }

        merged = [doc for doc in documents if doc.get("client_id") not in deleted_clients]
        positions = {doc.get("client_id"): index for index, doc in enumerate(merged)}
        for client_id, pending in pending_by_client.items():
            if client_id in positions:
                remote_document = merged[positions[client_id]]
                if pending.get("sync_state") == "conflict":
                    remote_document["sync_state"] = "conflict"
                    remote_document["sync_error"] = pending.get("sync_error")
                    remote_document["local_conflict_payload"] = pending
                    remote_document["local_attachment_paths"] = pending.get("local_attachment_paths", [])
                    remote_document["server_document_id"] = int(remote_document["id"])
                    remote_document["server_version"] = int(remote_document.get("sync_version") or 1)
                else:
                    pending["server_document_id"] = pending.get("server_document_id") or int(remote_document["id"])
                    pending["server_version"] = pending.get("server_version") or remote_document.get("sync_version")
                    merged[positions[client_id]] = pending
            else:
                merged.append(pending)
        if getattr(self, "_async_expense_reads", False):
            if self._expense_read_busy:
                self.exp_api_status_lbl.setText(f"Показаны доступные данные; обновление в фоне. В очереди: {len(queued)}.")
            elif getattr(self, "_expense_read_error", ""):
                self.exp_api_status_lbl.setText(f"Показаны сохранённые данные и очередь; обновление недоступно: {self._expense_read_error}")
            else:
                self.exp_api_status_lbl.setText(f"Общий реестр обновлён. В очереди: {len(queued)}.")
        elif online:
            self.exp_api_status_lbl.setText(
                f"Общий реестр доступен. В очереди: {len(queued)}."
            )
        return merged, online

    @staticmethod
    def _expense_money_label(totals):
        return " · ".join(
            f"{currency} {amount:,.2f}"
            for currency, amount in sorted(totals.items())
        ) or "0.00"

    @staticmethod
    def _expense_distribution_label(document):
        gross = parse_decimal(document.get("gross_amount"))
        net_basis = document.get("allocation_basis") == "net"
        remaining = parse_decimal(document.get("unallocated_net" if net_basis else "unallocated_gross"))
        allocations = document.get("allocations") or []
        if not allocations:
            return "Не распределено"
        if remaining is not None and remaining != 0:
            return f"Частично; осталось {document['currency_code']} {remaining:,.2f}" + (" без НДС" if net_basis else " с НДС")
        if document.get("sync_state") == "conflict":
            return "Конфликт синхронизации"
        if document.get("sync_state") == "pending":
            return "Ожидает синхронизации"
        return "Распределено"

    def load_expense_documents(self, *_):
        if not hasattr(self, "table_supplier_documents"):
            return
        date_from = self.exp_date_from.text().strip()
        date_to = self.exp_date_to.text().strip()
        if date_from > date_to:
            self.table_supplier_documents.setRowCount(0)
            self.exp_api_status_lbl.setText("Проверьте период: начало позже окончания.")
            return
        selected_filter = self.exp_filter_object.currentData()
        only_unallocated = self.exp_filter_unallocated.isChecked()
        documents, _online = self._expense_document_snapshot(date_from, date_to)
        filtered = []
        for document in documents:
            allocations = document.get("allocations") or []
            if selected_filter == "company" and not any(item.get("object_id") is None and item.get("destination", "company") == "company" for item in allocations):
                continue
            if isinstance(selected_filter, int) and not any(item.get("object_id") == selected_filter for item in allocations):
                continue
            remaining = parse_decimal(document.get("unallocated_net" if document.get("allocation_basis") == "net" else "unallocated_gross"))
            if only_unallocated and (remaining is None or remaining == 0):
                continue
            filtered.append(document)

        selected_client_id = None
        current_row = self.table_supplier_documents.currentRow()
        if current_row >= 0 and self.table_supplier_documents.item(current_row, 0):
            old_doc = self.table_supplier_documents.item(current_row, 0).data(Qt.ItemDataRole.UserRole)
            selected_client_id = old_doc.get("client_id") if isinstance(old_doc, dict) else None
        gross_totals, vat_totals, net_totals, unallocated_totals = {}, {}, {}, {}
        reviews = 0
        for document in filtered:
            currency = str(document.get("currency_code") or "SEK")
            if document.get("verification_status") == "confirmed":
                for field, totals in (("gross_amount", gross_totals), ("vat_amount", vat_totals), ("net_amount", net_totals)):
                    amount = parse_decimal(document.get(field))
                    if amount is not None:
                        totals[currency] = totals.get(currency, Decimal(0)) + amount
            if document.get("verification_status") != "confirmed":
                reviews += 1
            remaining = parse_decimal(document.get("unallocated_gross"))
            if remaining is not None and remaining != 0:
                unallocated_totals[currency] = unallocated_totals.get(currency, Decimal(0)) + remaining
        self.exp_docs_gross_lbl.setText(self._expense_money_label(gross_totals))
        self.exp_docs_vat_lbl.setText(self._expense_money_label(vat_totals))
        self.exp_docs_net_lbl.setText(self._expense_money_label(net_totals))
        self.exp_docs_unallocated_lbl.setText(self._expense_money_label(unallocated_totals))
        self.exp_docs_review_lbl.setText(str(reviews))

        self.table_supplier_documents.setCurrentCell(-1, -1)
        self.table_supplier_documents.setRowCount(len(filtered))
        restored_row = -1
        for row_index, document in enumerate(filtered):
            allocations = document.get("allocations") or []
            object_names = sorted({
                item.get("object_name") or "Общие расходы фирмы"
                for item in allocations
            })
            object_label = ", ".join(object_names) if object_names else "Не распределено"
            currency = str(document.get("currency_code") or "SEK")
            vat_text = f"{document['vat_amount']} {currency}" if document.get("vat_amount") is not None else "Неизвестно"
            status = self._expense_distribution_label(document)
            if document.get("sync_error"):
                status += f"; {document['sync_error']}"
            processing = document.get("processing") or {}
            if processing:
                status += f"; обработка: {processing.get('state')}"
                if processing.get("errors"):
                    status += "; " + "; ".join(processing["errors"])
            values = (
                str(document.get("id") or document.get("client_id")),
                document.get("document_date") or "—",
                document.get("supplier_name") or "Не указан",
                object_label,
                f"{document['gross_amount']} {currency}" if document.get("gross_amount") is not None else "—",
                vat_text,
                f"{status} · {document.get('verification_status', 'draft')}",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                self.table_supplier_documents.setItem(row_index, column, item)
            self.table_supplier_documents.item(row_index, 0).setData(Qt.ItemDataRole.UserRole, document)
            if document.get("client_id") == selected_client_id:
                restored_row = row_index
        if restored_row >= 0:
            self.table_supplier_documents.setCurrentCell(restored_row, 1)

    def load_object_expense_overview(self, *_):
        if not hasattr(self, "object_expenses_table"):
            return
        object_id = self.object_expenses_object.currentData()
        if object_id is None:
            self.object_expenses_table.setRowCount(0)
            return
        date_from = self.object_expenses_date_from.text().strip() if self.object_expenses_period_enabled.isChecked() else None
        date_to = self.object_expenses_date_to.text().strip() if self.object_expenses_period_enabled.isChecked() else None
        if date_from and date_to and date_from > date_to:
            self.object_expenses_table.setRowCount(0)
            self.object_expenses_selected_label.setText("Проверьте период: начало позже окончания.")
            return
        documents, _online = self._expense_document_snapshot(date_from, date_to)
        is_company = object_id == "company"
        selected_id = None if is_company else int(object_id)
        rows = []
        totals = {"gross": {}, "vat": {}, "net": {}, "unallocated": {}}
        unknown = {"gross": False, "vat": False, "net": False}
        for document in documents:
            allocations = [
                item for item in (document.get("allocations") or [])
                if (item.get("object_id") is None and item.get("destination", "company") == "company" if is_company else item.get("object_id") == selected_id)
            ]
            if not allocations:
                continue
            currency = str(document.get("currency_code") or "SEK")
            if document.get("verification_status") == "confirmed":
                for allocation in allocations:
                    for field, key in (("gross_amount", "gross"), ("vat_amount", "vat"), ("net_amount", "net")):
                        amount = parse_decimal(allocation.get(field))
                        if amount is not None:
                            totals[key][currency] = totals[key].get(currency, Decimal(0)) + amount
                        else:
                            unknown[key] = True
            remaining = parse_decimal(document.get("unallocated_gross"))
            if remaining is not None and remaining:
                totals["unallocated"][currency] = totals["unallocated"].get(currency, Decimal(0)) + remaining
            rows.append((document, allocations))
        self.object_expenses_total_lbl.setText("Итого с НДС: " + ("Неизвестно (не все части указаны)" if unknown["gross"] else self._expense_money_label(totals["gross"])))
        self.object_expenses_vat_lbl.setText("НДС частей: " + ("Неизвестно (не все части указаны)" if unknown["vat"] else self._expense_money_label(totals["vat"])))
        self.object_expenses_net_lbl.setText(f"Сумма без НДС: {self._expense_money_label(totals['net'])}")
        self.object_expenses_unallocated_lbl.setText(
            f"Не распределено (отдельно): {self._expense_money_label(totals['unallocated'])}"
        )
        self.object_expenses_selected_label.setText(
            "Общие расходы фирмы" if is_company else self.object_expenses_object.currentText()
        )
        self.object_expenses_table.setRowCount(len(rows))
        for row_index, (document, allocations) in enumerate(rows):
            currency = str(document.get("currency_code") or "SEK")
            allocated_gross_values = [parse_decimal(item.get("gross_amount")) for item in allocations]
            allocated_gross = sum(allocated_gross_values, Decimal(0)) if all(value is not None for value in allocated_gross_values) else None
            allocated_net_values = [parse_decimal(item.get("net_amount")) for item in allocations]
            allocated_net = sum(allocated_net_values, Decimal(0)) if all(value is not None for value in allocated_net_values) else None
            amount_label = f"{allocated_gross:,.2f} {currency}" if allocated_gross is not None else "Неизвестно"
            allocated_vat_values = [parse_decimal(item.get("vat_amount")) for item in allocations]
            allocated_vat = sum(allocated_vat_values, Decimal(0)) if all(value is not None for value in allocated_vat_values) else None
            values = (
                str(document.get("id") or document.get("client_id")),
                document.get("document_date") or "—", document.get("supplier_name") or "Не указан",
                document.get("invoice_number") or "Документ расходов",
                amount_label,
                f"{allocated_vat:,.2f} {currency}" if allocated_vat is not None else "Неизвестно",
                self._expense_distribution_label(document),
            )
            for column, value in enumerate(values):
                self.object_expenses_table.setItem(row_index, column, QTableWidgetItem(str(value)))
            self.object_expenses_table.item(row_index, 4).setToolTip(
                f"Без НДС: {allocated_net:,.2f} {currency}" if allocated_net is not None else "Сумма без НДС неизвестна")
            self.object_expenses_table.item(row_index, 0).setData(Qt.ItemDataRole.UserRole, document)

    def _store_expense_originals(self, paths, client_id):
        from expense_documents import document_storage_root

        sources = [Path(path) for path in paths or []]
        total_size = sum(path.stat().st_size for path in sources if path.is_file())
        if total_size > 25 * 1024 * 1024:
            raise ValueError("Общий размер оригиналов не должен превышать 25 МБ.")
        destination_root = document_storage_root() / "outbox" / safe_folder_name(client_id)
        destination_root.mkdir(parents=True, exist_ok=True)
        stored_paths = []
        try:
            for index, source in enumerate(sources):
                if not source.is_file():
                    raise FileNotFoundError(f"Файл не найден: {source.name}")
                safe_name = safe_folder_name(source.stem)
                target = destination_root / f"{index + 1:02d}_{safe_name}{source.suffix.casefold()}"
                temporary = target.with_suffix(target.suffix + ".part")
                shutil.copy2(source, temporary)
                if sha256_file(source) != sha256_file(temporary):
                    temporary.unlink(missing_ok=True)
                    raise OSError(f"Не удалось проверить копию {source.name}.")
                temporary.replace(target)
                stored_paths.append(str(target.resolve()))
            return stored_paths
        except Exception:
            for stored_path in stored_paths:
                Path(stored_path).unlink(missing_ok=True)
            raise

    def _expense_object_choices(self, combo, selected=None):
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Выберите объект", "unselected")
        combo.addItem("Общие расходы фирмы", "company")
        for item in getattr(self, "_expense_objects", []):
            combo.addItem(
                f"{item['name']} ({item.get('markning') or 'без кода'}) · {item.get('company') or ''}",
                int(item["id"]),
            )
        index = combo.findData(selected)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif combo.count() > 2:
            combo.setCurrentIndex(2)
        combo.blockSignals(False)

    def _review_net_invoice(self, document, source_paths, parsed_fields, server_document=None):
        fields = {**parsed_fields, **document}
        source_text = str(fields.get("raw_text") or fields.get("recognized_text") or "")
        non_invoice_type = fields.get("non_invoice_document_type") or non_invoice_document_type(source_text)
        context = invoice_context(source_text)
        context["prepaid"] = context["prepaid"] or fields.get("prepaid") is True
        context["reverse_charge"] = context["reverse_charge"] or fields.get("reverse_charge") is True
        server_document = server_document or document
        client_id = str(fields.get("client_id") or uuid.uuid4())
        preview_paths = list(source_paths or fields.get("local_attachment_paths") or [])
        if not preview_paths and fields.get("has_original"):
            preview_paths = self._expense_document_original_paths(server_document)
        dialog = QDialog(self)
        dialog.setWindowTitle("Проверка фактуры и распределение расходов")
        dialog.resize(1040, 760)
        dialog.setMinimumSize(860, 650)
        layout = QVBoxLayout(dialog)
        top = QSplitter(Qt.Orientation.Horizontal)
        top.setChildrenCollapsible(False)
        gallery = None
        if preview_paths:
            try:
                gallery = DocumentGalleryDialog(preview_paths, "Оригинал", dialog)
                gallery.setWindowFlags(Qt.WindowType.Widget)
                top.addWidget(gallery)
                dialog.finished.connect(lambda _result: gallery.close())
            except Exception as error:
                top.addWidget(QLabel(f"Оригинал недоступен: {error}"))
        else:
            top.addWidget(QLabel("Оригинал не прикреплён"))
        details = QWidget()
        form = QFormLayout(details)
        if non_invoice_type:
            note = QLabel(
                "Подтверждение заказа нельзя подтвердить как расход. Загрузите фактуру или чек."
                if non_invoice_type == "order_confirmation"
                else "Решение о транспортном налоге нельзя подтвердить как фактуру."
            )
            note.setWordWrap(True)
            form.addRow("Тип документа:", note)
        inputs = {}
        for key, label in (
            ("supplier_name", "Поставщик"), ("invoice_number", "Номер фактуры"),
            ("document_date", "Дата (ГГГГ-ММ-ДД)"), ("due_date", "Срок оплаты (ГГГГ-ММ-ДД)"), ("currency_code", "Валюта"),
            ("net_amount", "Сумма без НДС"), ("vat_amount", "НДС фактуры"),
            ("rounding_amount", "Округление"), ("amount_due", "К оплате"),
            ("payment_reference", "OCR / reference"),
        ):
            value = fields.get(key)
            if key == "currency_code" and value == "UNKNOWN":
                value = None
            widget = QLineEdit(str(value) if value is not None else "")
            if key == "rounding_amount":
                widget.setPlaceholderText("Не указано в документе")
            elif key == "currency_code":
                widget.setPlaceholderText("Не указана: выберите валюту")
            elif key == "due_date":
                widget.setPlaceholderText("Не указан или требует проверки")
            widget.setObjectName("invoice_" + key)
            inputs[key] = widget
            form.addRow(("Остаток к оплате" if key == "amount_due" and context["prepaid"] else label) + ":", widget)
        original_due_text = inputs["due_date"].text().strip()
        due_origin = fields.get("due_date_kind") or ("manual" if original_due_text else "absent")
        due_origin_note = QLabel()
        due_origin_note.setObjectName("invoiceDueDateOrigin")
        due_origin_note.setWordWrap(True)
        form.addRow("Источник срока:", due_origin_note)
        due_reviewed = QCheckBox("Срок оплаты и его источник сверены")
        due_reviewed.setObjectName("invoiceDueDateReviewed")
        due_reviewed.setChecked(fields.get("due_date_reviewed") is True)
        due_reviewed.setVisible(bool(fields.get("due_date_review_required") or due_origin in {"calculated", "ambiguous"}))
        form.addRow(due_reviewed)
        document_total = QLabel()
        document_total.setObjectName("invoiceDocumentTotal")
        if context["prepaid"]:
            form.addRow("Сумма документа с НДС:", document_total)
            prepaid_note = QLabel("Предоплаченный документ: общий итог не равен остатку к оплате. Пока доступен только черновик.")
            prepaid_note.setWordWrap(True)
            form.addRow(prepaid_note)
        tax_reviewed = QCheckBox("Обратное начисление проверено по оригиналу")
        tax_reviewed.setObjectName("invoiceTaxReviewed")
        tax_reviewed.setChecked(fields.get("tax_reviewed") is True)
        if context["reverse_charge"]:
            form.addRow(tax_reviewed)
        top.addWidget(details)
        top.setSizes([500, 500])
        layout.addWidget(top, 3)
        table = QTableWidget(0, 6)
        table.setObjectName("invoiceAllocations")
        table.setHorizontalHeaderLabels(["Заказ / пометка", "Без НДС", "Назначение", "Объект", "Категория", "Источник"])
        table.setMinimumHeight(155)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(table, 2)
        source_rows = []
        objects = list(getattr(self, "_expense_objects", []))
        distribution = QLabel()
        distribution.setObjectName("invoiceDistribution")
        issues_label = QLabel()
        issues_label.setObjectName("invoiceIssues")
        issues_label.setWordWrap(True)
        row_actions = QHBoxLayout()
        add_row_button = QPushButton("Добавить строку")
        add_row_button.setObjectName("invoiceAddAllocation")
        remove_row_button = QPushButton("Удалить строку")
        remove_row_button.setObjectName("invoiceRemoveAllocation")
        row_actions.addWidget(add_row_button)
        row_actions.addWidget(remove_row_button)
        row_actions.addWidget(distribution, 1)
        layout.addLayout(row_actions)
        layout.addWidget(issues_label)
        ambiguity_reviewed = QCheckBox("Неоднозначные строки исправлены и сверены с оригиналом")
        ambiguity_reviewed.setObjectName("invoiceAmbiguityReviewed")
        ambiguity_reviewed.setVisible(any(item.get("ambiguous") for item in fields.get("order_proposals") or []))
        layout.addWidget(ambiguity_reviewed)
        extra_toggle = QPushButton("Дополнительно")
        extra_toggle.setCheckable(True)
        extra = QTextEdit()
        extra.setReadOnly(True)
        extra.setMaximumHeight(90)
        extra.setPlainText(str(fields.get("raw_text") or fields.get("recognized_text") or "")
                           + "\n\n" + "\n".join(fields.get("review_reasons") or []))
        extra.hide()
        extra_toggle.toggled.connect(extra.setVisible)
        layout.addWidget(extra_toggle)
        layout.addWidget(extra)
        actions = QHBoxLayout()
        draft = QPushButton("Сохранить черновик")
        draft.setObjectName("invoiceSaveDraft")
        confirm = QPushButton("Подтвердить распределение")
        confirm.setObjectName("invoiceConfirm")
        cancel = QPushButton("Отмена")
        actions.addWidget(draft)
        actions.addStretch()
        actions.addWidget(confirm)
        actions.addWidget(cancel)
        layout.addLayout(actions)

        def values():
            current = {key: widget.text().strip() for key, widget in inputs.items()}
            current.update({**context, "tax_reviewed": tax_reviewed.isChecked(), "recognized_text": source_text})
            current.update(due_date_edit(fields, current["due_date"], due_reviewed.isChecked()))
            rows = []
            for index, original in enumerate(source_rows):
                destination = table.cellWidget(index, 2).currentData()
                object_id = table.cellWidget(index, 3).currentData() if destination == "object" else None
                amount = table.item(index, 1).text().strip()
                unchanged = parse_invoice_money(amount) == parse_invoice_money(original.get("net_amount"))
                rows.append({
                    **original, "original_marker": table.item(index, 0).text().strip(),
                    "net_amount": amount or None, "destination": destination,
                    "object_id": object_id, "expense_category": table.cellWidget(index, 4).text().strip(),
                    "vat_amount": original.get("vat_amount") if unchanged else None,
                    "gross_amount": None,
                    "ambiguous": bool(original.get("ambiguous")) and not ambiguity_reviewed.isChecked(),
                })
            return current, rows

        def refresh(*_):
            current, rows = values()
            origin_names = {"explicit": "Явно указан в документе", "calculated": "Рассчитан по условиям оплаты — требуется сверка",
                            "manual": "Изменён вручную", "ambiguous": "Неоднозначный срок — укажите проверенную дату",
                            "absent": "Срок в документе не найден"}
            due_origin_note.setText(origin_names.get(current["due_date_kind"], "Источник требует проверки"))
            due_reviewed.setVisible(current["due_date_review_required"])
            net_value = parse_invoice_money(current["net_amount"])
            vat_value = parse_invoice_money(current["vat_amount"])
            document_total.setText(decimal_text(net_value + vat_value) if net_value is not None and vat_value is not None else "Неизвестно")
            allocated = sum((parse_invoice_money(row["net_amount"]) or Decimal(0) for row in rows
                             if row.get("destination") == "company"
                             or row.get("destination") == "object" and row.get("object_id") is not None), Decimal(0))
            total = parse_invoice_money(current["net_amount"])
            remaining = decimal_text(total - allocated) if total is not None else "неизвестно"
            distribution.setText(f"Без НДС: распределено {decimal_text(allocated)} / осталось {remaining}")
            issues = net_invoice_issues(current, rows)
            if not current["supplier_name"]:
                issues.append("Укажите поставщика.")
            if not current["invoice_number"]:
                issues.append("Укажите номер фактуры.")
            try:
                datetime.strptime(current["document_date"], "%Y-%m-%d")
            except ValueError:
                issues.append("Укажите корректную дату фактуры.")
            if current["currency_code"].upper() not in {"SEK", "EUR", "NOK", "DKK", "USD", "GBP", "CHF", "PLN"}:
                issues.append("Выберите валюту.")
            issues_label.setText("Для подтверждения: " + " ".join(issues[:3]) if issues else "Суммы согласованы. Назначения выбраны.")
            confirm.setEnabled(not issues and non_invoice_type is None)

        def append_row(original=None):
            original = dict(original or {})
            row = table.rowCount()
            table.blockSignals(True)
            table.insertRow(row)
            marker = str(original.get("original_marker") or original.get("project_code") or "")
            table.setItem(row, 0, QTableWidgetItem(marker))
            table.item(row, 0).setToolTip("Заказ: " + str(original.get("order_id") or "не указан"))
            table.setItem(row, 1, QTableWidgetItem(str(original.get("net_amount") or "")))
            destination = QComboBox()
            for label, value in (("Выберите назначение", "unselected"), ("На объект", "object"), ("Общие расходы фирмы", "company")):
                destination.addItem(label, value)
            object_combo = QComboBox()
            object_combo.addItem("Выберите объект", None)
            for item in objects:
                object_combo.addItem(f"{item['name']} ({item.get('markning') or ''})", int(item["id"]))
            selected = original.get("object_id")
            if selected is None and original.get("project_code"):
                matches = [int(item["id"]) for item in objects if str(item.get("markning") or "").casefold()
                           == str(original["project_code"]).casefold()]
                selected = matches[0] if len(matches) == 1 else None
            if selected is not None:
                object_combo.setCurrentIndex(max(0, object_combo.findData(selected)))
            selected_destination = original.get("destination") or ("object" if selected is not None else "unselected")
            destination.setCurrentIndex(max(0, destination.findData(selected_destination)))
            category = QLineEdit(str(original.get("expense_category") or ""))
            category.setPlaceholderText("Необязательно")
            table.setCellWidget(row, 2, destination)
            table.setCellWidget(row, 3, object_combo)
            table.setCellWidget(row, 4, category)
            sources = original.get("sources") or []
            source_button = QPushButton("Стр. " + str(sources[0]["page"]) if sources else "Нет страницы")
            source_button.setEnabled(bool(gallery and sources))
            if gallery and sources:
                source_button.clicked.connect(lambda _checked=False, source=sources[0]: gallery.show_source(
                    int(source.get("attachment_index") or 0), int(source["page"])))
            table.setCellWidget(row, 5, source_button)
            source_rows.append(original)
            table.blockSignals(False)

            def destination_changed(*_):
                object_combo.setEnabled(destination.currentData() == "object")
                category.setEnabled(destination.currentData() == "company")
                refresh()

            destination.currentIndexChanged.connect(destination_changed)
            object_combo.currentIndexChanged.connect(refresh)
            category.textChanged.connect(refresh)
            destination_changed()

        def remove_row():
            index = table.currentRow()
            if index >= 0:
                table.removeRow(index)
                source_rows.pop(index)
                refresh()

        initial_rows = fields.get("order_proposals") or fields.get("allocations") or [{"net_amount": fields.get("net_amount")}]
        for row in initial_rows:
            append_row(row)
        table.itemChanged.connect(refresh)
        for widget in inputs.values():
            widget.textChanged.connect(refresh)
        ambiguity_reviewed.toggled.connect(refresh)
        tax_reviewed.toggled.connect(refresh)
        due_reviewed.toggled.connect(refresh)
        add_row_button.clicked.connect(lambda: append_row())
        remove_row_button.clicked.connect(remove_row)

        def save(status):
            current, rows = values()
            if status == "confirmed" and non_invoice_type:
                QMessageBox.warning(
                    dialog, "Тип документа",
                    "Подтверждение заказа нельзя подтвердить как расход. Загрузите фактуру или чек."
                    if non_invoice_type == "order_confirmation"
                    else "Решение о транспортном налоге нельзя подтвердить как фактуру.",
                )
                return
            if current["due_date"]:
                date_errors = due_date_issues({"due_date": current["due_date"], "due_date_kind": "manual"})
                if date_errors:
                    QMessageBox.warning(dialog, "Срок оплаты", " ".join(date_errors))
                    return
            for key in ("net_amount", "vat_amount", "rounding_amount", "amount_due"):
                if current[key] and parse_invoice_money(current[key]) is None:
                    QMessageBox.warning(dialog, "Формат суммы", "Исправьте формат денежной суммы перед сохранением.")
                    return
            if any(row.get("net_amount") and parse_invoice_money(row["net_amount"]) is None for row in rows):
                QMessageBox.warning(dialog, "Распределение", "Исправьте формат суммы без НДС в строках.")
                return
            if status == "confirmed" and (net_invoice_issues(current, rows) or not confirm.isEnabled()):
                return
            net = parse_invoice_money(current["net_amount"])
            vat = parse_invoice_money(current["vat_amount"])
            payload = {**current, "client_id": client_id, "allocation_basis": "net",
                       "currency_code": current["currency_code"].upper() or "UNKNOWN",
                       "gross_amount": decimal_text(net + vat) if net is not None and vat is not None else None,
                       "verification_status": status, "allocations": rows, "order_proposals": rows,
                       "recognized_text": fields.get("raw_text") or fields.get("recognized_text") or "",
                       "document_comment": fields.get("document_comment") or "",
                       "expense_category": fields.get("expense_category") or "Прочие расходы"}
            known_hashes = [item["sha256"] for item in fields.get("attachments") or [] if item.get("sha256")]
            if known_hashes:
                payload["required_attachment_sha256"] = known_hashes
            staged = []
            try:
                if source_paths:
                    staged = self._store_expense_originals(source_paths, client_id)
                server_id = server_document.get("server_document_id")
                if server_id is None and isinstance(server_document.get("id"), int):
                    server_id = server_document["id"]
                self.queue_expense_document_operation(
                    client_id, "upsert", payload, server_document_id=server_id,
                    server_version=server_document.get("server_version") or server_document.get("sync_version"),
                    attachment_paths=staged if source_paths else fields.get("local_attachment_paths"),
                )
            except Exception as error:
                for path in staged:
                    Path(path).unlink(missing_ok=True)
                QMessageBox.warning(dialog, "Не сохранено", str(error))
                return
            dialog.accept()
            self.load_expense_documents()
            self.background_sync_with_cloud()

        draft.clicked.connect(lambda: save("draft"))
        confirm.clicked.connect(lambda: save("confirmed"))
        cancel.clicked.connect(dialog.reject)
        refresh()
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _expense_document_review_dialog(
        self, document=None, source_paths=None, parsed_fields=None, recognition_message=""
    ):
        document = dict(document or {})
        if document.get("usage") == "object_cost" and not document.get("local_conflict_payload"):
            self.open_incoming_documents()
            viewer = getattr(self, "_incoming_viewer", None)
            if viewer is not None:
                card = viewer.store.conn.execute(
                    "SELECT document_id FROM register WHERE client_id=?", (document.get("client_id"),)).fetchone()
                if card is not None:
                    if viewer.keep_unsaved():
                        viewer.selector.setCurrentIndex(viewer.selector.findData(card["document_id"]))
                        viewer.select_document()
                    return True
        server_document = dict(document)
        local_conflict_payload = document.get("local_conflict_payload")
        if isinstance(local_conflict_payload, dict):
            document = dict(local_conflict_payload)
            document["local_attachment_paths"] = server_document.get("local_attachment_paths", [])
        parsed_fields = dict(parsed_fields or {})
        source_paths = list(source_paths or [])
        non_invoice_type = (
            document.get("non_invoice_document_type")
            or parsed_fields.get("non_invoice_document_type")
            or non_invoice_document_type(str(
                document.get("raw_text") or document.get("recognized_text")
                or parsed_fields.get("raw_text") or parsed_fields.get("recognized_text") or ""
            ))
        )
        self.load_expense_document_filters()
        object_cost = document.get("usage") == "object_cost"
        if not object_cost and (document.get("allocation_basis") == "net" or parsed_fields.get("allocation_basis") == "net"
                or document.get("order_proposals") or parsed_fields.get("order_proposals")):
            if document and document.get("allocation_basis") != "net":
                document = {**document, "allocation_basis": "net", "rounding_amount": "0.00",
                            "amount_due": document.get("gross_amount")}
            return self._review_net_invoice(document, source_paths, parsed_fields, server_document)
        client_id = str(document.get("client_id") or parsed_fields.get("client_id") or uuid.uuid4())
        existing_allocations = list(document.get("allocations") or [])
        dialog = QDialog(self)
        dialog.setWindowTitle("Документ расходов" if document.get("client_id") else "Новый расход")
        dialog.setMinimumWidth(620)
        form = QFormLayout(dialog)
        if non_invoice_type and not object_cost:
            note = QLabel(
                "Подтверждение заказа нельзя подтвердить как расход. Загрузите фактуру или чек."
                if non_invoice_type == "order_confirmation"
                else "Решение о транспортном налоге нельзя подтвердить как фактуру."
            )
            note.setWordWrap(True)
            form.addRow("Тип документа:", note)

        supplier = QLineEdit(str(document.get("supplier_name") or parsed_fields.get("supplier_name") or ""))
        supplier.setObjectName("expenseSupplier")
        date_value = document.get("document_date") or parsed_fields.get("document_date") or QDate.currentDate().toString("yyyy-MM-dd")
        document_date = create_date_field(str(date_value))
        document_date.setObjectName("expenseDocumentDate")
        due_fields = {**parsed_fields, **document}
        due_date_input = QLineEdit(str(due_fields.get("due_date") or ""))
        due_date_input.setObjectName("expenseDueDate")
        due_date_input.setPlaceholderText("ГГГГ-ММ-ДД; не указан — оставить пустым")
        due_date_reviewed = QCheckBox("Срок оплаты и источник сверены")
        due_date_reviewed.setChecked(due_fields.get("due_date_reviewed") is True)
        due_date_reviewed.setVisible(bool(due_fields.get("due_date_review_required") or due_fields.get("due_date_kind") in {"calculated", "ambiguous"}))
        gross_value = document.get("gross_amount") if document else parsed_fields.get("gross_amount")
        vat_value = document.get("vat_amount") if document else parsed_fields.get("vat_amount")
        gross_amount = QLineEdit(str(gross_value) if gross_value is not None else "")
        gross_amount.setObjectName("expenseGrossInput")
        gross_amount.setPlaceholderText("0.00")
        vat_amount = QLineEdit(str(vat_value) if vat_value is not None else "")
        vat_amount.setObjectName("expenseVatInput")
        vat_amount.setPlaceholderText("Неизвестно")
        net_preview = QLabel("Неизвестно")
        currency = QComboBox()
        currency.setObjectName("expenseCurrency")
        currency.addItems(["SEK", "EUR", "NOK", "DKK", "USD", "GBP", "CHF", "PLN", "UNKNOWN"])
        currency.setCurrentText(str(document.get("currency_code") or parsed_fields.get("currency_code") or "SEK").upper())
        single_object = QComboBox()
        single_object.setObjectName("expenseObject")
        selected_object = "unselected" if document else None
        if len(existing_allocations) == 1:
            selected_object = existing_allocations[0].get("object_id")
            if selected_object is None:
                selected_object = "company"
        elif not document:
            current_filter = self.exp_filter_object.currentData()
            selected_object = current_filter if isinstance(current_filter, int) else None
        self._expense_object_choices(single_object, selected_object)
        comment = QLineEdit(str(document.get("document_comment") or parsed_fields.get("document_comment") or ""))
        comment.setObjectName("expenseComment")
        status = QComboBox()
        status.setObjectName("expenseVerificationStatus")
        for label, value in (
            ("Черновик", "draft"),
            ("Требует проверки", "requires_review"),
            ("Подтверждён", "confirmed"),
        ):
            status.addItem(label, value)
        status_index = status.findData(str(document.get("verification_status") or "requires_review"))
        status.setCurrentIndex(max(0, status_index))

        form.addRow("Поставщик:", supplier)
        form.addRow("Дата:", document_date)
        if not object_cost:
            form.addRow("Срок оплаты:", due_date_input)
            form.addRow(due_date_reviewed)
        amount_row = QHBoxLayout()
        amount_row.addWidget(QLabel("Сумма с НДС:"))
        amount_row.addWidget(gross_amount)
        amount_row.addWidget(QLabel("Валюта:"))
        amount_row.addWidget(currency)
        form.addRow(amount_row)
        form.addRow("НДС (moms):", vat_amount)
        form.addRow("Сумма без НДС:", net_preview)
        form.addRow("Объект:", single_object)
        form.addRow("Комментарий:", comment)
        form.addRow("Статус:", status)
        recognition_label = QLabel(recognition_message or "Распознавание не запускалось; значения можно ввести вручную.")
        recognition_label.setWordWrap(True)
        form.addRow("Распознавание:", recognition_label)

        split_check = QCheckBox("Разделить по объектам")
        split_check.setObjectName("expenseSplitByObjects")
        split_table = QTableWidget(0, 3)
        split_table.setObjectName("expenseAllocations")
        split_table.setHorizontalHeaderLabels(["Объект / фирма", "Сумма с НДС", "НДС"])
        split_table.setVisible(False)
        split_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        split_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        split_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        distribution_total = QLabel("Распределено: 0.00; осталось: —")
        distribution_note = QLabel(
            "НДС в частях указывается вручную. Неизвестный НДС не распределяется пропорционально."
        )
        distribution_note.setWordWrap(True)
        allocation_buttons = QHBoxLayout()
        add_allocation = QPushButton("Добавить строку")
        add_allocation.setObjectName("expenseAddAllocation")
        remove_allocation = QPushButton("Удалить строку")
        allocation_buttons.addWidget(add_allocation)
        allocation_buttons.addWidget(remove_allocation)
        form.addRow(split_check)
        form.addRow(split_table)
        form.addRow(distribution_total)
        form.addRow(distribution_note)
        form.addRow(allocation_buttons)

        additional_toggle = QPushButton("Дополнительно")
        additional_toggle.setCheckable(True)
        additional_fields = QFrame()
        additional_fields.setVisible(False)
        additional_layout = QFormLayout(additional_fields)
        invoice_number = QLineEdit(str(document.get("invoice_number") or parsed_fields.get("invoice_number") or ""))
        category = QLineEdit(str(document.get("expense_category") or parsed_fields.get("expense_category") or "Прочие расходы"))
        additional_layout.addRow("Номер документа:", invoice_number)
        additional_layout.addRow("Категория:", category)
        additional_toggle.toggled.connect(additional_fields.setVisible)
        form.addRow(additional_toggle)
        form.addRow(additional_fields)

        def append_allocation(object_id, allocated_gross, allocated_vat):
            row = split_table.rowCount()
            split_table.insertRow(row)
            object_combo = QComboBox()
            self._expense_object_choices(object_combo, object_id)
            split_table.setCellWidget(row, 0, object_combo)
            split_table.setItem(row, 1, QTableWidgetItem(str(allocated_gross or "")))
            split_table.setItem(row, 2, QTableWidgetItem(str(allocated_vat) if allocated_vat is not None else ""))

        def update_net_preview(*_):
            gross = parse_decimal(gross_amount.text())
            vat = parse_decimal(vat_amount.text())
            net_preview.setText(
                f"{gross - vat:,.2f} {currency.currentText()}"
                if gross is not None and vat is not None else "Неизвестно"
            )
            if not split_check.isChecked():
                allocated = (
                    gross if single_object.currentData() != "unselected" else Decimal(0)
                ) if gross is not None else Decimal(0)
            else:
                allocated = Decimal(0)
                for row in range(split_table.rowCount()):
                    allocated += parse_decimal(split_table.item(row, 1).text() if split_table.item(row, 1) else "") or Decimal(0)
            remaining = gross - allocated if gross is not None else None
            distribution_total.setText(
                f"Распределено: {allocated:,.2f} {currency.currentText()}; "
                f"осталось: {f'{remaining:,.2f} {currency.currentText()}' if remaining is not None else 'неизвестно'}"
            )

        def set_split(enabled):
            if enabled and split_table.rowCount() == 0:
                if existing_allocations:
                    for allocation in existing_allocations:
                        selected = allocation.get("object_id")
                        if selected is None and allocation.get("company_expense"):
                            selected = "company"
                        append_allocation(selected, allocation.get("gross_amount"), allocation.get("vat_amount"))
                else:
                    selected = single_object.currentData()
                    append_allocation(selected, gross_amount.text(), vat_amount.text())
            split_table.setVisible(enabled)
            single_object.setVisible(not enabled)
            add_allocation.setVisible(enabled)
            remove_allocation.setVisible(enabled)
            update_net_preview()

        split_check.toggled.connect(set_split)
        add_allocation.clicked.connect(lambda: (append_allocation(None, "0.00", ""), update_net_preview()))
        remove_allocation.clicked.connect(lambda: (split_table.removeRow(split_table.currentRow()) if split_table.currentRow() >= 0 else None, update_net_preview()))
        split_table.itemChanged.connect(update_net_preview)
        for widget in (gross_amount, vat_amount):
            widget.textChanged.connect(update_net_preview)
        currency.currentTextChanged.connect(update_net_preview)

        if len(existing_allocations) > 1:
            split_check.setChecked(True)
        update_net_preview()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить документ")
        def accept_due_date():
            if object_cost:
                dialog.accept()
                return
            if non_invoice_type and not object_cost and status.currentData() == "confirmed":
                QMessageBox.warning(
                    dialog, "Тип документа",
                    "Подтверждение заказа нельзя подтвердить как расход. Загрузите фактуру или чек."
                    if non_invoice_type == "order_confirmation"
                    else "Решение о транспортном налоге нельзя подтвердить как фактуру.",
                )
                return
            candidate = due_date_edit(due_fields, due_date_input.text(), due_date_reviewed.isChecked())
            if candidate["due_date"]:
                format_errors = due_date_issues({"due_date": candidate["due_date"], "due_date_kind": "manual"})
                if format_errors:
                    QMessageBox.warning(dialog, "Срок оплаты", " ".join(format_errors))
                    return
            date_errors = due_date_issues(candidate)
            if status.currentData() == "confirmed" and date_errors and not object_cost:
                QMessageBox.warning(dialog, "Срок оплаты", " ".join(date_errors))
                return
            dialog.accept()

        buttons.accepted.connect(accept_due_date)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False

        due_values = {} if object_cost else due_date_edit(due_fields, due_date_input.text(), due_date_reviewed.isChecked())
        date_errors = due_date_issues(due_values)
        format_errors = due_date_issues({"due_date": due_values.get("due_date"), "due_date_kind": "manual"})
        if not object_cost and (format_errors or status.currentData() == "confirmed" and date_errors):
            QMessageBox.warning(self, "Срок оплаты", " ".join(date_errors))
            return False
        gross = parse_decimal(gross_amount.text())
        vat = parse_decimal(vat_amount.text())
        if gross_amount.text().strip() and gross is None:
            QMessageBox.warning(self, "Расход", "Проверьте сумму с НДС.")
            return False
        if vat_amount.text().strip() and vat is None:
            QMessageBox.warning(self, "Расход", "Проверьте сумму НДС.")
            return False
        allocations = []
        if split_check.isChecked():
            for row in range(split_table.rowCount()):
                object_combo = split_table.cellWidget(row, 0)
                row_gross = parse_decimal(split_table.item(row, 1).text() if split_table.item(row, 1) else "")
                row_vat_text = split_table.item(row, 2).text() if split_table.item(row, 2) else ""
                row_vat = parse_decimal(row_vat_text)
                if row_gross is None or row_gross == 0:
                    continue
                if row_vat_text.strip() and row_vat is None:
                    QMessageBox.warning(self, "Распределение", f"Проверьте НДС в строке {row + 1}.")
                    return False
                selected = object_combo.currentData()
                if selected == "unselected":
                    QMessageBox.warning(self, "Распределение", f"Выберите объект в строке {row + 1}.")
                    return False
                allocations.append({
                    "object_id": None if selected == "company" else selected,
                    "gross_amount": decimal_text(row_gross),
                    "vat_amount": decimal_text(row_vat),
                })
        elif gross is not None:
            selected = single_object.currentData()
            if selected != "unselected":
                allocations.append({
                    "object_id": None if selected == "company" else selected,
                    "gross_amount": decimal_text(gross),
                    "vat_amount": decimal_text(vat),
                })

        payload = {
            "client_id": client_id,
            **due_values,
            "supplier_name": supplier.text().strip(),
            "document_date": document_date.text(),
            "currency_code": currency.currentText().strip().upper() or "SEK",
            "net_amount": decimal_text(gross - vat) if gross is not None and vat is not None else None,
            "vat_amount": decimal_text(vat),
            "gross_amount": decimal_text(gross),
            "expense_category": category.text().strip() or "Прочие расходы",
            "document_comment": comment.text().strip(),
            "invoice_number": invoice_number.text().strip(),
            "document_type": document.get("document_type") or parsed_fields.get("document_type") or "manual",
            "usage": document.get("usage"),
            "verification_status": status.currentData(),
            "allocations": allocations,
        }
        if object_cost:
            payload["cost_operation_id"] = uuid.uuid4().hex
        known_hashes = [item["sha256"] for item in document.get("attachments") or [] if item.get("sha256")]
        if known_hashes:
            payload["required_attachment_sha256"] = known_hashes
        if "raw_text" in parsed_fields:
            payload["recognized_text"] = str(parsed_fields.get("raw_text") or "")
        elif "recognized_text" in document:
            payload["recognized_text"] = str(document.get("recognized_text") or "")
        staged_paths = []
        try:
            if source_paths:
                staged_paths = self._store_expense_originals(source_paths, client_id)
            existing_server_id = server_document.get("server_document_id") or document.get("server_document_id")
            if existing_server_id is None and isinstance(server_document.get("id"), int):
                existing_server_id = int(server_document["id"])
            self.queue_expense_document_operation(
                client_id,
                "upsert",
                payload,
                server_document_id=existing_server_id,
                server_version=server_document.get("server_version") or server_document.get("sync_version"),
                attachment_paths=staged_paths if source_paths else document.get("local_attachment_paths"),
            )
        except Exception as error:
            for path in staged_paths:
                Path(path).unlink(missing_ok=True)
            QMessageBox.warning(self, "Расход", f"Документ не поставлен в очередь: {error}")
            return False
        self.load_expense_documents()
        self.background_sync_with_cloud()
        QMessageBox.information(self, "Расход", "Документ сохранён и поставлен в очередь синхронизации.")
        return True

    def open_manual_document_dialog(self, *_):
        self.load_expense_document_filters()
        self._expense_document_review_dialog(
            document=None,
            parsed_fields={"currency_code": "SEK", "document_type": "manual"},
            recognition_message="Распознавание не запускалось; заполните поля вручную.",
        )

    def upload_expense_document(self, *_):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Добавить один или несколько файлов документа", "",
            "Документы и фотографии (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp)",
        )
        if not paths:
            return
        groups = [[path] for path in paths]
        if len(paths) > 1:
            mode, accepted = QInputDialog.getItem(
                self, "Загрузка фактур", "Как обработать выбранные файлы?",
                ["Отдельная фактура для каждого файла", "Страницы одной фактуры"], 0, False,
            )
            if not accepted:
                return
            if mode == "Страницы одной фактуры":
                groups = [paths]
        for group in groups:
            self._upload_expense_document_paths(group)

    def _upload_expense_document_paths(self, paths):
        try:
            incoming_hashes = [sha256_file(path) for path in paths]
            if len(set(incoming_hashes)) != len(incoming_hashes):
                QMessageBox.warning(self, "Повторный оригинал", "Один и тот же файл выбран несколько раз. Уберите повторные копии.")
                return
            documents, _online = self._expense_document_snapshot(fresh=True)
            for document in documents:
                known_hashes = {item.get("sha256") for item in document.get("attachments") or [] if item.get("sha256")}
                for local_path in document.get("local_attachment_paths") or []:
                    if Path(local_path).is_file():
                        known_hashes.add(sha256_file(local_path))
                if known_hashes.intersection(incoming_hashes):
                    QMessageBox.information(
                        self, "Документ уже загружен",
                        "Этот оригинал уже есть в реестре или очереди синхронизации. "
                        "Открыта существующая карточка; второй расход не создан.",
                    )
                    self._expense_document_review_dialog(document=document)
                    return
        except OSError as error:
            QMessageBox.warning(self, "Оригинал недоступен", str(error))
            return
        from expense_documents import extract_local_document

        extractions = []
        errors = []
        for path in paths:
            try:
                extractions.append((path, extract_local_document(path)))
            except Exception as error:
                errors.append(f"{Path(path).name}: {error}")
        text_parts = [
            f"--- {Path(path).name} ---\n{extraction.text}"
            for path, extraction in extractions if extraction.text.strip()
        ]
        combined_text = "\n\n".join(text_parts)
        source_indices = {str(path): index for index, path in enumerate(paths)}
        order_proposal = parse_project_order_allocations([
            {"source": Path(path).name, "page": page + 1, "text": page_text,
             "attachment_index": source_indices[str(path)]}
            for path, extraction in extractions
            for page, page_text in enumerate(extraction.pages or (extraction.text,))
        ])
        structured_pages = [
            {"source": Path(path).name, "page": page + 1, "text": page_text,
             "attachment_index": source_indices[str(path)],
             "elements": extraction.page_elements[page] if extraction.page_elements else []}
            for path, extraction in extractions
            for page, page_text in enumerate(extraction.pages or (extraction.text,))
        ]
        parsed = parse_invoice_pages(structured_pages) if combined_text else {
            "supplier_name": "", "document_date": None, "gross_amount": None,
            "vat_amount": None, "currency_code": "SEK", "document_type": "manual",
            "document_comment": "", "raw_text": "",
        }
        if errors:
            recognition_message = "Распознавание выполнено не для всех файлов: " + "; ".join(errors)
            parsed["review_reasons"] = [*parsed.get("review_reasons", []), *errors]
        elif combined_text:
            methods = sorted({extraction.method for _, extraction in extractions})
            recognition_message = f"Распознавание выполнено: {', '.join(methods)}. Проверьте и при необходимости исправьте поля."
        else:
            recognition_message = "Распознавание не выполнено; оригинал будет сохранён, заполните поля вручную."
        parsed["raw_text"] = combined_text
        from incoming_documents import enabled as incoming_enabled
        if (len(paths) == 1 and Path(paths[0]).suffix.casefold() == ".pdf"
                and parsed.get("document_type", "invoice") not in ("invoice", "receipt", "credit_note")
                and incoming_enabled()):
            self.open_incoming_documents()
            viewer = getattr(self, "_incoming_viewer", None)
            if viewer is None:
                return
            if not viewer.keep_unsaved():
                return
            try:
                content = Path(paths[0]).read_bytes()
                viewer.store.add_pdf(content, Path(paths[0]).name, {
                    "fields": parsed, "state": "requires_review",
                    "methods": [extraction.method for _, extraction in extractions], "errors": errors})
                document_id = viewer.store.conn.execute(
                    "SELECT id FROM documents WHERE sha256=?", (hashlib.sha256(content).hexdigest(),)).fetchone()[0]
                viewer.reload_documents()
                viewer.selector.setCurrentIndex(viewer.selector.findData(document_id))
            except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
                QMessageBox.warning(self, "Документ не открыт", str(error))
            return
        if "order_proposals" not in parsed:
            parsed["order_proposals"] = order_proposal["orders"]
        parsed["order_proposal_warnings"] = order_proposal["warnings"]
        if order_proposal["warnings"]:
            recognition_message += " " + "; ".join(order_proposal["warnings"])
        self._expense_document_review_dialog(
            document=None, source_paths=paths, parsed_fields=parsed,
            recognition_message=recognition_message,
        )

    def calculate_allocation_vat(self, table):
        document = self._selected_expense_document(table)
        if not document:
            return
        identifier = document.get("server_document_id") or document.get("id")
        if not isinstance(identifier, int) or document.get("sync_state") in {"pending", "conflict"}:
            QMessageBox.information(self, "НДС частей", "Сначала завершите синхронизацию документа и разрешите конфликты.")
            return
        if QMessageBox.question(self, "НДС частей", "Рассчитать НДС по ставкам исходных позиций? Итоги будут сохранены только при полном совпадении с фактурой.") != QMessageBox.StandardButton.Yes:
            return
        try:
            request_expense_api("POST", f"/api/expense-documents/{identifier}/allocation-vat",
                                json={"expected_version": document.get("server_version") or document.get("sync_version")})
            self._invalidate_expense_reads()
            self.load_expense_documents()
            self.load_object_expense_overview()
            QMessageBox.information(self, "НДС частей", "НДС и суммы с НДС сохранены; сумма НДС частей совпадает с фактурой.")
        except Exception as error:
            QMessageBox.warning(self, "НДС частей", str(error))

    def reprocess_selected_document(self, *_):
        document = self._selected_expense_document(self.table_supplier_documents)
        if not document:
            return
        identifier = document.get("server_document_id") or document.get("id")
        if not isinstance(identifier, int):
            QMessageBox.information(self, "Распознавание", "Сначала синхронизируйте локальный черновик.")
            return
        if QMessageBox.question(self, "Распознавание", "Повторно определить поля из сохранённого оригинала? Проверьте результат перед подтверждением.") != QMessageBox.StandardButton.Yes:
            return
        try:
            version = document.get("server_version") or document.get("sync_version")
            if len(document.get("attachments") or []) > 1:
                result = request_expense_api("POST", f"/api/expense-documents/{identifier}/split",
                                             json={"expected_version": version})
                identifiers = result["children"]
            else:
                identifiers = [identifier]
            for selected_id in identifiers:
                current = request_expense_api("GET", f"/api/expense-documents/{selected_id}")["document"]
                request_expense_api("POST", f"/api/expense-documents/{selected_id}/recognize",
                                    json={"expected_version": current["sync_version"], "replace_fields": True})
            self._invalidate_expense_reads()
            self.load_expense_documents()
        except Exception as error:
            QMessageBox.warning(self, "Распознавание", str(error))

    def _selected_expense_document(self, table):
        row = table.currentRow()
        item = table.item(row, 0) if row >= 0 else None
        document = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(document, dict):
            QMessageBox.information(self, "Расход", "Выберите документ.")
            return None
        return document

    def review_selected_document(self, *_):
        document = self._selected_expense_document(self.table_supplier_documents)
        if document:
            self._expense_document_review_dialog(document=document)

    def review_selected_object_expense(self, *_):
        document = self._selected_expense_document(self.object_expenses_table)
        if document:
            self._expense_document_review_dialog(document=document)

    def _delete_expense_document(self, document):
        client_id = str(document.get("client_id") or "")
        if not client_id:
            QMessageBox.warning(self, "Расход", "У документа отсутствует постоянный client_id.")
            return False
        confirmation = QMessageBox.question(
            self, "Удаление расхода",
            "Пометить документ удалённым во всех клиентах? Его синхронизированное удаление нельзя отменить.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return False
        server_id = document.get("server_document_id")
        if server_id is None and isinstance(document.get("id"), int):
            server_id = int(document["id"])
        self.queue_expense_document_operation(
            client_id, "delete", {"client_id": client_id},
            server_document_id=server_id,
            server_version=document.get("server_version") or document.get("sync_version"),
            attachment_paths=None,
        )
        self.load_expense_documents()
        self.load_object_expense_overview()
        self.background_sync_with_cloud()
        return True

    def delete_selected_expense_document(self, *_):
        document = self._selected_expense_document(self.table_supplier_documents)
        if document:
            self._delete_expense_document(document)

    def delete_selected_object_expense(self, *_):
        document = self._selected_expense_document(self.object_expenses_table)
        if document:
            self._delete_expense_document(document)

    def open_selected_document_original(self, *_):
        document = self._selected_expense_document(self.table_supplier_documents)
        if document:
            self._open_expense_document_original(document)

    def open_selected_object_expense_original(self, *_):
        document = self._selected_expense_document(self.object_expenses_table)
        if document:
            self._open_expense_document_original(document)

    def _expense_document_original_paths(self, document):
        local_paths = document.get("local_attachment_paths") or []
        if local_paths:
            paths = [Path(path) for path in local_paths if Path(path).is_file()]
        else:
            paths = []
            attachments = document.get("attachments") or []
            if not attachments and document.get("has_original"):
                attachments = [{"id": None, "original_name": document.get("original_name")}]
            for index, attachment in enumerate(attachments):
                server_id = document.get("server_document_id") or document.get("id")
                if not isinstance(server_id, int):
                    continue
                attachment_id = attachment.get("id")
                endpoint = (
                    f"/api/expense-documents/{server_id}/attachments/{int(attachment_id)}/original"
                    if attachment_id is not None
                    else f"/api/expense-documents/{server_id}/original"
                )
                try:
                    response = requests.get(
                        f"{CLOUD_URL.rstrip('/')}{endpoint}",
                        headers=sync_headers(), timeout=30,
                    )
                    response.raise_for_status()
                    name = safe_folder_name(Path(attachment.get("original_name") or f"attachment-{index + 1}").stem)
                    suffix = Path(attachment.get("original_name") or "document.pdf").suffix or ".pdf"
                    path = Path(self._expense_temp_directory.name) / f"{document.get('client_id', server_id)}_{index + 1}_{name}{suffix}"
                    path.write_bytes(response.content)
                    paths.append(path)
                except Exception as error:
                    QMessageBox.warning(self, "Оригинал", f"Не удалось загрузить вложение: {error}")
                    return []
        return paths

    def _open_expense_document_original(self, document):
        paths = self._expense_document_original_paths(document)
        if not paths:
            QMessageBox.information(self, "Оригинал", "У документа пока нет оригиналов.")
            return
        DocumentGalleryDialog(paths, document.get("supplier_name") or "Расход", self).exec()

    def setup_invoice_tab(self):
        layout = QVBoxLayout(self.tab_invoice)
        card = QFrame()
        card.setObjectName("card")
        card_layout = QFormLayout(card)
        card_layout.setSpacing(8)

        self.inv_date_from = create_date_field("2026-10-01")
        self.inv_date_to = create_date_field("2026-10-31")
        
        self.inv_comp_cb = QComboBox()
        self.load_companies_into_combobox(self.inv_comp_cb)
        
        self.inv_obj_cb = QComboBox()
        self.load_invoice_objects(self.inv_comp_cb.currentText())
        self.inv_comp_cb.currentTextChanged.connect(self.load_invoice_objects)

        card_layout.addRow("Период с:", self.inv_date_from)
        card_layout.addRow("Период по:", self.inv_date_to)
        card_layout.addRow("Фирма:", self.inv_comp_cb)
        card_layout.addRow("Объект:", self.inv_obj_cb)

        fast_pris_box = QHBoxLayout()
        self.fast_pris_check = QCheckBox("📌 Фиксированная цена за объект (Fast pris)")
        self.fast_pris_check.setStyleSheet("color: #63b3ed; font-weight: bold;")
        self.fast_pris_input = QLineEdit()
        self.fast_pris_input.setPlaceholderText("Сумма без НДС (SEK)")
        self.fast_pris_input.setEnabled(False)
        self.fast_pris_check.toggled.connect(self.fast_pris_input.setEnabled)

        fast_pris_box.addWidget(self.fast_pris_check)
        fast_pris_box.addWidget(self.fast_pris_input)
        card_layout.addRow(fast_pris_box)

        btn_layout = QHBoxLayout()
        gen_btn = QPushButton("📊 Просмотр сводки и журнала")
        gen_btn.clicked.connect(self.generate_invoice)
        
        export_pdf_inv_btn = QPushButton("📄 Сформировать общий PDF (SV)")
        export_pdf_inv_btn.setStyleSheet("background-color: #2b6cb0; border-color: #4299e1;")
        export_pdf_inv_btn.clicked.connect(self.export_invoice_to_pdf)
        
        mark_invoiced_btn = QPushButton("✅ Закрыть период")
        mark_invoiced_btn.setStyleSheet("background-color: #276749; border-color: #38a169;")
        mark_invoiced_btn.clicked.connect(self.mark_period_as_invoiced)

        unmark_invoiced_btn = QPushButton("❌ Вернуть в работу")
        unmark_invoiced_btn.setObjectName("danger")
        unmark_invoiced_btn.clicked.connect(self.unmark_period_as_invoiced)

        btn_layout.addWidget(gen_btn)
        btn_layout.addWidget(export_pdf_inv_btn)
        btn_layout.addWidget(mark_invoiced_btn)
        btn_layout.addWidget(unmark_invoiced_btn)
        card_layout.addRow(btn_layout)

        layout.addWidget(card)
        layout.addWidget(QLabel(
            "<b>Сводка отработанных часов за период (OB входит в общие часы):</b>"
        ))
        
        self.table_invoice = QTableWidget()
        self.table_invoice.setColumnCount(6)
        self.table_invoice.setHorizontalHeaderLabels([
            "Сотрудник", "Всего часов", "Из них OB1", "Из них OB2", "Другие OB", "Статус",
        ])
        invoice_header = self.table_invoice.horizontalHeader()
        invoice_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in {
            0: 180, 1: 100, 2: 100, 3: 100, 5: 120,
        }.items():
            self.table_invoice.setColumnWidth(column, width)
        invoice_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table_invoice.setWordWrap(True)
        self.table_invoice.setAlternatingRowColors(True)
        self.table_invoice.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table_invoice)

        self.invoice_summary_table = QTableWidget(8, 2)
        self.invoice_summary_table.setHorizontalHeaderLabels(["Sammanfattning", "Värde"])
        self.invoice_summary_table.verticalHeader().setVisible(False)
        self.invoice_summary_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.invoice_summary_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.invoice_summary_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.invoice_summary_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        layout.addWidget(self.invoice_summary_table)
        self.invoice_summary_note = QLabel(
            "OB-timmar ingår i arbetade timmar och läggs inte till utöver dessa."
        )
        self.invoice_summary_note.setWordWrap(True)
        layout.addWidget(self.invoice_summary_note)
        self.invoice_ob_rules_lbl = QLabel()
        self.invoice_ob_rules_lbl.setWordWrap(True)
        layout.addWidget(self.invoice_ob_rules_lbl)

    def load_invoice_objects(self, company):
        selected_id = self.inv_obj_cb.currentData()
        blocked = self.inv_obj_cb.blockSignals(True)
        self.inv_obj_cb.clear()
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT id, name, markning, sync_id FROM objects WHERE company = ? ORDER BY name, id",
            (company,),
        ).fetchall()
        conn.close()
        for row in rows:
            display = f"{row['markning']} | {row['name']}" if row['markning'] else row['name']
            self.inv_obj_cb.addItem(display, int(row['id']))
            self.inv_obj_cb.setItemData(self.inv_obj_cb.count() - 1, row['sync_id'], Qt.ItemDataRole.UserRole + 1)
        if selected_id is not None:
            self.inv_obj_cb.setCurrentIndex(self.inv_obj_cb.findData(selected_id))
        else:
            self.inv_obj_cb.setCurrentIndex(-1)
        self.inv_obj_cb.blockSignals(blocked)

    def setup_reporting_pages(self):
        layout = self.tab_invoice.layout()
        summary = QWidget()
        summary_layout = QVBoxLayout(summary)
        while layout.count() > 1:
            item = layout.takeAt(1)
            summary_layout.addWidget(item.widget())
        self.report_pages = QTabWidget()
        self.report_pages.addTab(summary, "Сводка и стоимость")
        self.report_pages.addTab(self.tab_dagbok, "Журнал работ")
        layout.addWidget(self.report_pages)

    def collect_invoice_report(self):
        d_from = self.inv_date_from.text().strip()
        d_to = self.inv_date_to.text().strip()
        comp = self.inv_comp_cb.currentText()
        object_id = self.inv_obj_cb.currentData()
        if not comp or object_id is None:
            raise ValueError("Выберите фирму и объект. Если объект исчез из справочника, выберите его заново.")
        datetime.strptime(d_from, "%Y-%m-%d")
        datetime.strptime(d_to, "%Y-%m-%d")
        if d_from > d_to:
            raise ValueError("Начало периода должно быть не позже окончания.")
        conn = get_db_connection()
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            cursor = conn.cursor()
            obj_row = cursor.execute(
                "SELECT * FROM objects WHERE id = ? AND company = ?", (object_id, comp)
            ).fetchone()
            if obj_row is None or obj_row['sync_id'] != self.inv_obj_cb.currentData(Qt.ItemDataRole.UserRole + 1):
                raise ValueError("Выбранный объект изменился или больше не существует. Обновите справочник.")
            obj = obj_row['name']
            object_names = invoice_object_names(cursor, obj_row)
            object_placeholders = ','.join('?' for _name in object_names)
            has_ob_schema = invoice_ob_schema_present(cursor)
            journal_rows = [dict(row) for row in cursor.execute(f"""
                SELECT * FROM shifts
                WHERE company = ? AND object_name IN ({object_placeholders})
                    AND date >= ? AND date <= ?
                ORDER BY date, employee, id
            """, (comp, *object_names, d_from, d_to)).fetchall()]
            journal_segments = {
                row["sync_id"]: [dict(segment) for segment in get_ob_segments(cursor, row["sync_id"])]
                for row in journal_rows if has_ob_schema and row["entry_type"] == RECORD_TYPE_OB
            }
            payroll = collect_invoice_payroll(cursor, comp, object_names, d_from, d_to, has_ob_schema)
            ob_category_hours, ob_premium_hours = (
                _collect_invoice_ob_hours(cursor, comp, object_names, d_from, d_to)
                if has_ob_schema else ({}, {})
            )
            ob_rule_descriptions = describe_category_time_ranges(
                d_from,
                d_to,
                {
                    "weekday_early_evening": "OB1",
                    "weekday_late_evening_night": "OB2",
                },
            )
            ob_join = """
                LEFT JOIN (SELECT shift_id, SUM(amount) AS segment_amount,
                                  SUM(hours) AS segment_hours, COUNT(*) AS segment_count
                           FROM ob_segments GROUP BY shift_id) ob_segment
                ON ob_segment.shift_id = shifts.sync_id
            """ if has_ob_schema else ""
            ob_amount_sql = "COALESCE(ob_segment.segment_amount, 0)" if has_ob_schema else "0"
            missing_segments_sql = (
                "COALESCE(ob_segment.segment_count, 0) = 0 "
                "OR ABS(COALESCE(ob_segment.segment_hours, 0) - shifts.hours) > 0.000001"
                if has_ob_schema else "1"
            )
            rows = cursor.execute(f'''
                SELECT employee,
                    SUM(hours),
                    SUM(CASE WHEN entry_type IN ('work', 'ob') THEN transport ELSE 0 END),
                    MAX(COALESCE(invoiced, 0)),
                    SUM(CASE WHEN entry_type = 'ob' THEN hours ELSE 0 END),
                    SUM(CASE WHEN entry_type = 'ob' THEN {ob_amount_sql} ELSE 0 END),
                    MIN(COALESCE(invoiced, 0)),
                    SUM(CASE WHEN entry_type = 'ob' AND ({missing_segments_sql}) THEN 1 ELSE 0 END)
                FROM shifts {ob_join}
                WHERE company = ? AND object_name IN ({object_placeholders}) AND date >= ? AND date <= ?
                    AND entry_type IN ('work', 'ob')
                GROUP BY employee ORDER BY employee
            ''', (comp, *object_names, d_from, d_to)).fetchall()
            warnings = []
            for row in rows:
                employee = row[0]
                categories = ob_category_hours.get(employee, {})
                premium_hours = ob_premium_hours.get(employee, 0.0)
                if (
                    row[7]
                    or sum(categories.values()) > premium_hours + 0.000001
                    or premium_hours > row[1] + 0.000001
                ):
                    ob_category_hours[employee] = None
                    warnings.append(
                        f"{employee}: OB-timmarnas fördelning kan inte verifieras; "
                        "sparade segment saknas eller är inkonsekventa."
                    )
            rows = [tuple(row[:3]) + (2 if row[3] and not row[6] else row[3],) + tuple(row[4:6]) for row in rows]
            is_fast_pris = self.fast_pris_check.isChecked()
            fast_pris_amount = parse_float(self.fast_pris_input.text(), 0.0) if is_fast_pris else 0.0
            if is_fast_pris and (not math.isfinite(fast_pris_amount) or fast_pris_amount <= 0):
                raise ValueError("Укажите положительную фиксированную цену; стоимость не рассчитана.")
            return {
                'object_id': object_id, 'sync_id': obj_row['sync_id'], 'company': comp, 'object': obj,
                'object_names': object_names,
                'date_from': d_from, 'date_to': d_to, 'rows': rows,
                'ob_category_hours': ob_category_hours,
                'ob_premium_hours': ob_premium_hours,
                'ob_rule_descriptions': ob_rule_descriptions,
                'rate': obj_row['rate'], 'transport_rate': obj_row['transport_rate'],
                'is_fast_pris': is_fast_pris,
                'fast_pris_amount': fast_pris_amount,
                'warnings': warnings,
                'payroll': payroll,
                'journal_rows': journal_rows,
                'journal_segments': journal_segments,
            }
        finally:
            conn.close()

    def _prepare_invoice_view(self, report):
        prepared_rows = []
        totals = {
            "hours": 0.0, "ob_categories": {},
            "work_amount": 0.0, "transport_amount": 0.0, "ob_amount": 0.0,
            "without_ob_hours": 0.0, "invoice_total": 0.0,
        }
        warnings = report.setdefault("warnings", [])

        def positive_rate(value):
            if value is None:
                return None
            value = float(value)
            return value if math.isfinite(value) and value > 0 else None

        def amount(hours, rate):
            if not hours:
                return 0.0
            if rate is None:
                return None
            return float((Decimal(str(hours)) * Decimal(str(rate))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ))

        for row in report["rows"]:
            employee = row[0]
            hours = float(row[1] or 0.0)
            trips = float(row[2] or 0.0)
            status_code = row[3]
            categories = report["ob_category_hours"].get(employee, {})
            premium_hours = report["ob_premium_hours"].get(employee, 0.0)
            without_ob_hours = hours - premium_hours if categories is not None else None
            ob_amount = None if categories is None or premium_hours > 0 else 0.0
            if ob_amount is None:
                warnings.append(
                    "Avtalsregel för OB-tillägg i kundfakturan saknas. Sparade lönebelopp "
                    "och löneprocent är inte ett avtalat fakturapåslag. "
                    + ("OB-tillägget kan inte fastställas separat." if report["is_fast_pris"]
                       else "OB och slutbelopp kan inte fastställas.")
                )
            other_categories = {
                category: category_hours for category, category_hours in (categories or {}).items()
                if category not in {"weekday_early_evening", "weekday_late_evening_night"}
            }
            rate = 0.0 if report["is_fast_pris"] else positive_rate(report["rate"])
            work_amount = 0.0 if report["is_fast_pris"] else amount(hours, rate)
            transport_amount = amount(trips, positive_rate(report["transport_rate"]))
            if work_amount is None:
                warnings.append("Objektets timpris är inte fastställt; arbetskostnaden kan inte beräknas.")
            if transport_amount is None:
                warnings.append("Resepriset är inte fastställt; resekostnaden kan inte beräknas.")
            row_total = (
                work_amount + transport_amount + ob_amount
                if all(value is not None for value in (work_amount, transport_amount, ob_amount))
                else None
            )
            status = (
                "Частично закрыт" if status_code == 2
                else "✅ Выставлен" if status_code == 1
                else "⏳ В работе"
            )
            prepared_rows.append({
                "employee": employee, "hours": hours, "rate": rate,
                "work_amount": work_amount, "transport_amount": transport_amount,
                "row_total": row_total, "status": status,
                "without_ob_hours": without_ob_hours,
                "ob1_hours": float(categories.get("weekday_early_evening", 0.0)) if categories is not None else None,
                "ob2_hours": float(categories.get("weekday_late_evening_night", 0.0)) if categories is not None else None,
                "other_ob_categories": other_categories,
                "ob_amount": ob_amount,
            })
            totals["hours"] += hours
            for key, value in (
                ("work_amount", work_amount), ("transport_amount", transport_amount),
                ("ob_amount", ob_amount), ("without_ob_hours", without_ob_hours),
            ):
                totals[key] = totals[key] + value if totals[key] is not None and value is not None else None
            for category, category_hours in (categories or {}).items():
                totals["ob_categories"][category] = (
                    totals["ob_categories"].get(category, 0.0) + category_hours
                )

        if report["is_fast_pris"]:
            totals["work_amount"] = report["fast_pris_amount"]
            totals["transport_amount"] = 0.0
            totals["invoice_total"] = report["fast_pris_amount"]
        else:
            components = (totals["work_amount"], totals["transport_amount"], totals["ob_amount"])
            totals["invoice_total"] = sum(components) if all(value is not None for value in components) else None
        totals["known_subtotal"] = (
            totals["work_amount"] + totals["transport_amount"]
            if totals["work_amount"] is not None and totals["transport_amount"] is not None else None
        )
        report["warnings"] = list(dict.fromkeys(warnings))
        report["prepared_rows"] = prepared_rows
        report["totals"] = totals
        if "journal_rows" in report:
            prepare_customer_costs(report)
            for row in prepared_rows:
                employee_shifts = [
                    shift for shift in report["journal_rows"]
                    if shift["employee"] == row["employee"] and shift["entry_type"] in ("work", "ob")
                ]
                rates = [
                    positive_rate(shift["rate"] if shift["entry_type"] == "work"
                                  else shift.get("work_rate_cache")) or positive_rate(report["rate"])
                    for shift in employee_shifts
                ]
                amounts = [money(shift["hours"], rate) for shift, rate in zip(employee_shifts, rates)]
                row["rate"] = rates[0] if rates and all(rate == rates[0] for rate in rates) else None
                row["work_amount"] = (
                    0.0 if report["is_fast_pris"] else
                    sum(amounts) if all(value is not None for value in amounts) else None
                )
                components = (row["work_amount"], row["transport_amount"], row["ob_amount"])
                row["row_total"] = sum(components) if all(value is not None for value in components) else None
        return report

    @classmethod
    def _invoice_summary_items(cls, report):
        totals = report["totals"]
        rule_descriptions = report.get("ob_rule_descriptions", {})
        split_known = totals["without_ob_hours"] is not None
        ob1_hours = totals["ob_categories"].get("weekday_early_evening", 0.0) if split_known else None
        ob2_hours = totals["ob_categories"].get("weekday_late_evening_night", 0.0) if split_known else None
        items = [
            ("Fakturaunderlag — timmar", ""),
            ("Arbetade timmar totalt", cls._invoice_value(totals["hours"], ".1f", " tim")),
            ("Varav utan OB", cls._invoice_value(totals["without_ob_hours"], ".1f", " tim")),
            (
                f"Varav {cls._invoice_ob_rule_short_caption('OB1', rule_descriptions.get('OB1', ''))}",
                cls._invoice_value(ob1_hours, ".1f", " tim"),
            ),
            (
                f"Varav {cls._invoice_ob_rule_short_caption('OB2', rule_descriptions.get('OB2', ''))}",
                cls._invoice_value(ob2_hours, ".1f", " tim"),
            ),
        ]
        for category in sorted(totals["ob_categories"]):
            if category in {"weekday_early_evening", "weekday_late_evening_night"}:
                continue
            label = INVOICE_OB_CATEGORY_LABELS.get(category, category)
            items.append((f"Varav {label}", cls._invoice_value(totals["ob_categories"][category], ".1f", " tim")))
        items.append(("Kundfakturering", ""))
        for line in report.get("cost_lines", []):
            items.append((
                f"{line['label']} — {cls._invoice_value(line['quantity'], '.1f')} {line['unit']}"
                f" × {cls._invoice_value(line['rate'])} SEK",
                cls._invoice_value(line["amount"], ",.2f", " SEK"),
            ))
        items.append((
            "Preliminärt totalbelopp" if totals["invoice_total"] is None else "Totalt belopp",
            cls._invoice_value(totals["known_subtotal"], ",.2f", " SEK"),
        ))
        return items

    @staticmethod
    def _invoice_value(value, spec=".2f", suffix=""):
        if value is not None and spec == ".1f":
            return format_quantity(value, ".") + suffix
        return "Ej fastställbart" if value is None else f"{value:{spec}}{suffix}"

    @staticmethod
    def _invoice_summary_note(report):
        note = "OB-timmar ingår i arbetade timmar och läggs inte till."
        if report["totals"]["invoice_total"] is None:
            note += (
                " PRELIMINÄRT: Endast kända belopp ingår, utan ej fastställda priser "
                "eller OB-tillägg. Slutbeloppet kan inte fastställas."
            )
        if report["is_fast_pris"]:
            note += " Fast pris inkluderar resa och OB."
        return note

    def _render_invoice_summary(self, report):
        items = self._invoice_summary_items(report)
        self.invoice_summary_table.setRowCount(len(items))
        for row_index, (label, value) in enumerate(items):
            self.invoice_summary_table.setItem(row_index, 0, QTableWidgetItem(label))
            self.invoice_summary_table.setItem(row_index, 1, QTableWidgetItem(value))
            if label in ("Arbetade timmar totalt", "Totalt belopp", "Preliminärt totalbelopp") or label.startswith("Varav "):
                for column in range(2):
                    item = self.invoice_summary_table.item(row_index, column)
                    strongest = label in ("Totalt belopp", "Preliminärt totalbelopp")
                    item.setBackground(QColor("#c8ded4" if strongest else "#dbe7ef"))
                    item.setForeground(QColor("#152a38"))
                    font = item.font()
                    font.setBold(True)
                    font.setPointSize(12 if strongest else 11)
                    item.setFont(font)
        self.invoice_summary_note.setText(self._invoice_summary_note(report))
        rule_descriptions = report.get("ob_rule_descriptions", {})
        self.invoice_ob_rules_lbl.setText(
            f"{self._invoice_ob_rule_caption('OB1', rule_descriptions.get('OB1', ''))}; "
            f"{self._invoice_ob_rule_caption('OB2', rule_descriptions.get('OB2', ''))}"
        )

    @staticmethod
    def _invoice_ob_rule_short_caption(label, description):
        if not description:
            return f"{label} (kan inte fastställas)"
        if description.startswith("Ej tillämpligt"):
            return f"{label} (ej tillämpligt)"
        intervals = list(dict.fromkeys(
            part.rsplit(" ", 1)[-1]
            for part in description.split("; ")
            if "–" in part.rsplit(" ", 1)[-1]
        ))
        if not intervals:
            return f"{label} (kan inte fastställas)"
        return f"{label} ({', '.join(intervals)})"

    @staticmethod
    def _invoice_ob_rule_caption(label, description):
        caption = SmartReportApp._invoice_ob_rule_short_caption(label, description)
        if not description or description.startswith("Ej tillämpligt"):
            return caption
        return f"{caption} — {description}"

    def generate_invoice(self):
        try:
            report = self.collect_invoice_report()
        except ValueError as error:
            self.table_invoice.setRowCount(0)
            if hasattr(self, "table_dagbok"):
                self._render_dagbok_report({"journal_rows": [], "journal_segments": {}})
            empty_report = {
                "is_fast_pris": False,
                "totals": {
                    "hours": 0.0, "ob_categories": {},
                    "work_amount": 0.0, "transport_amount": 0.0,
                    "ob_amount": None, "invoice_total": None,
                    "without_ob_hours": None, "known_subtotal": None,
                },
                "ob_rule_descriptions": {},
                "warnings": [str(error)],
            }
            self._render_invoice_summary(empty_report)
            self.invoice_summary_note.setText(str(error))
            self._invoice_report = None
            QMessageBox.warning(self, "Счёт для бухгалтера", str(error))
            return None
        report = self._prepare_invoice_view(report)
        self.table_invoice.setRowCount(len(report["prepared_rows"]))
        for row_idx, row in enumerate(report["prepared_rows"]):
            other_ob = ", ".join(
                f"{INVOICE_OB_CATEGORY_LABELS.get(category, category)} {hours:g}"
                for category, hours in sorted(row["other_ob_categories"].items())
            )
            row_values = [
                row["employee"], self._invoice_value(row["hours"], ".1f"),
                self._invoice_value(row["ob1_hours"], ".1f"),
                self._invoice_value(row["ob2_hours"], ".1f"), other_ob, row["status"],
            ]
            for col_idx, val in enumerate(row_values):
                it = QTableWidgetItem(str(val))
                if row["status"] == "✅ Выставлен":
                    it.setBackground(QColor("#1c4532"))
                self.table_invoice.setItem(row_idx, col_idx, it)
        self._render_invoice_summary(report)
        self._invoice_report = report
        if hasattr(self, "table_dagbok"):
            self._render_dagbok_report(report)
        return report

    def export_invoice_to_pdf(self):
        self._export_customer_report_pdf()

    def _export_customer_report_pdf(self, *, journal_only=False):
        report = self.generate_invoice()
        if report is None:
            return
        if not report["journal_rows"] and not report["is_fast_pris"]:
            QMessageBox.warning(
                self, "Отчёты и фактурирование",
                f"{report['company']} / {report['object']}: за выбранный период нет записей. "
                "Закрытые и незакрытые записи включены.",
            )
            return
        try:
            translations = self._collect_journal_translations(report)
            if translations is None:
                self.statusBar().showMessage("Экспорт PDF отменён. Исходные описания не изменены.", 8000)
                return
            report = {**report, "journal_translations": translations}
            filename = (
                ("Arbetsjournal" if journal_only else "Fakturaunderlag")
                + f"_{report['company']}_{report['object']}_{report['date_from']}_till_{report['date_to']}.pdf"
            )
            filename = "".join("_" if character in '<>:"/\\|?*' else character for character in filename)
            os.makedirs(REPORTS_DIR, exist_ok=True)
            filepath = os.path.join(REPORTS_DIR, filename)
            write_customer_pdf(
                report, filepath, DEFAULT_FONT, DEFAULT_FONT_BOLD,
                self._journal_description_swedish, journal_only=journal_only,
            )
            QMessageBox.information(self, "Отчёты и фактурирование", f"PDF sparad:\n{filepath}")
            if sys.platform == "win32":
                os.startfile(filepath)
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(filepath)))
        except Exception as error:
            logging.exception("Customer report PDF export failed")
            QMessageBox.critical(self, "Ошибка PDF", f"Не удалось создать PDF: {error}")

    @staticmethod
    def _journal_description_swedish(text):
        import re
        if not re.search(r"[\u0400-\u04ff]", text):
            return text
        translated = translate_to_swedish(text, allow_network=False)
        if re.search(r"[\u0400-\u04ff]", translated):
            raise ValueError(
                "Arbetsbeskrivningen kunde inte översättas fullständigt till svenska. "
                "Originaltexten är oförändrad; granska beskrivningen före export."
            )
        return translated

    def _collect_journal_translations(self, report):
        translations = {}
        pending = []
        for row in report["journal_rows"]:
            text = row["comment"] or ""
            try:
                translations[row["sync_id"]] = self._journal_description_swedish(text)
            except ValueError as error:
                pending.append((row, translate_to_swedish(text, allow_network=False), str(error)))
        if pending:
            dialog = JournalTranslationDialog(pending, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            translations.update(dialog.translations)
        return translations

    def mark_period_as_invoiced(self):
        try:
            report = self.collect_invoice_report()
        except ValueError as error:
            QMessageBox.warning(self, "Счёт для бухгалтера", str(error))
            return
        d_from, d_to = report['date_from'], report['date_to']
        comp, obj = report['company'], report['object']

        reply = QMessageBox.question(
            self, "Подтверждение",
            f"Отметить все смены по объекту <b>{obj}</b> за период <b>{d_from} — {d_to}</b> как выставленные в счет?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            if (self.inv_obj_cb.currentData() != report['object_id']
                    or self.inv_comp_cb.currentText() != comp
                    or self.inv_date_from.text() != d_from or self.inv_date_to.text() != d_to):
                QMessageBox.warning(self, "Параметры изменились", "Сформируйте отчёт заново; период не закрыт.")
                return
            conn = get_db_connection()
            cursor = conn.cursor()
            placeholders = ','.join('?' for _name in report['object_names'])
            work_filter = f"company = ? AND object_name IN ({placeholders}) AND date >= ? AND date <= ? AND entry_type = 'work'"
            parameters = (comp, *report['object_names'], d_from, d_to)
            cursor.execute(f"UPDATE shifts SET invoiced = 1 WHERE {work_filter}", parameters)
            enqueue_matching_shifts(cursor, work_filter, parameters)
            conn.commit()
            conn.close()

            QMessageBox.information(self, "Успех", "Период успешно закрыт!")
            self.generate_invoice()
            self.load_shifts_history()
            self.background_sync_with_cloud()

    def unmark_period_as_invoiced(self):
        try:
            report = self.collect_invoice_report()
        except ValueError as error:
            QMessageBox.warning(self, "Счёт для бухгалтера", str(error))
            return
        d_from, d_to = report['date_from'], report['date_to']
        comp, obj = report['company'], report['object']

        reply = QMessageBox.question(
            self, "Подтверждение",
            f"Вернуть в работу все смены по объекту <b>{obj}</b> за период <b>{d_from} — {d_to}</b>?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            if (self.inv_obj_cb.currentData() != report['object_id']
                    or self.inv_comp_cb.currentText() != comp
                    or self.inv_date_from.text() != d_from or self.inv_date_to.text() != d_to):
                QMessageBox.warning(self, "Параметры изменились", "Сформируйте отчёт заново; статус периода не изменён.")
                return
            conn = get_db_connection()
            cursor = conn.cursor()
            placeholders = ','.join('?' for _name in report['object_names'])
            work_filter = f"company = ? AND object_name IN ({placeholders}) AND date >= ? AND date <= ? AND entry_type = 'work'"
            parameters = (comp, *report['object_names'], d_from, d_to)
            cursor.execute(f"UPDATE shifts SET invoiced = 0 WHERE {work_filter}", parameters)
            enqueue_matching_shifts(cursor, work_filter, parameters)
            conn.commit()
            conn.close()

            QMessageBox.information(self, "Успех", "Статус сброшен!")
            self.generate_invoice()
            self.load_shifts_history()
            self.background_sync_with_cloud()

    def load_objects_for_company(self, comp_name, obj_cb, record_type=None, archived_object_name=None):
        selected_name = obj_cb.currentData() or archived_object_name
        obj_cb.clear()
        if not comp_name:
            return False
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT name, COALESCE(markning, ''), COALESCE(rate, 0.0),
                   COALESCE(transport_rate, 0.0), is_active
            FROM objects
            WHERE company = ? AND (is_active = 1 OR (? = 1 AND name = ?))
            ORDER BY name
        """, (comp_name, int(archived_object_name is not None), archived_object_name or ""))
        rows = cursor.fetchall()
        conn.close()
        if record_type in SPECIAL_OBJECTS:
            reference = SPECIAL_OBJECTS[record_type]
            rows = [
                row for row in rows
                if matches_special_object(row[0], row[1], reference)
            ]
            if len(rows) != 1:
                return False
        elif record_type == RECORD_TYPE_WORK:
            rows = [
                row for row in rows
                if not any(
                    matches_special_object(row[0], row[1], reference)
                    for reference in SPECIAL_OBJECTS.values()
                )
            ]
        for row in rows:
            name, mark, rate, tr_rate, is_active = row[0], row[1], row[2], row[3], bool(row[4])
            display = f"{mark} | {name} ({rate} kr/ч, тр: {tr_rate})" if mark else f"{name} ({rate} kr/ч)"
            if not is_active:
                display += " — В архиве"
            obj_cb.addItem(display, userData=name)
        if selected_name:
            selected_index = obj_cb.findData(selected_name)
            if selected_index >= 0:
                obj_cb.setCurrentIndex(selected_index)
        return bool(rows)

    def setup_dagbok_tab(self):
        layout = QVBoxLayout(self.tab_dagbok)
        journal_pdf = QPushButton("📖 PDF только журнала (SV)")
        journal_pdf.clicked.connect(self.export_dagbok_to_pdf)
        layout.addWidget(journal_pdf)
        layout.addWidget(QLabel("<b>Журнал выполненных работ (Dagbok):</b>"))
        
        self.table_dagbok = QTableWidget()
        self.table_dagbok.setColumnCount(6)
        self.table_dagbok.setHorizontalHeaderLabels(["Дата", "Сотрудник", "Тип", "Часы", "Детализация / OB", "Описание"])
        
        header = self.table_dagbok.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        
        self.table_dagbok.setColumnWidth(0, 100)
        self.table_dagbok.setColumnWidth(1, 150)
        self.table_dagbok.setColumnWidth(2, 75)
        self.table_dagbok.setColumnWidth(3, 70)
        self.table_dagbok.setColumnWidth(4, 185)
        self.table_dagbok.setAlternatingRowColors(True)
        layout.addWidget(self.table_dagbok)

        dag_summary_box = QHBoxLayout()
        self.dag_total_hours_lbl = QLabel("<b>Работа: 0.0 ч</b>")
        self.dag_total_atf_lbl = QLabel("<b>АТФ: 0.0 ч</b>")
        self.dag_total_sick_lbl = QLabel("<b>Больничный: 0.0 ч</b>")
        self.dag_total_vacation_lbl = QLabel("<b>Отпуск: 0.0 ч</b>")
        self.dag_total_holiday_lbl = QLabel("<b>Красные дни: 0.0 ч</b>")
        self.dag_total_ob_lbl = QLabel("<b>В том числе записи OB: 0.0 ч</b>")
        dag_summary_box.addWidget(self.dag_total_hours_lbl)
        dag_summary_box.addWidget(self.dag_total_atf_lbl)
        dag_summary_box.addWidget(self.dag_total_sick_lbl)
        dag_summary_box.addWidget(self.dag_total_vacation_lbl)
        dag_summary_box.addWidget(self.dag_total_holiday_lbl)
        dag_summary_box.addWidget(self.dag_total_ob_lbl)
        dag_summary_box.addStretch()
        layout.addLayout(dag_summary_box)

    def generate_dagbok(self):
        return self.generate_invoice()

    def _render_dagbok_report(self, report):
        rows = report['journal_rows']
        ob_segments_by_id = report['journal_segments']
        self.table_dagbok.setRowCount(len(rows))
        totals = {
            RECORD_TYPE_WORK: 0.0, RECORD_TYPE_ATF: 0.0, RECORD_TYPE_SICK: 0.0,
            RECORD_TYPE_VACATION: 0.0, RECORD_TYPE_HOLIDAY: 0.0, RECORD_TYPE_OB: 0.0,
        }

        for row_idx, row in enumerate(rows):
            date = row["date"]
            emp = row["employee"]
            record_type = row["entry_type"]
            hrs = float(row["hours"] or 0.0)
            comment = row["comment"] or ""
            if record_type in totals:
                totals[record_type] += hrs
            
            detalization = ""
            detail_tooltip = ""
            if record_type == RECORD_TYPE_OB:
                segments = ob_segments_by_id.get(row["sync_id"], [])
                start_t = row["start_time"] if "start_time" in row.keys() else None
                end_t = row["end_time"] if "end_time" in row.keys() else None
                detalization = format_ob_detail(start_t, end_t, hrs, segments)
                if not detalization and segments:
                    detalization = f"{segments[0]['start_time']}–{segments[-1]['end_time']} ({format_quantity(hrs, '.')} ч)"
                detail_tooltip = "; ".join(
                    f"{segment['segment_date']} {segment['start_time']}–{segment['end_time']} "
                    f"{segment['hours']:.2f} ч, +{segment['premium_percent']:.0f}%, "
                    f"×{segment['multiplier']:.2f}, {segment['amount']:.2f} kr"
                    for segment in segments
                )
            elif record_type == RECORD_TYPE_ATF:
                detalization = "АТФ (p1000)"
            elif record_type == RECORD_TYPE_SICK:
                detalization = "Больничный (p1001)"
            elif record_type == RECORD_TYPE_VACATION:
                detalization = "Отпуск / Semestertimmar (p1002)"
            elif record_type == RECORD_TYPE_HOLIDAY:
                detalization = "Красные дни / Röda dagar (p1004)"
            
            it_date = QTableWidgetItem(str(date))
            it_date.setTextAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
            it_emp = QTableWidgetItem(str(emp))
            it_type = QTableWidgetItem(record_type_short_label(record_type))
            it_type.setTextAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
            it_hrs = QTableWidgetItem(format_quantity(hrs, ".") + " ч")
            it_hrs.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            it_det = QTableWidgetItem(str(detalization))
            if detail_tooltip:
                it_det.setToolTip(detail_tooltip)
            it_comm = QTableWidgetItem(str(comment))
            it_comm.setToolTip(str(comment))

            self.table_dagbok.setItem(row_idx, 0, it_date)
            self.table_dagbok.setItem(row_idx, 1, it_emp)
            self.table_dagbok.setItem(row_idx, 2, it_type)
            self.table_dagbok.setItem(row_idx, 3, it_hrs)
            self.table_dagbok.setItem(row_idx, 4, it_det)
            self.table_dagbok.setItem(row_idx, 5, it_comm)

        worked_hours = totals[RECORD_TYPE_WORK] + totals[RECORD_TYPE_OB]
        self.dag_total_hours_lbl.setText(f"<b>Работа: {format_quantity(worked_hours, '.')} ч</b>")
        self.dag_total_atf_lbl.setText(f"<b>АТФ: {totals[RECORD_TYPE_ATF]:.1f} ч</b>")
        self.dag_total_sick_lbl.setText(f"<b>Больничный: {totals[RECORD_TYPE_SICK]:.1f} ч</b>")
        self.dag_total_vacation_lbl.setText(f"<b>Отпуск: {totals[RECORD_TYPE_VACATION]:.1f} ч</b>")
        self.dag_total_holiday_lbl.setText(f"<b>Красные дни: {totals[RECORD_TYPE_HOLIDAY]:.1f} ч</b>")
        self.dag_total_ob_lbl.setText(f"<b>В том числе записи OB: {totals[RECORD_TYPE_OB]:.2f} ч</b>")

    def export_dagbok_to_pdf(self):
        self._export_customer_report_pdf(journal_only=True)

    def setup_sprav_tab(self):
        layout = QVBoxLayout(self.tab_sprav)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)

        top_layout = QHBoxLayout()
        top_layout.setSpacing(12)

        # Секция 1: Фирмы
        comp_card = QFrame()
        comp_card.setObjectName("card")
        comp_card_layout = QVBoxLayout(comp_card)
        comp_card_layout.setSpacing(6)
        comp_card_layout.addWidget(QLabel("<b>🏢 Фирмы (Заказчики):</b>"))
        
        comp_add_layout = QHBoxLayout()
        self.new_comp_input = QLineEdit()
        self.new_comp_input.setPlaceholderText("Название фирмы...")
        add_comp_btn = QPushButton("➕ Добавить")
        add_comp_btn.clicked.connect(self.add_company)
        comp_add_layout.addWidget(self.new_comp_input, stretch=3)
        comp_add_layout.addWidget(add_comp_btn, stretch=1)
        comp_card_layout.addLayout(comp_add_layout)

        self.table_companies = QTableWidget()
        self.table_companies.setColumnCount(1)
        self.table_companies.setHorizontalHeaderLabels(["Фирма"])
        self.table_companies.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        comp_card_layout.addWidget(self.table_companies, stretch=1)

        del_comp_btn = QPushButton("🗑 Удалить фирму")
        del_comp_btn.setObjectName("danger")
        del_comp_btn.clicked.connect(self.delete_company)
        comp_card_layout.addWidget(del_comp_btn)
        top_layout.addWidget(comp_card, stretch=1)

        # Секция 2: Сотрудники
        emp_card = QFrame()
        emp_card.setObjectName("card")
        emp_card_layout = QVBoxLayout(emp_card)
        emp_card_layout.setSpacing(6)
        emp_card_layout.addWidget(QLabel("<b>👥 Сотрудники:</b>"))
        
        emp_add_layout = QHBoxLayout()
        self.new_emp_input = QLineEdit()
        self.new_emp_input.setPlaceholderText("ФИО...")
        self.new_emp_rate_input = QLineEdit()
        self.new_emp_rate_input.setPlaceholderText("kr/ч...")
        self.new_emp_rate_input.setFixedWidth(80)
        add_emp_btn = QPushButton("➕ Добавить")
        add_emp_btn.clicked.connect(self.add_or_update_employee)
        emp_add_layout.addWidget(self.new_emp_input, stretch=2)
        emp_add_layout.addWidget(self.new_emp_rate_input, stretch=1)
        emp_add_layout.addWidget(add_emp_btn)
        emp_card_layout.addLayout(emp_add_layout)

        employee_view_layout = QHBoxLayout()
        archive_filter_style = (
            "QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #718096; "
            "border-radius: 3px; background-color: #1A202C; } "
            "QCheckBox::indicator:checked { background-color: #3182CE; border-color: #90CDF4; }"
        )
        self.show_inactive_employees_check = QCheckBox("Показать неактивных")
        self.show_inactive_employees_check.setStyleSheet(archive_filter_style)
        self.show_inactive_employees_check.setChecked(False)
        self.show_inactive_employees_check.toggled.connect(self.load_employees_table)
        self.employee_counts_label = QLabel()
        employee_view_layout.addWidget(self.show_inactive_employees_check)
        employee_view_layout.addStretch()
        employee_view_layout.addWidget(self.employee_counts_label)
        emp_card_layout.addLayout(employee_view_layout)

        self.table_employees = QTableWidget()
        self.table_employees.setColumnCount(4)
        self.table_employees.setHorizontalHeaderLabels(["ФИО", "kr/ч", "Статус", "ПИН"])
        self.table_employees.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table_employees.itemSelectionChanged.connect(self.update_employee_status_action)
        self.table_employees.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        emp_card_layout.addWidget(self.table_employees, stretch=1)

        emp_btn_layout = QHBoxLayout()
        self.employee_status_button = QPushButton("Деактивировать сотрудника")
        self.employee_status_button.setObjectName("danger")
        self.employee_status_button.setEnabled(False)
        self.employee_status_button.clicked.connect(self.toggle_employee_active)
        
        set_pin_btn = QPushButton("🔑 Задать ПИН")
        set_pin_btn.clicked.connect(self.reset_employee_pin_desktop)

        emp_btn_layout.addWidget(self.employee_status_button)
        emp_btn_layout.addWidget(set_pin_btn)
        emp_card_layout.addLayout(emp_btn_layout)
        top_layout.addWidget(emp_card, stretch=1)

        layout.addLayout(top_layout, stretch=1)

        # Нижняя панель (Управление объектами со ставками и транспортом)
        obj_card = QFrame()
        obj_card.setObjectName("card")
        obj_card_layout = QVBoxLayout(obj_card)
        obj_card_layout.setSpacing(6)
        obj_card_layout.addWidget(QLabel("<b>🏗 Управление объектами (Ставки и транспорт):</b>"))

        object_view_layout = QHBoxLayout()
        self.show_archived_objects_check = QCheckBox("Показать архив")
        self.show_archived_objects_check.setStyleSheet(archive_filter_style)
        self.show_archived_objects_check.setChecked(False)
        self.show_archived_objects_check.toggled.connect(self.load_objects_table)
        self.object_counts_label = QLabel()
        object_view_layout.addWidget(self.show_archived_objects_check)
        object_view_layout.addStretch()
        object_view_layout.addWidget(self.object_counts_label)
        obj_card_layout.addLayout(object_view_layout)

        obj_add_layout = QHBoxLayout()
        self.new_obj_comp_cb = QComboBox()
        self.load_companies_into_combobox(self.new_obj_comp_cb)

        self.new_obj_name_input = QLineEdit()
        self.new_obj_name_input.setPlaceholderText("Адрес / Название объекта...")
        
        self.new_obj_mark_input = QLineEdit()
        self.new_obj_mark_input.setPlaceholderText("Markning")
        self.new_obj_mark_input.setFixedWidth(90)
        
        self.new_obj_rate_input = QLineEdit()
        self.new_obj_rate_input.setPlaceholderText("kr/ч")
        self.new_obj_rate_input.setFixedWidth(70)

        self.new_obj_trans_input = QLineEdit()
        self.new_obj_trans_input.setPlaceholderText("Транс.kr")
        self.new_obj_trans_input.setFixedWidth(75)

        add_obj_btn = QPushButton("➕ Добавить")
        add_obj_btn.clicked.connect(self.add_object)

        obj_add_layout.addWidget(self.new_obj_comp_cb, stretch=2)
        obj_add_layout.addWidget(self.new_obj_name_input, stretch=3)
        obj_add_layout.addWidget(self.new_obj_mark_input, stretch=1)
        obj_add_layout.addWidget(self.new_obj_rate_input, stretch=1)
        obj_add_layout.addWidget(self.new_obj_trans_input, stretch=1)
        obj_add_layout.addWidget(add_obj_btn)
        obj_card_layout.addLayout(obj_add_layout)
        
        self.table_objects = QTableWidget()
        self.table_objects.setColumnCount(6)
        self.table_objects.setHorizontalHeaderLabels(["Фирма", "Объект / Адрес", "Markning", "Факт. ставка (kr/ч)", "Транспорт (kr/выезд)", "Состояние"])
        
        obj_header = self.table_objects.horizontalHeader()
        obj_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        obj_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        obj_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        obj_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        obj_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        obj_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        
        self.table_objects.setColumnWidth(0, 150)
        self.table_objects.setColumnWidth(2, 110)
        self.table_objects.setColumnWidth(3, 140)
        self.table_objects.setColumnWidth(4, 150)
        self.table_objects.setColumnWidth(5, 120)
        self.table_objects.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        self.table_objects.cellChanged.connect(self.on_object_cell_changed)
        self.table_objects.itemSelectionChanged.connect(self.update_object_archive_action)
        obj_card_layout.addWidget(self.table_objects, stretch=1)

        self.object_archive_button = QPushButton("В архив")
        self.object_archive_button.setObjectName("danger")
        self.object_archive_button.setEnabled(False)
        self.object_archive_button.clicked.connect(self.toggle_object_archive)
        obj_card_layout.addWidget(self.object_archive_button)

        layout.addWidget(obj_card, stretch=1)

    def load_companies_into_combobox(self, cb):
        current_val = cb.currentText()
        signals_blocked = cb.blockSignals(True)
        cb.clear()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM companies ORDER BY name")
        rows = cursor.fetchall()
        conn.close()
        for row in rows:
            cb.addItem(row[0])
        if current_val:
            idx = cb.findText(current_val)
            cb.setCurrentIndex(idx)
        cb.blockSignals(signals_blocked)

    def add_company(self):
        comp_name = self.new_comp_input.text().strip()
        if not comp_name:
            return
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO companies (name) VALUES (?)", (comp_name,))
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        conn.close()
        self.new_comp_input.clear()
        self.load_dropdowns()
        self.background_sync_with_cloud()

    def load_companies_table(self):
        if hasattr(self, 'table_companies'):
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM companies ORDER BY name")
            rows = cursor.fetchall()
            conn.close()
            self.table_companies.setRowCount(len(rows))
            for row_idx, row in enumerate(rows):
                self.table_companies.setItem(row_idx, 0, QTableWidgetItem(row[0]))

    def delete_company(self):
        selected = self.table_companies.currentRow()
        if selected < 0:
            return
        comp_name = self.table_companies.item(selected, 0).text()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM companies WHERE name = ?', (comp_name,))
        conn.commit()
        conn.close()
        self.load_dropdowns()
        self.background_sync_with_cloud()

    def add_object(self):
        comp = self.new_obj_comp_cb.currentText().strip()
        name = self.new_obj_name_input.text().strip()
        mark = self.new_obj_mark_input.text().strip()
        rate_val = parse_float(self.new_obj_rate_input.text(), 0.0)
        trans_rate_val = parse_float(self.new_obj_trans_input.text(), 0.0)
        
        if not name or not comp:
            QMessageBox.warning(self, "Ошибка", "Выберите фирму и укажите название объекта!")
            return
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            existing = cursor.execute(
                "SELECT id, sync_id, is_active FROM objects WHERE name = ? AND company = ?",
                (name, comp),
            ).fetchone()
            if existing and not existing["sync_id"]:
                raise ValueError("Эта legacy-запись ещё не связана с сервером. Сначала выполните явное сопоставление.")
            if existing:
                cursor.execute(
                    "UPDATE objects SET markning = ?, rate = ?, transport_rate = ?, is_active = 1 WHERE id = ?",
                    (mark, rate_val, trans_rate_val, int(existing["id"])),
                )
                operation = "activate" if not existing["is_active"] else "upsert"
                enqueue_object_sync_event(cursor, int(existing["id"]), operation)
            else:
                cursor.execute("""
                    INSERT INTO objects (name, markning, company, rate, transport_rate, sync_id, is_active)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                """, (name, mark, comp, rate_val, trans_rate_val, str(uuid.uuid4())))
                enqueue_object_sync_event(cursor, int(cursor.lastrowid), "upsert")
            conn.commit()
        except Exception as e:
            conn.rollback()
            QMessageBox.critical(self, "Ошибка", f"Не удалось добавить объект: {e}")
            conn.close()
            return
        conn.close()
        self.new_obj_name_input.clear()
        self.new_obj_mark_input.clear()
        self.new_obj_rate_input.clear()
        self.new_obj_trans_input.clear()
        self.load_dropdowns()
        self.background_sync_with_cloud()

    def on_object_cell_changed(self, row, col):
        if col in [1, 2, 3, 4]:
            conn = None
            try:
                name_item = self.table_objects.item(row, 1)
                mark_item = self.table_objects.item(row, 2)
                if not name_item or not mark_item:
                    return
                rate_item = self.table_objects.item(row, 3)
                trans_item = self.table_objects.item(row, 4)
                if not rate_item or not trans_item:
                    return
                object_item = self.table_objects.item(row, 0)
                object_id = int(object_item.data(Qt.ItemDataRole.UserRole))
                name = name_item.text().strip()
                mark = mark_item.text().strip()
                if not name or len(name) > 200 or len(mark) > 50:
                    raise ValueError("Название должно содержать до 200 символов, markning — до 50.")
                new_rate = parse_float(rate_item.text(), 0.0)
                new_trans = parse_float(trans_item.text(), 0.0)
                if not 0 <= new_rate <= 100000 or not 0 <= new_trans <= 100000:
                    raise ValueError("Ставки должны быть в диапазоне от 0 до 100000.")

                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("BEGIN IMMEDIATE")
                object_state = cursor.execute(
                    "SELECT sync_id, is_active, sync_version FROM objects WHERE id = ?", (object_id,)
                ).fetchone()
                if object_state is None:
                    raise ValueError("Объект больше не существует. Обновите список.")
                if not object_state["sync_id"]:
                    raise ValueError("Объект ещё не связан с сервером; дождитесь синхронизации.")
                if not object_state["is_active"]:
                    raise ValueError("Объект находится в архиве. Сначала восстановите его.")
                expected_revision = object_item.data(Qt.ItemDataRole.UserRole + 1)
                expected_sync_id = object_item.data(Qt.ItemDataRole.UserRole + 2)
                if (expected_revision != object_state["sync_version"]
                        or expected_sync_id != object_state["sync_id"]):
                    raise ValueError(
                        f"Объект изменился после загрузки таблицы. Введённый markning «{mark}» "
                        "не применён; проверьте обновлённую запись и повторите изменение."
                    )
                column_name, new_value = {
                    1: ("name", name), 2: ("markning", mark),
                    3: ("rate", new_rate), 4: ("transport_rate", new_trans),
                }[col]
                if col == 1:
                    ensure_object_name_history(cursor)
                cursor.execute(
                    f"UPDATE objects SET {column_name} = ? "
                    "WHERE id = ? AND sync_id = ? AND sync_version = ? AND is_active = 1",
                    (new_value, object_id, expected_sync_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Состояние объекта изменилось; обновите список и повторите действие.")
                event = enqueue_object_sync_event(cursor, object_id, "upsert")
                conn.commit()
                object_item.setData(Qt.ItemDataRole.UserRole + 1, event["revision"])
                self._object_edit_sync_pending = True
            except Exception as error:
                if conn is not None:
                    conn.rollback()
                QMessageBox.warning(self, "Объект", f"Изменение не сохранено: {error}")
                QTimer.singleShot(0, self.load_objects_table)
                return
            finally:
                if conn is not None:
                    conn.close()
            self.background_sync_with_cloud()

    def delete_object(self):
        selected = self.table_objects.currentRow()
        if selected < 0:
            return
        object_item = self.table_objects.item(selected, 0)
        object_id = int(object_item.data(Qt.ItemDataRole.UserRole))
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            object_state = cursor.execute(
                "SELECT sync_id, is_active FROM objects WHERE id = ?", (object_id,)
            ).fetchone()
            if object_state and object_state["is_active"]:
                cursor.execute("UPDATE objects SET is_active = 0 WHERE id = ?", (object_id,))
                if object_state["sync_id"]:
                    enqueue_object_sync_event(cursor, object_id, "deactivate")
            conn.commit()
        except Exception as error:
            conn.rollback()
            QMessageBox.critical(self, "Ошибка", f"Не удалось архивировать объект: {error}")
            conn.close()
            return
        conn.close()
        self.load_dropdowns()
        self.background_sync_with_cloud()

    def link_legacy_object(self):
        selected = self.table_objects.currentRow()
        if selected < 0:
            return
        object_item = self.table_objects.item(selected, 0)
        object_id = int(object_item.data(Qt.ItemDataRole.UserRole))
        conn = get_db_connection()
        row = conn.execute(
            "SELECT name, markning, company, sync_id, rate, transport_rate, is_active, sync_version "
            "FROM objects WHERE id = ?",
            (object_id,),
        ).fetchone()
        conn.close()
        if row is None or row["sync_id"]:
            QMessageBox.information(self, "Сопоставление объекта", "Выберите legacy-запись без серверной связи.")
            return
        try:
            payload = request_expense_api("GET", "/api/expense-objects?include_inactive=1")
        except Exception as error:
            QMessageBox.warning(self, "Сопоставление объекта", f"Не удалось загрузить серверный справочник: {error}")
            return
        candidates = exact_legacy_object_candidates(
            payload.get("objects", []), row["company"], row["name"], row["markning"]
        )
        if len(candidates) != 1:
            QMessageBox.information(
                self,
                "Сопоставление объекта",
                "Нет единственной несвязанной серверной записи с точным совпадением фирмы, названия и markning. Изменения не внесены.",
            )
            return
        candidate = candidates[0]
        local_rate = float(row["rate"] or 0.0)
        local_transport = float(row["transport_rate"] or 0.0)
        server_rate = float(candidate.get("rate") or 0.0)
        server_transport = float(candidate.get("transport_rate") or 0.0)
        rates_compatible = (
            (local_rate == server_rate or (server_rate == 0 and local_rate > 0))
            and (local_transport == server_transport or (server_transport == 0 and local_transport > 0))
        )
        if (
            int(bool(row["is_active"])) != int(bool(candidate.get("is_active", 1)))
            or int(row["sync_version"] or 0) != int(candidate.get("sync_version") or 0)
            or int(candidate.get("sync_version") or 0) != 0
            or not rates_compatible
        ):
            QMessageBox.information(
                self,
                "Сопоставление объекта",
                "Состояния или ненулевые ставки различаются; запись оставлена без изменений.",
            )
            return
        confirmation = QMessageBox.question(
            self,
            "Подтвердить связь",
            f"Связать «{row['name']}» с серверным ID {candidate['id']}? "
            f"Локальные ставки {local_rate:g}/{local_transport:g} сохранятся; "
            "нулевые серверные ставки будут заменены локальными.",
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            current = cursor.execute(
                "SELECT sync_id FROM objects WHERE id = ?", (object_id,)
            ).fetchone()
            if current is None or current["sync_id"]:
                raise ValueError("Локальная запись уже была связана или удалена.")
            enqueue_object_sync_event(cursor, object_id, "link_legacy", {
                "sync_id": str(uuid.uuid4()),
                "server_object_id": int(candidate["id"]),
                "expected_server_name": candidate["name"],
                "expected_server_company": candidate["company"],
                "expected_server_markning": candidate.get("markning") or "",
                "expected_server_rate": server_rate,
                "expected_server_transport_rate": server_transport,
                "expected_server_is_active": int(bool(candidate.get("is_active", 1))),
                "expected_server_revision": int(candidate.get("sync_version") or 0),
            })
            conn.commit()
        except Exception as error:
            conn.rollback()
            QMessageBox.warning(self, "Сопоставление объекта", f"Связь не поставлена в очередь: {error}")
            conn.close()
            return
        conn.close()
        self.load_dropdowns()
        self.background_sync_with_cloud()

    def load_dropdowns(self, refresh_tables=True):
        conn = get_db_connection()
        cursor = conn.cursor()

        if hasattr(self, 'emp_cb'):
            self.emp_cb.clear()
            cursor.execute("SELECT name FROM employees WHERE is_active = 1 ORDER BY name")
            for row in cursor.fetchall():
                self.emp_cb.addItem(row[0])

        if hasattr(self, 'bal_emp_cb'):
            cursor.execute("SELECT name FROM employees ORDER BY name")
            balance_people = [row[0] for row in cursor.fetchall()]
            current_people = [
                self.bal_emp_cb.itemText(index) for index in range(self.bal_emp_cb.count())
            ]
            # Rebuilding an open popup resets its scroll and highlighted employee.
            if not self.bal_emp_cb.view().isVisible() and current_people != balance_people:
                selected_balance_person = self.bal_emp_cb.currentText()
                blocked = self.bal_emp_cb.blockSignals(True)
                try:
                    self.bal_emp_cb.clear()
                    self.bal_emp_cb.addItems(balance_people)
                    if selected_balance_person:
                        self.bal_emp_cb.setCurrentText(selected_balance_person)
                finally:
                    self.bal_emp_cb.blockSignals(blocked)

        if hasattr(self, 'filter_emp'):
            curr_emp = self.filter_emp.currentText()
            self.filter_emp.clear()
            self.filter_emp.addItem("-- Все сотрудники --")
            cursor.execute("SELECT name FROM employees ORDER BY name")
            for row in cursor.fetchall():
                self.filter_emp.addItem(row[0])
            idx = self.filter_emp.findText(curr_emp)
            if idx >= 0:
                self.filter_emp.setCurrentIndex(idx)

        if hasattr(self, 'filter_comp'):
            curr_comp = self.filter_comp.currentText()
            self.filter_comp.clear()
            self.filter_comp.addItem("-- Все фирмы --")
            cursor.execute("SELECT name FROM companies ORDER BY name")
            for row in cursor.fetchall():
                self.filter_comp.addItem(row[0])
            idx = self.filter_comp.findText(curr_comp)
            if idx >= 0:
                self.filter_comp.setCurrentIndex(idx)

        if hasattr(self, 'filter_obj'):
            curr_obj = self.filter_obj.currentText()
            self.filter_obj.clear()
            self.filter_obj.addItem("-- Все объекты --")
            cursor.execute("SELECT name FROM objects WHERE is_active = 1 ORDER BY name")
            for row in cursor.fetchall():
                self.filter_obj.addItem(row[0])
            idx = self.filter_obj.findText(curr_obj)
            if idx >= 0:
                self.filter_obj.setCurrentIndex(idx)

        for cb in [getattr(self, 'comp_cb', None), getattr(self, 'inv_comp_cb', None), getattr(self, 'dag_comp_cb', None), getattr(self, 'new_obj_comp_cb', None)]:
            if cb:
                self.load_companies_into_combobox(cb)

        if hasattr(self, 'obj_cb') and hasattr(self, 'comp_cb'):
            record_type = self.record_type_cb.currentData() if hasattr(self, 'record_type_cb') else RECORD_TYPE_WORK
            self.load_objects_for_company(self.comp_cb.currentText(), self.obj_cb, record_type)

        if hasattr(self, 'inv_obj_cb') and hasattr(self, 'inv_comp_cb'):
            self.load_invoice_objects(self.inv_comp_cb.currentText())

        if hasattr(self, 'dag_obj_cb') and hasattr(self, 'dag_comp_cb'):
            self.load_objects_for_company(self.dag_comp_cb.currentText(), self.dag_obj_cb)

        conn.close()

        if refresh_tables and hasattr(self, 'table_employees'):
            self.load_employees_table()
        if refresh_tables and hasattr(self, 'table_objects'):
            self.load_objects_table()
        if refresh_tables and hasattr(self, 'table_companies'):
            self.load_companies_table()

    def load_objects_table(self):
        if self.table_objects.state() == QTableWidget.State.EditingState:
            return
        selected_object_id = None
        current_row = self.table_objects.currentRow()
        if current_row >= 0:
            selected_item = self.table_objects.item(current_row, 0)
            if selected_item is not None:
                selected_object_id = selected_item.data(Qt.ItemDataRole.UserRole)
        try:
            self.table_objects.cellChanged.disconnect(self.on_object_cell_changed)
        except TypeError:
            pass

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT is_active, COUNT(*) FROM objects GROUP BY is_active")
        counts = {int(row[0]): int(row[1]) for row in cursor.fetchall()}
        self.object_counts_label.setText(
            f"Активных: {counts.get(1, 0)} | В архиве: {counts.get(0, 0)}"
        )
        cursor.execute("""
            SELECT id, company, name, markning, COALESCE(rate, 0.0),
                   COALESCE(transport_rate, 0.0), is_active, sync_id, server_id, sync_version
            FROM objects
            WHERE is_active = 1 OR ? = 1
            ORDER BY company, name, id
        """, (int(self.show_archived_objects_check.isChecked()),))
        rows = cursor.fetchall()
        conn.close()

        self.table_objects.clearSelection()
        self.table_objects.setCurrentCell(-1, -1)
        self.table_objects.setRowCount(len(rows))
        for row_idx, row in enumerate(rows):
            company_item = QTableWidgetItem(row[1] or "")
            company_item.setData(Qt.ItemDataRole.UserRole, int(row[0]))
            company_item.setData(Qt.ItemDataRole.UserRole + 1, int(row[9] or 0))
            company_item.setData(Qt.ItemDataRole.UserRole + 2, row[7])
            self.table_objects.setItem(row_idx, 0, company_item)
            name_item = QTableWidgetItem(row[2])
            mark_item = QTableWidgetItem(row[3] or "")
            company_item.setFlags(company_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if not row[6] or not row[7]:
                for item in (name_item, mark_item):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table_objects.setItem(row_idx, 1, name_item)
            self.table_objects.setItem(row_idx, 2, mark_item)
            
            rate_item = QTableWidgetItem(f"{row[4]:.2f}")
            rate_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            trans_item = QTableWidgetItem(f"{row[5]:.2f}")
            trans_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if not row[6] or not row[7]:
                rate_item.setFlags(rate_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                trans_item.setFlags(trans_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table_objects.setItem(row_idx, 3, rate_item)
            self.table_objects.setItem(row_idx, 4, trans_item)
            state_label = "В архиве" if not row[6] else "Активен" if row[7] else "Legacy: не связан"
            state_item = QTableWidgetItem(state_label)
            state_item.setFlags(state_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table_objects.setItem(row_idx, 5, state_item)
            if not row[6]:
                for column in range(self.table_objects.columnCount()):
                    cell = self.table_objects.item(row_idx, column)
                    cell.setForeground(QColor("#A0AEC0"))
                    cell.setBackground(QColor("#2D3748"))
            if selected_object_id is not None and int(row[0]) == int(selected_object_id):
                self.table_objects.selectRow(row_idx)

        self.table_objects.cellChanged.connect(self.on_object_cell_changed)
        self.update_object_archive_action()

    def update_object_archive_action(self):
        selected = self.table_objects.currentRow()
        if selected < 0:
            self.object_archive_button.setText("Выберите объект")
            self.object_archive_button.setEnabled(False)
            return
        item = self.table_objects.item(selected, 0)
        object_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        conn = get_db_connection()
        row = conn.execute(
            "SELECT is_active, sync_id, server_id FROM objects WHERE id = ?", (object_id,)
        ).fetchone() if object_id is not None else None
        pending = conn.execute(
            "SELECT 1 FROM object_sync_outbox WHERE object_id = ? LIMIT 1", (object_id,)
        ).fetchone() if object_id is not None else None
        conn.close()
        if row is None:
            self.object_archive_button.setText("Выберите объект")
            self.object_archive_button.setEnabled(False)
        elif not row["sync_id"] or not row["server_id"]:
            self.object_archive_button.setText("Ожидает связи с сервером")
            self.object_archive_button.setToolTip("Изменение активности доступно после устойчивой синхронизации объекта.")
            self.object_archive_button.setEnabled(False)
        elif pending:
            self.object_archive_button.setText("Синхронизация ожидается")
            self.object_archive_button.setToolTip("Дождитесь подтверждения текущего изменения объекта.")
            self.object_archive_button.setEnabled(False)
        else:
            archived = not bool(row["is_active"])
            self.object_archive_button.setText("Восстановить из архива" if archived else "В архив")
            self.object_archive_button.setToolTip("")
            self.object_archive_button.setEnabled(True)

    def toggle_object_archive(self):
        selected = self.table_objects.currentRow()
        if selected < 0:
            return
        item = self.table_objects.item(selected, 0)
        object_id = int(item.data(Qt.ItemDataRole.UserRole)) if item is not None else 0
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            row = cursor.execute(
                "SELECT is_active, sync_id, server_id FROM objects WHERE id = ?", (object_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Выбранный объект больше не существует.")
            if not row["sync_id"] or not row["server_id"]:
                raise ValueError("Объект ещё не имеет устойчивой связи с сервером.")
            if cursor.execute(
                "SELECT 1 FROM object_sync_outbox WHERE object_id = ? LIMIT 1", (object_id,)
            ).fetchone():
                raise ValueError("Предыдущее изменение объекта ещё не подтверждено сервером.")
            activating = not bool(row["is_active"])
            operation = "activate" if activating else "deactivate"
            cursor.execute(
                "UPDATE objects SET is_active = ? WHERE id = ? AND is_active = ?",
                (int(activating), object_id, int(bool(row["is_active"]))),
            )
            if cursor.rowcount != 1:
                raise ValueError("Состояние объекта изменилось; обновите справочник и повторите действие.")
            enqueue_object_sync_event(cursor, object_id, operation)
            conn.commit()
        except Exception as error:
            conn.rollback()
            conn.close()
            QMessageBox.warning(self, "Архив объектов", str(error))
            self.load_objects_table()
            return
        conn.close()
        self.load_dropdowns()
        self.background_sync_with_cloud()

    def load_employees_table(self):
        selected_employee_id = None
        current_row = self.table_employees.currentRow()
        if current_row >= 0:
            selected_item = self.table_employees.item(current_row, 0)
            if selected_item is not None:
                selected_employee_id = selected_item.data(Qt.ItemDataRole.UserRole)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT is_active, COUNT(*) FROM employees GROUP BY is_active")
        counts = {int(row[0]): int(row[1]) for row in cursor.fetchall()}
        self.employee_counts_label.setText(
            f"Активных: {counts.get(1, 0)} | Неактивных: {counts.get(0, 0)}"
        )
        cursor.execute("""
            SELECT id, name, salary_rate, is_active
            FROM employees
            WHERE is_active = 1 OR ? = 1
            ORDER BY is_active DESC, name, id
        """, (int(self.show_inactive_employees_check.isChecked()),))
        rows = cursor.fetchall()
        conn.close()

        pin_statuses = {}
        try:
            headers = sync_headers()
            res = requests.get(f"{CLOUD_URL}/admin/get-users-status", headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                for u in data.get("users", []):
                    pin_statuses[u["username"].strip().lower()] = u.get("has_pin", False)
        except Exception:
            pass

        self.table_employees.clearSelection()
        self.table_employees.setCurrentCell(-1, -1)
        self.table_employees.setRowCount(len(rows))
        for row_idx, row in enumerate(rows):
            employee_id, name, rate, is_active = int(row[0]), row[1], row[2], bool(row[3])
            has_pin = pin_statuses.get(name.strip().lower(), None)
            name_item = QTableWidgetItem(name)
            name_item.setData(Qt.ItemDataRole.UserRole, employee_id)
            self.table_employees.setItem(row_idx, 0, name_item)
            self.table_employees.setItem(row_idx, 1, QTableWidgetItem(f"{rate:.2f}" if rate else "0.00"))

            status_item = QTableWidgetItem("Активен" if is_active else "Неактивен")
            self.table_employees.setItem(row_idx, 2, status_item)
            pin_text = "Неизвестно" if has_pin is None else "Задан" if has_pin else "Не задан"
            pin_item = QTableWidgetItem(pin_text)
            if has_pin:
                pin_item.setForeground(QColor("#68D391"))
            elif has_pin is False:
                pin_item.setForeground(QColor("#F6AD55"))
            self.table_employees.setItem(row_idx, 3, pin_item)
            if not is_active:
                for column in range(self.table_employees.columnCount()):
                    cell = self.table_employees.item(row_idx, column)
                    cell.setForeground(QColor("#A0AEC0"))
                    cell.setBackground(QColor("#2D3748"))
            if selected_employee_id is not None and employee_id == int(selected_employee_id):
                self.table_employees.selectRow(row_idx)
        self.update_employee_status_action()

    def update_employee_status_action(self):
        selected = self.table_employees.currentRow()
        if selected < 0:
            self.employee_status_button.setText("Выберите сотрудника")
            self.employee_status_button.setEnabled(False)
            return
        item = self.table_employees.item(selected, 0)
        employee_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        conn = get_db_connection()
        row = conn.execute(
            "SELECT name, is_active FROM employees WHERE id = ?", (employee_id,)
        ).fetchone() if employee_id is not None else None
        conn.close()
        if row is None:
            self.employee_status_button.setText("Выберите сотрудника")
            self.employee_status_button.setEnabled(False)
            return
        self.employee_status_button.setText(
            "Деактивировать сотрудника" if row["is_active"] else "Активировать сотрудника"
        )
        self.employee_status_button.setEnabled(True)

    def reset_employee_pin_desktop(self):
        selected = self.table_employees.currentRow()
        if selected < 0:
            QMessageBox.warning(self, "Внимание", "Выберите сотрудника!")
            return
        emp_name = self.table_employees.item(selected, 0).text()
        
        new_pin, ok = QInputDialog.getText(
            self, 
            "Установка ПИН-кода", 
            f"Введите новый ПИН-код для сотрудника:\n{emp_name}", 
            QLineEdit.EchoMode.Normal
        )
        
        if ok and new_pin.strip():
            try:
                ensure_sync_dataset()
                headers = sync_headers()
                response = requests.post(
                    f"{CLOUD_URL}/admin/set-user-pin", 
                    headers=headers, 
                    json={"username": emp_name, "pin": new_pin.strip()}, 
                    timeout=5
                )
                if response.status_code == 200:
                    QMessageBox.information(self, "Успех", f"ПИН-код для {emp_name} успешно установлен!")
                    self.load_employees_table()
                else:
                    QMessageBox.critical(self, "Ошибка", f"Сервер отклонил запрос:\n{response.text}")
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", f"Не удалось связаться с облаком:\n{e}")

    def add_or_update_employee(self):
        name = self.new_emp_input.text().strip()
        rate_val = parse_float(self.new_emp_rate_input.text(), 0.0)
        if not name:
            return
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute('''
                INSERT INTO employees (name, salary_rate) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET salary_rate = ?
            ''', (name, rate_val, rate_val))
            enqueue_employee_sync_event(cursor, name, "activate")
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        conn.close()
        self.new_emp_input.clear()
        self.new_emp_rate_input.clear()
        self.load_dropdowns()
        self.background_sync_with_cloud()

    def toggle_employee_active(self):
        selected = self.table_employees.currentRow()
        if selected < 0:
            return
        item = self.table_employees.item(selected, 0)
        employee_id = int(item.data(Qt.ItemDataRole.UserRole)) if item is not None else 0
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            row = cursor.execute(
                "SELECT name, is_active FROM employees WHERE id = ?", (employee_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Выбранный сотрудник больше не существует.")
            employee_name = row["name"]
            employee_key = employee_sync_key(employee_name)
            pending = cursor.execute(
                "SELECT 1 FROM employee_sync_outbox WHERE employee_key = ? "
                "UNION ALL SELECT 1 FROM employee_restore_outbox WHERE employee_key = ? LIMIT 1",
                (employee_key, employee_key),
            ).fetchone()
            if pending:
                raise ValueError("Предыдущее изменение сотрудника ещё не подтверждено сервером.")
            activating = not bool(row["is_active"])
            operation = "activate" if activating else "deactivate"
            cursor.execute(
                "UPDATE employees SET is_active = ? WHERE id = ? AND is_active = ?",
                (int(activating), employee_id, int(bool(row["is_active"]))),
            )
            if cursor.rowcount != 1:
                raise ValueError("Состояние сотрудника изменилось; обновите список и повторите действие.")
            event_id = enqueue_employee_sync_event(cursor, employee_name, operation)
            if activating and event_id is None:
                raise ValueError("Активация не поставлена в очередь: локальный tombstone не подтверждён.")
            conn.commit()
        except Exception as error:
            conn.rollback()
            conn.close()
            QMessageBox.warning(self, "Сотрудники", str(error))
            self.load_employees_table()
            return
        conn.close()
        self.load_dropdowns()
        self.sync_with_cloud()

    def save_shift(self):
        date = self.date_input.text().strip()
        emp = self.emp_cb.currentText()
        comp = self.comp_cb.currentText()
        obj_name = self.obj_cb.currentData() or self.obj_cb.currentText()
        record_type = self.record_type_cb.currentData() or RECORD_TYPE_WORK
        conn = get_db_connection()
        cursor = conn.cursor()
        ob_result = None
        if record_type == RECORD_TYPE_OB:
            if not ob_schema_ready(cursor):
                conn.close()
                QMessageBox.warning(self, "Требуется миграция", "Сначала выполните migrate_ob.py с резервной копией desktop-базы.")
                return
            try:
                base_rate = get_local_employee_ob_base_rate(cursor, emp)
                if base_rate <= 0:
                    raise ValueError("В профиле сотрудника не настроена базовая ставка. Попросите администратора обновить профиль.")
                ob_result = calculate_work_interval(
                    date, self.ob_start_time.currentText(),
                    self.ob_end_time.currentText(),
                    base_rate,
                )
                date = ob_result["start_date"]
            except (TypeError, ValueError) as error:
                conn.close()
                QMessageBox.warning(self, "Некорректный интервал OB", str(error))
                return
        if record_type in SPECIAL_OBJECTS:
            special_row, error = resolve_special_object(cursor, comp, record_type)
            if error or not special_row or obj_name != special_row['name']:
                conn.close()
                QMessageBox.warning(self, "Справочник объекта", error or "Выберите существующую справочную запись для выбранного типа.")
                return
        elif record_type not in (RECORD_TYPE_WORK, RECORD_TYPE_OB):
            conn.close()
            QMessageBox.warning(self, "Тип записи", "Необходимо выбрать известный тип записи.")
            return
        else:
            cursor.execute("SELECT COALESCE(markning, '') FROM objects WHERE company = ? AND name = ? AND is_active = 1", (comp, obj_name))
            object_row = cursor.fetchone()
            if object_row and any(
                matches_special_object(obj_name, object_row[0], reference)
                for reference in SPECIAL_OBJECTS.values()
            ):
                conn.close()
                QMessageBox.warning(self, "Тип записи", "Служебные объекты нельзя сохранить как обычную работу.")
                return
        
        hours_val = ob_result["total_hours"] if ob_result else parse_float(self.hours_input.text(), -1)
        
        rate_input_str = self.rate_input.text().strip()
        if ob_result:
            rate_val = 0.0
        elif rate_input_str:
            rate_val = parse_float(rate_input_str, -1)
        else:
            cursor.execute("SELECT rate FROM objects WHERE name = ? AND company = ? AND is_active = 1", (obj_name, comp))
            obj_row = cursor.fetchone()
            rate_val = obj_row[0] if obj_row and obj_row[0] else 0.0

        trans_val = parse_float(self.trans_input.text(), 0.0)
        comment = self.comment_input.text().strip()
        if record_type in (RECORD_TYPE_WORK, RECORD_TYPE_OB):
            rate_val = 0.0
        elif record_type in SPECIAL_OBJECTS:
            rate_val = 0.0
            trans_val = 0.0

        if hours_val < 0 or rate_val < 0 or not math.isfinite(trans_val) or not 0 <= trans_val <= 10000:
            conn.close()
            QMessageBox.warning(self, "Ошибка", "Проверьте часы, ставку и транспорт (от 0 до 10000)!")
            return

        sync_id = str(uuid.uuid4())
        cursor.execute('''
            INSERT INTO shifts (
                date, employee, company, object_name, hours, rate, transport, comment,
                invoiced, sync_id, sync_version, entry_type, work_rate_cache, work_transport_cache
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?, ?)
        ''', (
            date, emp, comp, obj_name, hours_val, rate_val, trans_val, comment,
            sync_id, record_type,
            rate_val if record_type == RECORD_TYPE_WORK else 0.0,
            trans_val if record_type == RECORD_TYPE_WORK else 0.0
        ))
        if ob_result:
            shift_id = cursor.lastrowid
            cursor.execute(
                "UPDATE shifts SET start_time = ?, end_time = ?, ob_base_rate = ? WHERE id = ?",
                (ob_result["start_time"], ob_result["end_time"], ob_result["segments"][0]["base_rate"], shift_id),
            )
            replace_ob_segments(cursor, sync_id, ob_result["segments"])
        payload = {
            "date": date, "employee": emp, "company": comp, "object_name": obj_name,
            "hours": hours_val, "rate": rate_val, "transport": trans_val,
            "comment": comment, "invoiced": 0, "record_type": record_type
        }
        if ob_result:
            payload.update({
                "start_time": ob_result["start_time"],
                "end_time": ob_result["end_time"],
            })
        enqueue_shift_operation(cursor, sync_id, 'upsert', 0, payload)
        conn.commit()
        conn.close()
        
        if record_type in (
            RECORD_TYPE_WORK, RECORD_TYPE_OB, RECORD_TYPE_VACATION, RECORD_TYPE_HOLIDAY,
        ):
            self.background_sync_with_cloud()
            QMessageBox.information(self, "Успех", "OB-интервал сохранён и рассчитан." if record_type == RECORD_TYPE_OB else "Смена успешно сохранена!")
        else:
            QMessageBox.information(self, "Сохранено локально", "Запись сохранена отдельно от рабочих часов. Сервер не поддерживает этот тип, поэтому запись остаётся в локальной очереди.")
        self.load_shifts_history()

    def load_shifts_history(self):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            has_ob_schema = ob_schema_ready(cursor)
            if has_ob_schema:
                cursor.execute("SELECT id, date, employee, company, object_name, entry_type, hours, rate, transport, comment, invoiced, start_time, end_time, sync_id FROM shifts ORDER BY id DESC LIMIT 200")
            else:
                cursor.execute("SELECT id, date, employee, company, object_name, entry_type, hours, rate, transport, comment, invoiced FROM shifts ORDER BY id DESC LIMIT 200")
            rows = cursor.fetchall()

            ob_segments_by_id = {}
            if has_ob_schema:
                sync_ids = [row["sync_id"] for row in rows if row["entry_type"] == RECORD_TYPE_OB and row["sync_id"]]
                for sid in sync_ids:
                    ob_segments_by_id[sid] = get_ob_segments(cursor, sid)
            conn.close()

            self.table_shifts.setRowCount(len(rows))
            for row_idx, row in enumerate(rows):
                is_inv = row["invoiced"] == 1
                status_text = "✅ Выставлен" if is_inv else "⏳ В работе"

                entry_type = row["entry_type"] or RECORD_TYPE_WORK
                has_start = bool("start_time" in row.keys() and row["start_time"])
                has_end = bool("end_time" in row.keys() and row["end_time"])
                if entry_type == RECORD_TYPE_WORK and has_start and has_end:
                    entry_type = RECORD_TYPE_OB
                short_type = record_type_short_label(entry_type)

                detalization = ""
                ob_amount_str = ""
                detail_tooltip = ""
                if has_ob_schema and entry_type == RECORD_TYPE_OB:
                    segments = ob_segments_by_id.get(row["sync_id"], [])
                    if not segments and has_start and has_end:
                        try:
                            calc_conn = get_db_connection()
                            base_rate = float(row['ob_base_rate'] or 0.0) if "ob_base_rate" in row.keys() else 0.0
                            if base_rate <= 0:
                                base_rate = get_local_employee_ob_base_rate(calc_conn.cursor(), row['employee'])
                            calc_res = calculate_work_interval(row['date'], row['start_time'], row['end_time'], base_rate)
                            replace_ob_segments(calc_conn.cursor(), row['sync_id'], calc_res["segments"])
                            calc_conn.commit()
                            calc_conn.close()
                            segments = calc_res["segments"]
                        except Exception:
                            pass
                    ob_amount = sum(segment["amount"] for segment in segments)
                    ob_amount_str = f"{ob_amount:.2f}"
                    detalization = format_ob_detail(row['start_time'], row['end_time'], row['hours'], segments)
                    if segments:
                        detail_tooltip = ";\n".join(
                            f"{s['segment_date']} {s['start_time']}–{s['end_time']}: {s['hours']:.2f} ч (×{s['multiplier']:.2f}, {s['amount']:.2f} kr)"
                            for s in segments
                        ) + f"\nИтого OB: {ob_amount:.2f} kr"
                elif entry_type == RECORD_TYPE_ATF:
                    detalization = "АТФ (p1000)"
                elif entry_type == RECORD_TYPE_SICK:
                    detalization = "Больничный (p1001)"
                elif entry_type == RECORD_TYPE_VACATION:
                    detalization = "Отпуск / Semestertimmar (p1002)"
                elif entry_type == RECORD_TYPE_HOLIDAY:
                    detalization = "Красные дни / Röda dagar (p1004)"

                values = [
                    row["id"],                                       # 0: ID
                    row["date"],                                     # 1: Дата
                    row["employee"],                                 # 2: Сотрудник
                    row["company"],                                  # 3: Фирма
                    row["object_name"],                              # 4: Объект
                    short_type,                                      # 5: Тип
                    f"{float(row['hours'] or 0.0):.1f}",             # 6: Часы
                    detalization,                                    # 7: Детализация / OB
                    f"{float(row['rate'] or 0.0):.2f}" if row['rate'] else "0.00", # 8: Факт. ставка
                    f"{float(row['transport'] or 0.0):.1f}" if row['transport'] else "0.0", # 9: Транспорт
                    row["comment"] or "",                            # 10: Комментарий
                    status_text,                                     # 11: Статус фактуры
                    ob_amount_str,                                   # 12: OB сумма (SEK)
                ]

                for col_idx, val in enumerate(values):
                    it = QTableWidgetItem(str(val))
                    if col_idx in (1, 5, 6, 8, 9, 11, 12):
                        it.setTextAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
                    if col_idx == 7 and detail_tooltip:
                        it.setToolTip(detail_tooltip)
                    elif col_idx in (2, 3, 4, 7, 10) and val:
                        it.setToolTip(str(val))
                    if is_inv:
                        it.setBackground(QColor("#1c4532"))
                    self.table_shifts.setItem(row_idx, col_idx, it)
            self.load_dropdowns()
            self.apply_shifts_filters()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось обновить данные: {e}")

    def edit_shift(self):
        selected = self.table_shifts.currentRow()
        if selected < 0:
            QMessageBox.warning(self, "Внимание", "Выберите смену для изменения!")
            return

        local_id = self.table_shifts.item(selected, 0).text()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM shifts WHERE id = ?", (local_id,))
        shift = cursor.fetchone()
        has_ob_schema = ob_schema_ready(cursor)
        stored_ob_segments = (get_ob_segments(cursor, shift["sync_id"])
                              if shift and has_ob_schema and shift["entry_type"] == RECORD_TYPE_OB else [])
        cursor.execute("SELECT name, is_active FROM employees ORDER BY name")
        employees = cursor.fetchall()
        cursor.execute("SELECT name FROM companies ORDER BY name")
        companies = [row[0] for row in cursor.fetchall()]
        conn.close()
        if not shift:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Изменить смену")
        form = QFormLayout(dialog)
        date_field = create_date_field(shift['date'])
        employee_field = QComboBox()
        employee_field.setEditable(True)
        for employee in employees:
            name = employee["name"]
            is_active = bool(employee["is_active"])
            display = name if is_active else f"{name} — Неактивен"
            employee_field.addItem(display, userData=name)
        if employee_field.findData(shift['employee']) < 0:
            employee_field.addItem(f"{shift['employee']} — Неактивен", userData=shift['employee'])
        employee_field.setCurrentIndex(employee_field.findData(shift['employee']))

        def selected_employee_name():
            current_index = employee_field.currentIndex()
            selected_name = employee_field.itemData(current_index) if current_index >= 0 else None
            if selected_name and employee_field.currentText() == employee_field.itemText(current_index):
                return str(selected_name).strip()
            return employee_field.currentText().strip()

        company_field = QComboBox()
        company_field.setEditable(True)
        company_field.addItems(companies)
        company_field.setCurrentText(shift['company'])
        record_type_field = QComboBox()
        for record_type in (
            RECORD_TYPE_WORK, RECORD_TYPE_OB, RECORD_TYPE_ATF,
            RECORD_TYPE_SICK, RECORD_TYPE_VACATION, RECORD_TYPE_HOLIDAY,
        ):
            record_type_field.addItem(record_type_label(record_type), record_type)
        current_type = shift['entry_type'] or RECORD_TYPE_REVIEW
        work_values = [
            float(shift['work_rate_cache'] or (shift['rate'] if current_type == RECORD_TYPE_WORK else 0.0)),
            float(shift['work_transport_cache'] or (shift['transport'] if current_type == RECORD_TYPE_WORK else 0.0)),
        ]
        previous_type = [current_type]
        type_index = record_type_field.findData(current_type)
        if type_index < 0:
            record_type_field.insertItem(0, record_type_label(RECORD_TYPE_REVIEW), RECORD_TYPE_REVIEW)
            type_index = 0
        record_type_field.setCurrentIndex(type_index)
        object_field = QComboBox()
        self.load_objects_for_company(
            shift['company'], object_field, current_type,
            archived_object_name=shift['object_name'],
        )
        object_index = object_field.findData(shift['object_name'])
        if object_index >= 0:
            object_field.setCurrentIndex(object_index)
        def reload_dialog_objects(*_):
            company = company_field.currentText()
            record_type = record_type_field.currentData()
            archived_name = shift['object_name'] if (
                company == shift['company'] and record_type == current_type
            ) else None
            self.load_objects_for_company(
                company, object_field, record_type, archived_object_name=archived_name
            )

        company_field.currentTextChanged.connect(reload_dialog_objects)
        record_type_field.currentIndexChanged.connect(reload_dialog_objects)
        hours_field = QDoubleSpinBox()
        hours_field.setRange(0.5, 24)
        hours_field.setSingleStep(0.5)
        hours_field.setDecimals(1)
        hours_field.setValue(shift['hours'])
        rate_field = QDoubleSpinBox()
        rate_field.setRange(0, 100000)
        rate_field.setDecimals(2)
        rate_field.setValue(shift['rate'])
        original_transport = float(shift["transport"] or 0)
        if original_transport in (0, 1):
            transport_field = QCheckBox("Учитывать поездку")
            transport_field.setChecked(bool(original_transport))
            def transport_amount():
                return float(transport_field.isChecked())
            def set_transport_amount(value):
                transport_field.setChecked(bool(value))
        else:
            transport_field = QDoubleSpinBox()
            transport_field.setRange(0, 10000)
            transport_field.setDecimals(2)
            transport_field.setValue(original_transport)
            transport_field.setToolTip("Прежнее числовое значение сохранено; новые поездки отмечаются галочкой.")
            transport_amount = transport_field.value
            set_transport_amount = transport_field.setValue
        transport_field.setObjectName("edit_shift_transport")
        hours_field.setObjectName("edit_shift_hours")
        rate_field.setObjectName("edit_shift_rate")
        transport_value = [transport_amount()]
        comment_field = QLineEdit(shift['comment'] or '')
        ob_start_field = create_half_hour_time_field((shift["start_time"] if has_ob_schema else None) or "07:00")
        ob_end_field = create_half_hour_time_field((shift["end_time"] if has_ob_schema else None) or "18:00")
        ob_preview_field = QLabel("")
        ob_preview_field.setWordWrap(True)

        def unchanged_ob_interval():
            return (current_type == RECORD_TYPE_OB and record_type_field.currentData() == RECORD_TYPE_OB
                    and date_field.text() == shift["date"]
                    and selected_employee_name() == shift["employee"]
                    and ob_start_field.currentText() == shift["start_time"]
                    and ob_end_field.currentText() == shift["end_time"]
                    and bool(stored_ob_segments))

        def update_type_values():
            new_type = record_type_field.currentData()
            if previous_type[0] == RECORD_TYPE_WORK and new_type != RECORD_TYPE_WORK:
                work_values[0] = rate_field.value()
                work_values[1] = transport_amount()
                rate_field.setValue(0.0)
            elif previous_type[0] != RECORD_TYPE_WORK and new_type == RECORD_TYPE_WORK:
                rate_field.setValue(work_values[0])
            elif new_type != RECORD_TYPE_WORK:
                rate_field.setValue(0.0)
            if new_type not in (RECORD_TYPE_WORK, RECORD_TYPE_OB):
                if previous_type[0] in (RECORD_TYPE_WORK, RECORD_TYPE_OB):
                    transport_value[0] = transport_amount()
                set_transport_amount(0.0)
            elif previous_type[0] not in (RECORD_TYPE_WORK, RECORD_TYPE_OB):
                set_transport_amount(transport_value[0])
            previous_type[0] = new_type

        def refresh_dialog_ob_preview(*_):
            if record_type_field.currentData() != RECORD_TYPE_OB:
                ob_preview_field.setText("")
                return
            if unchanged_ob_interval():
                ob_preview_field.setText(
                    "<br>".join(
                        f"{segment['segment_date']} {segment['start_time']}–{segment['end_time']}: "
                        f"{segment['hours']:.2f} ч, сумма {segment['amount']:.2f} kr"
                        for segment in stored_ob_segments)
                    + "<br><small>Сохранённый расчёт ОВ. Без изменения даты, сотрудника и интервала не пересчитывается.</small>")
                return
            preview_conn = get_db_connection()
            try:
                base_rate = get_local_employee_ob_base_rate(
                    preview_conn.cursor(), selected_employee_name()
                )
                if base_rate <= 0:
                    raise ValueError("В профиле сотрудника не настроена базовая ставка. Попросите администратора обновить профиль.")
                result = calculate_work_interval(
                    date_field.text(), ob_start_field.currentText(),
                    ob_end_field.currentText(), base_rate,
                )
            except (TypeError, ValueError) as error:
                ob_preview_field.setText(f"Ошибка интервала: {error}")
                return
            finally:
                preview_conn.close()
            ob_preview_field.setText(
                "<br>".join(
                    f"{segment['segment_date']} {segment['start_time']}–{segment['end_time']}: "
                    f"{segment['hours']:.2f} ч, +{segment['premium_percent']:.0f}%, "
                    f"×{segment['multiplier']:.2f}, сумма {segment['amount']:.2f} kr"
                    for segment in result["segments"]
                ) + f"<br><b>Всего: {result['total_hours']:.2f} ч; {result['total_amount']:.2f} kr</b>"
                + "<br><small>Начальные тарифы требуют подтверждения по применяемому kollektivavtal.</small>"
            )

        def update_dialog_ob_visibility(*_):
            is_ob = record_type_field.currentData() == RECORD_TYPE_OB
            for widget in (hours_field, rate_field):
                widget.setVisible(not is_ob)
                label = form.labelForField(widget)
                if label:
                    label.setVisible(not is_ob)
            for widget in (ob_start_field, ob_end_field, ob_preview_field):
                widget.setVisible(is_ob)
                label = form.labelForField(widget)
                if label:
                    label.setVisible(is_ob)
            refresh_dialog_ob_preview()

        record_type_field.currentIndexChanged.connect(update_type_values)
        record_type_field.currentIndexChanged.connect(update_dialog_ob_visibility)
        employee_field.currentTextChanged.connect(refresh_dialog_ob_preview)
        for ob_widget in (ob_start_field, ob_end_field):
            ob_widget.currentTextChanged.connect(refresh_dialog_ob_preview)
        date_field.dateChanged.connect(refresh_dialog_ob_preview)

        form.addRow("Дата:", date_field)
        form.addRow("Сотрудник:", employee_field)
        form.addRow("Фирма:", company_field)
        form.addRow("Тип записи:", record_type_field)
        form.addRow("Объект:", object_field)
        form.addRow("Часы:", hours_field)
        form.addRow("Ставка:", rate_field)
        form.addRow("Транспорт:", transport_field)
        form.addRow("Комментарий:", comment_field)
        form.addRow("Время начала:", ob_start_field)
        form.addRow("Время окончания:", ob_end_field)
        form.addRow("Рассчитанные OB-сегменты:", ob_preview_field)
        update_dialog_ob_visibility()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        record_type = record_type_field.currentData()
        object_name = object_field.currentData() or object_field.currentText().strip()
        ob_result = None
        check_conn = get_db_connection()
        check_cursor = check_conn.cursor()
        if record_type in SPECIAL_OBJECTS:
            special_row, error = resolve_special_object(check_cursor, company_field.currentText().strip(), record_type)
            if error or not special_row or special_row['name'] != object_name:
                check_conn.close()
                QMessageBox.warning(self, "Справочник объекта", error or "Выберите существующую справочную запись для выбранного типа.")
                return
        elif record_type not in (RECORD_TYPE_WORK, RECORD_TYPE_OB):
            check_conn.close()
            QMessageBox.warning(self, "Тип записи", "Необходимо выбрать известный тип записи.")
            return
        else:
            check_cursor.execute("SELECT COALESCE(markning, '') FROM objects WHERE company = ? AND name = ? AND is_active = 1", (company_field.currentText().strip(), object_name))
            object_row = check_cursor.fetchone()
            if object_row and any(
                matches_special_object(object_name, object_row[0], reference)
                for reference in SPECIAL_OBJECTS.values()
            ):
                check_conn.close()
                QMessageBox.warning(self, "Тип записи", "Служебные объекты нельзя сохранить как обычную работу.")
                return
        check_conn.close()

        preserve_ob = unchanged_ob_interval()
        if record_type == RECORD_TYPE_OB and not preserve_ob:
            schema_conn = get_db_connection()
            schema_ready = ob_schema_ready(schema_conn.cursor())
            rate_cursor = schema_conn.cursor()
            try:
                employee_base_rate = get_local_employee_ob_base_rate(
                    rate_cursor, selected_employee_name()
                ) if schema_ready else None
                if employee_base_rate is not None and employee_base_rate <= 0:
                    raise ValueError("В профиле сотрудника не настроена базовая ставка. Попросите администратора обновить профиль.")
            except ValueError as error:
                schema_conn.close()
                QMessageBox.warning(self, "Профиль сотрудника", str(error))
                return
            schema_conn.close()
            if not schema_ready:
                QMessageBox.warning(self, "Требуется миграция", "Сначала выполните migrate_ob.py с резервной копией desktop-базы.")
                return
            try:
                ob_result = calculate_work_interval(
                    date_field.text(), ob_start_field.currentText(),
                    ob_end_field.currentText(), employee_base_rate,
                )
                payload_date = ob_result["start_date"]
            except (TypeError, ValueError) as error:
                QMessageBox.warning(self, "Некорректный интервал OB", str(error))
                return
        else:
            payload_date = date_field.text()

        payload = {
            "date": payload_date, "employee": selected_employee_name(),
            "company": company_field.currentText().strip(), "object_name": object_name,
            "hours": shift["hours"] if preserve_ob else ob_result["total_hours"] if ob_result else hours_field.value(),
            "rate": rate_field.value() if record_type == RECORD_TYPE_WORK else 0.0,
            "transport": transport_amount() if record_type in (RECORD_TYPE_WORK, RECORD_TYPE_OB) else 0.0,
            "comment": comment_field.text().strip(),
            "invoiced": shift['invoiced'] or 0, "record_type": record_type
        }
        if ob_result:
            payload.update({
                "start_time": ob_result["start_time"], "end_time": ob_result["end_time"],
            })
        elif preserve_ob:
            payload.update(start_time=shift["start_time"], end_time=shift["end_time"])
        if not payload['employee'] or not payload['company'] or not payload['object_name']:
            QMessageBox.warning(self, "Ошибка", "Заполните сотрудника, фирму и объект.")
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            sync_id = shift['sync_id'] or str(uuid.uuid4())
            if record_type != shift['entry_type']:
                cursor.execute("UPDATE shift_sync_outbox SET state = 'blocked' WHERE sync_id = ? AND state = 'pending'", (sync_id,))
                if record_type == RECORD_TYPE_WORK:
                    cursor.execute("DELETE FROM shift_sync_outbox WHERE sync_id = ? AND state = 'blocked'", (sync_id,))
            if ob_schema_ready(cursor):
                cursor.execute('''
                    UPDATE shifts SET date = ?, employee = ?, company = ?, object_name = ?, hours = ?,
                        rate = ?, transport = ?, comment = ?, sync_id = ?, entry_type = ?,
                        work_rate_cache = ?, work_transport_cache = ?, start_time = ?, end_time = ?, ob_base_rate = ?
                    WHERE id = ?
                ''', (
                    payload['date'], payload['employee'], payload['company'], payload['object_name'],
                    payload['hours'], payload['rate'], payload['transport'], payload['comment'], sync_id, record_type,
                    rate_field.value() if record_type == RECORD_TYPE_WORK else work_values[0],
                    transport_amount() if record_type == RECORD_TYPE_WORK else work_values[1],
                    shift["start_time"] if preserve_ob else ob_result["start_time"] if ob_result else None,
                    shift["end_time"] if preserve_ob else ob_result["end_time"] if ob_result else None,
                    shift["ob_base_rate"] if preserve_ob else ob_result["segments"][0]["base_rate"] if ob_result else None,
                    local_id
                ))
            else:
                cursor.execute('''
                    UPDATE shifts SET date = ?, employee = ?, company = ?, object_name = ?, hours = ?,
                        rate = ?, transport = ?, comment = ?, sync_id = ?, entry_type = ?,
                        work_rate_cache = ?, work_transport_cache = ?
                    WHERE id = ?
                ''', (
                    payload['date'], payload['employee'], payload['company'], payload['object_name'],
                    payload['hours'], payload['rate'], payload['transport'], payload['comment'], sync_id, record_type,
                    rate_field.value() if record_type == RECORD_TYPE_WORK else work_values[0],
                    transport_amount() if record_type == RECORD_TYPE_WORK else work_values[1],
                    local_id
                ))
            if ob_result:
                replace_ob_segments(cursor, sync_id, ob_result["segments"])
            elif not preserve_ob and ob_schema_ready(cursor) and shift['entry_type'] == RECORD_TYPE_OB:
                cursor.execute("DELETE FROM ob_segments WHERE shift_id = ?", (sync_id,))
            enqueue_shift_operation(cursor, sync_id, 'upsert', shift['sync_version'], payload)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self.load_shifts_history()
        if record_type in (RECORD_TYPE_WORK, RECORD_TYPE_OB, RECORD_TYPE_HOLIDAY):
            self.background_sync_with_cloud()
            QMessageBox.information(self, "Изменение сохранено", "Запись сохранена.")
        elif record_type == RECORD_TYPE_VACATION:
            self.background_sync_with_cloud()
            QMessageBox.information(self, "Изменение сохранено", "Запись сохранена и поставлена в очередь синхронизации.")
        else:
            self.record_type_status_lbl.setText("Изменение сохранено локально. Сервер не поддерживает типы АТФ/больничного; ранее отправленная версия работы на сервере автоматически не изменена.")
            QMessageBox.information(self, "Сохранено локально", "Запись изменена локально. Для согласования её типа на сервере потребуется отдельное обновление серверного кода.")

    def delete_shift(self, index=None):
        selected = self.table_shifts.currentRow()
        if selected < 0:
            QMessageBox.warning(self, "Внимание", "Выберите смену для удаления!")
            return

        shift_id = self.table_shifts.item(selected, 0).text()
        reply = QMessageBox.question(self, "Подтверждение", "Удалить смену?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,))
            shift = cursor.fetchone()
            if shift:
                sync_id = shift['sync_id'] or str(uuid.uuid4())
                enqueue_shift_operation(cursor, sync_id, 'delete', shift['sync_version'], shift_payload(shift))
                if ob_schema_ready(cursor):
                    cursor.execute("DELETE FROM ob_segments WHERE shift_id = ?", (sync_id,))
                cursor.execute('DELETE FROM shifts WHERE id = ?', (shift_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        self.load_shifts_history()
        if shift and shift['entry_type'] in (
            RECORD_TYPE_WORK, RECORD_TYPE_OB, RECORD_TYPE_VACATION, RECORD_TYPE_HOLIDAY,
        ):
            self.background_sync_with_cloud()
        elif shift:
            QMessageBox.information(self, "Удалено локально", "Запись удалена из десктопной базы. Сервер не поддерживает синхронизацию удаления АТФ/больничного.")

if __name__ == "__main__":
    try:
        if getattr(sys, "frozen", False):
            logging.basicConfig(filename=str(desktop_auth.app_data_dir() / "smartreport.log"), level=logging.INFO,
                                format="%(asctime)s [%(levelname)s] %(message)s", encoding="utf-8")
        if "--logout" in sys.argv:
            desktop_auth.logout(CLOUD_URL)
            sys.exit(0)
        if "--backup" in sys.argv:
            import server_backup
            token = desktop_auth.load_saved_token()
            folder = sys.argv[sys.argv.index("--backup") + 1]
            target, manifest = server_backup.pull_backup(desktop_auth.server_url(), token, folder)
            print(f"Копия сохранена: {target}; файлов: {len(manifest['files'])}; проблем: {len(manifest['problems'])}")
            sys.exit(1 if manifest["problems"] else 0)
        if "--diagnostic" in sys.argv:
            import argparse
            import desktop_diagnostics
            parser = argparse.ArgumentParser()
            parser.add_argument("--diagnostic", required=True)
            parser.add_argument("--case", default="case")
            parser.add_argument("--work", required=True)
            parser.add_argument("--out", required=True)
            options = parser.parse_args()
            try:
                desktop_diagnostics.run(sys.modules[__name__], options.diagnostic, options.work, options.case, options.out)
            except BaseException as error:
                Path(options.out).write_text(json.dumps({"error": f"{type(error).__name__}: {error}"}, ensure_ascii=False), encoding="utf-8")
                sys.exit(1)
            sys.exit(0)
        app = QApplication(sys.argv)
        app.setStyleSheet(DARK_THEME_QSS)
        resource_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        icon_path = next((p for p in (resource_dir / "Logo.ico", Path("logo.ico")) if p.exists()), None)
        if icon_path:
            app.setWindowIcon(QIcon(str(icon_path)))
        from desktop_login import ensure_login
        if not ensure_login():
            sys.exit(0)
        CLOUD_URL = desktop_auth.server_url()
        window = SmartReportApp()
        window.show()
        sys.exit(app.exec())
    except SystemExit:
        raise
    except Exception as e:
        logging.exception("Критическая ошибка запуска")
        try:
            traceback.print_exc()
        except Exception:
            pass
        if getattr(sys, "frozen", False):
            QMessageBox.critical(None, "SmartReport", f"Программа не запустилась: {e}")
        else:
            input("\nНажмите Enter...")