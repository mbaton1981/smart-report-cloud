import sys
import os
import shutil
import traceback
import subprocess
import sqlite3
import requests
import json
import uuid
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape
from datetime import datetime, timezone

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QTabWidget,
    QFrame, QFormLayout, QDateEdit, QCheckBox, QFileDialog, QInputDialog,
    QDialog, QDialogButtonBox, QDoubleSpinBox, QScrollArea, QSizePolicy,
    QTextEdit, QSplitter
)
from PyQt6.QtCore import Qt, QDate, QTimer, QThread, pyqtSignal, QEvent
from PyQt6.QtGui import QIcon, QColor, QDesktopServices, QImage, QPixmap
from PyQt6.QtCore import QUrl

# Импорты для генерации PDF
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, LongTable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from ob_calculator import calculate_work_interval, round_time_to_half_hour
from expense_documents import (
    apply_expense_documents_migration, decimal_text, document_storage_root,
    extract_local_document, find_document_duplicates, get_object_expense_summary,
    match_objects, parse_decimal, parse_document_text, store_original_file,
    validate_allocations,
)

# Локальная база данных SQLite и папки
DB_FILE = os.environ.get("SMARTREPORT_DB_FILE", "smart_report.db")
BACKUP_DIR = "backups"
REPORTS_DIR = "reports"
SALARIES_DIR = "salaries"

# 🌐 Адрес облачного сервера на Render и ваш секретный ключ синхронизации
CLOUD_URL = os.environ.get("SMARTREPORT_CLOUD_URL", "https://smart-report-server.onrender.com")
SYNC_API_KEY = os.environ.get("SYNC_API_KEY", "")

RECORD_TYPE_WORK = "work"
RECORD_TYPE_ATF = "atf"
RECORD_TYPE_SICK = "sick"
RECORD_TYPE_OB = "ob"
RECORD_TYPE_REVIEW = "review"
RECORD_TYPE_LABELS = {
    RECORD_TYPE_WORK: "Обычная работа",
    RECORD_TYPE_OB: "OB / Сверхурочные",
    RECORD_TYPE_ATF: "АТФ",
    RECORD_TYPE_SICK: "Больничный",
    RECORD_TYPE_REVIEW: "Требует проверки",
}

def record_type_short_label(record_type):
    mapping = {
        RECORD_TYPE_WORK: "Работа",
        RECORD_TYPE_OB: "OB",
        RECORD_TYPE_ATF: "АТФ",
        RECORD_TYPE_SICK: "Больничный",
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
}

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

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
            name TEXT UNIQUE NOT NULL,
            markning TEXT,
            company TEXT,
            rate REAL DEFAULT 0.0,
            transport_rate REAL DEFAULT 0.0
        )
    ''')
    
    cursor.execute("PRAGMA table_info(objects)")
    obj_columns = [col[1] for col in cursor.fetchall()]
    if 'rate' not in obj_columns:
        cursor.execute("ALTER TABLE objects ADD COLUMN rate REAL DEFAULT 0.0")
    if 'transport_rate' not in obj_columns:
        cursor.execute("ALTER TABLE objects ADD COLUMN transport_rate REAL DEFAULT 0.0")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            salary_rate REAL DEFAULT 0.0,
            is_active INTEGER NOT NULL DEFAULT 1
        )
    ''')

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
        cursor.execute("SELECT id, company, object_name FROM shifts WHERE object_name IN (?, ?)", tuple(item[0] for item in SPECIAL_OBJECTS.values()))
        for old_shift in cursor.fetchall():
            matching_types = []
            for record_type, (object_name, marking) in SPECIAL_OBJECTS.items():
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

    conn.commit()
    conn.close()
    apply_expense_documents_migration(DB_FILE, Path(DB_FILE).resolve().parent / BACKUP_DIR)

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
    allowed_types = set(allowed_types or (RECORD_TYPE_WORK, RECORD_TYPE_ATF, RECORD_TYPE_SICK))
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
    cursor.execute(
        "SELECT id, name, markning, company FROM objects WHERE company = ? AND name = ? AND COALESCE(markning, '') = ?",
        (company, reference[0], reference[1])
    )
    matches = cursor.fetchall()
    if len(matches) != 1:
        if not matches:
            return None, f"В справочнике фирмы «{company}» не найдена запись «{reference[0]} / {reference[1]}»."
        return None, f"В справочнике фирмы «{company}» неоднозначна запись «{reference[0]} / {reference[1]}»."
    return matches[0], None

def enqueue_matching_shifts(cursor, where_clause, params):
    cursor.execute(f"SELECT * FROM shifts WHERE {where_clause}", params)
    for row in cursor.fetchall():
        sync_id = row['sync_id'] or str(uuid.uuid4())
        if not row['sync_id']:
            cursor.execute("UPDATE shifts SET sync_id = ? WHERE id = ?", (sync_id, row['id']))
        enqueue_shift_operation(cursor, sync_id, 'upsert', row['sync_version'], shift_payload(row))

class SyncWorker(QThread):
    completed = pyqtSignal(bool, int, str)

    def __init__(self, window):
        super().__init__(window)
        self.window = window

    def run(self):
        try:
            conflicts = self.window.perform_sync_logic()
            self.completed.emit(True, conflicts, "")
        except Exception as error:
            self.completed.emit(False, 0, str(error))

def parse_float(val_str, default=0.0):
    if not val_str:
        return default
    try:
        cleaned = str(val_str).strip().replace(',', '.')
        return float(cleaned)
    except ValueError:
        return default

def translate_to_swedish(text):
    if not text or not str(text).strip():
        return ""
    
    raw_text = str(text).strip()
    
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
    replacements = {
        "гипсокартон": "gips", "гипсу": "gips", "картон": "kartong",
        "вентиляц": "ventilation", "электрик": "elarbete", "проводк": "eldragning",
        "розетк": "vägguttag", "демонтаж": "rivning", "демонт": "rivning",
        "монтаж": "montering", "монт": "montering", "покраск": "målning",
        "красил": "målade", "краск": "färg", "утеплен": "isolering",
        "изоляц": "isolering", "установк": "installation", "шпаклев": "spackling",
        "шпатлев": "spackling", "уборк": "städning", "убирал": "städade",
        "ремонт": "renovering", "стен": "väggar", "потолк": "tak",
        "пол": "golv", "окн": "fönster", "двер": "dörrar", "плитк": "kakel",
        "замен": "byte", "стяжк": "avjämningsmassa", "работ": "arbeten",
        "мусор": "avfall", "доставк": "leverans"
    }
    words = t.split()
    translated_words = []
    for word in words:
        clean_w = word.strip(".,;:!?()[]{}\"'")
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
    return " ".join(translated_words)

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
        border-radius: 6px;
        font-weight: 600;
    }
    QPushButton:hover {
        background-color: #3c495f;
        border-color: #4299e1;
    }
    QPushButton:pressed {
        background-color: #4299e1;
        border-color: #3182ce;
        padding-top: 8px;
        padding-bottom: 6px;
    }
    QPushButton:disabled {
        background-color: #252b35;
        color: #718096;
        border-color: #323946;
    }
    QPushButton:focus {
        border: 2px solid #90cdf4;
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
    QLineEdit, QComboBox, QDateEdit {
        background-color: #28303d;
        color: #f7fafc;
        border: 1px solid #3f4c60;
        padding: 6px 12px;
        border-radius: 6px;
        selection-background-color: #3182ce;
        selection-color: #ffffff;
    }
    QLineEdit:focus, QComboBox:focus, QDateEdit:focus {
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
    QTableWidget, QTableView {
        background-color: #1a1f26;
        alternate-background-color: #212832;
        color: #e2e8f0;
        gridline-color: #2d3748;
        border: 1px solid #323946;
        border-radius: 6px;
        selection-background-color: #2b6cb0;
        selection-color: #ffffff;
    }
    QTableWidget::item:selected {
        background-color: #3182ce;
        color: #ffffff;
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
        padding: 12px;
    }
"""

def create_date_field(default_date_str=""):
    date_edit = QDateEdit()
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
        self.setWindowTitle("SmartReport — Учет смен и отчеты")
        self.resize(1240, 880)
        self.setStyleSheet(DARK_THEME_QSS)

        init_db_once()
        self.init_ui()

        self.auto_sync_timer = QTimer(self)
        self.auto_sync_timer.timeout.connect(self.background_sync_with_cloud)
        self._sync_worker = None
        self._manual_sync_requested = False
        self.auto_sync_timer.start(2000)

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)

        self.tabs = QTabWidget()
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
        self.tabs.addTab(self.tab_expenses, "🏢 Расходы и налоги")
        self.tabs.addTab(self.tab_object_expenses, "Материалы и расходы по объектам")
        self.tabs.addTab(self.tab_invoice, "📄 Счёт для бухгалтера")
        self.tabs.addTab(self.tab_dagbok, "📖 Журнал работ")
        self.tabs.addTab(self.tab_sprav, "📚 Справочники")

        self.setup_hours_tab()
        self.setup_salary_tab()
        self.setup_balance_tab()
        self.setup_expenses_tab()
        self.setup_object_expenses_tab()
        self.setup_invoice_tab()
        self.setup_dagbok_tab()
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
        self.load_dropdowns()
        if index == 1:
            self.load_salary_table_template()
        elif index == 2:
            self.load_balance_table()
        elif index == 3:
            self.load_expenses_table()
        elif index == 4:
            self.load_object_expense_overview()
        elif index == 7:
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
        for record_type in (RECORD_TYPE_WORK, RECORD_TYPE_OB, RECORD_TYPE_ATF, RECORD_TYPE_SICK):
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
        self._standard_hour_widgets = (self.hours_input, self.rate_input, self.trans_input)
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
        control_card.setStyleSheet("background-color: #242933; border: 2px solid #3182ce; border-radius: 8px; margin-top: 5px;")
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
        self.table_shifts.setColumnCount(13)
        self.table_shifts.setHorizontalHeaderLabels(["ID", "Дата", "Сотрудник", "Фирма", "Объект", "Тип", "Часы", "Детализация / OB", "Факт. ставка", "Транспорт", "Комментарий", "Статус фактуры", "OB сумма (SEK)"])
        self.table_shifts.setColumnHidden(0, True)
        shifts_header = self.table_shifts.horizontalHeader()
        shifts_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in {
            1: 95, 2: 130, 3: 110, 4: 170, 5: 85, 6: 65,
            7: 175, 8: 80, 9: 210, 11: 110, 12: 95,
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

        cursor.execute("SELECT 1 FROM shift_sync_outbox WHERE sync_id = ? AND state = 'pending' LIMIT 1", (sync_id,))
        has_pending = cursor.fetchone() is not None
        remote_version = int(shift.get('version', 1))
        if local and has_pending:
            if remote_version > local['sync_version']:
                cursor.execute("UPDATE shift_sync_outbox SET state = 'conflict' WHERE sync_id = ? AND state = 'pending'", (sync_id,))
            else:
                return

        record_type = shift.get('record_type') or shift.get('submission_type') or RECORD_TYPE_WORK
        if record_type not in (RECORD_TYPE_WORK, RECORD_TYPE_ATF, RECORD_TYPE_SICK, RECORD_TYPE_OB):
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
        stored_transport = float(shift.get('transport', 0.0) or 0.0) if record_type == RECORD_TYPE_WORK else 0.0
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
            if segments:
                replace_ob_segments(cursor, sync_id, segments)
            elif shift.get("start_time") and shift.get("end_time"):
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

    def perform_sync_logic(self):
        headers = {"X-Sync-Key": SYNC_API_KEY}
        downloaded_ids = []
        typed_sync_supported = False
        ob_sync_supported = False
        schema_conn = get_db_connection()
        local_ob_enabled = ob_schema_ready(schema_conn.cursor())
        schema_conn.close()
        try:
            capabilities = requests.get(f"{CLOUD_URL}/sync/capabilities", headers=headers, timeout=4)
            if getattr(capabilities, 'status_code', None) == 200:
                capability_data = capabilities.json()
                typed_sync_supported = (
                    {RECORD_TYPE_WORK, RECORD_TYPE_ATF, RECORD_TYPE_SICK}.issubset(
                        set(capability_data.get('submission_types', []))
                    ) and capability_data.get('typed_tombstones') is True
                )
                ob_sync_supported = (
                    "ob" in set(capability_data.get('submission_types', []))
                    and capability_data.get('typed_tombstones') is True
                )
        except Exception:
            typed_sync_supported = False
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
        if typed_sync_supported or ob_sync_supported:
            allowed_types = set()
            if typed_sync_supported:
                allowed_types.update((RECORD_TYPE_WORK, RECORD_TYPE_ATF, RECORD_TYPE_SICK))
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
        if changes:
            pushed = requests.post(
                f"{CLOUD_URL}/sync/push", headers=headers, json={"changes": changes}, timeout=4
            )
            pushed.raise_for_status()
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
                        cursor.execute("UPDATE shift_sync_outbox SET state = 'conflict' WHERE operation_id = ?", (change['operation_id'],))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

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
        cursor.execute("SELECT name FROM companies ORDER BY name")
        companies = [row[0] for row in cursor.fetchall()]
        cursor.execute("SELECT name, markning, company, rate, transport_rate FROM objects ORDER BY name")
        objects = [{"name": row[0], "markning": row[1], "company": row[2], "rate": row[3], "transport_rate": row[4]} for row in cursor.fetchall()]
        conn.close()
        metadata_payload = {"employees": employees, "companies": companies, "objects": objects, "shifts": []}
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
        metadata_response.raise_for_status()
        return conflicts

    def _on_sync_completed(self, success, conflicts, error):
        self._sync_worker = None
        if success:
            self.load_shifts_history()
        if self._manual_sync_requested:
            self._manual_sync_requested = False
            if success and not conflicts:
                QMessageBox.information(self, "Синхронизация", "Синхронизация завершена.")
            elif success:
                QMessageBox.warning(self, "Конфликт синхронизации", f"Есть конфликтующих изменений: {conflicts}. Несохранённые данные оставлены в локальной очереди.")
            else:
                QMessageBox.warning(self, "Нет связи", f"Изменения сохранены локально и будут отправлены после восстановления связи.\n{error}")

    def sync_with_cloud(self):
        self._manual_sync_requested = True
        self.background_sync_with_cloud()

    def background_sync_with_cloud(self):
        if self._sync_worker and self._sync_worker.isRunning():
            return
        self._sync_worker = SyncWorker(self)
        self._sync_worker.completed.connect(self._on_sync_completed)
        self._sync_worker.start()

    def setup_salary_tab(self):
        layout = QVBoxLayout(self.tab_salary)
        total_card = QFrame()
        total_card.setObjectName("card")
        total_card.setStyleSheet("background-color: #242933; border: 2px solid #4299e1; border-radius: 8px; margin-bottom: 5px;")
        t_layout = QHBoxLayout(total_card)
        t_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        
        self.sal_summary_hours_lbl = QLabel("<b>Рабочие часы: 0.0</b>")
        self.sal_summary_ob_lbl = QLabel("<b>OB: 0 tim / 0.00 SEK</b>")
        self.sal_summary_net_lbl = QLabel("<b>Nettolön: 0.00 SEK</b>")
        self.sal_summary_employer_lbl = QLabel("<b>Arbetskostnad: 0.00 SEK</b>")

        t_layout.addWidget(self.sal_summary_hours_lbl)
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
        self.table_salary.setColumnCount(14)
        self.table_salary.setHorizontalHeaderLabels([
            "Anställd", "Arbete\ntim", "ATF\ntim", "Sjukfrånvaro\ntim", "Timlön\n(SEK)",
            "OB timmar /\nspecifikation", "Grundlön + OB\n(SEK)", "Semesterersättning\n(SEK)",
            "ATF/KLS\n(SEK)", "Sjuklön\n(SEK)", "Skatt\n(SEK)", "Nettolön\n(SEK)",
            "Arbetsgivaravgifter\n(SEK)", "Total kostnad\n(SEK)",
        ])
        salary_header = self.table_salary.horizontalHeader()
        salary_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        salary_header.setMinimumHeight(46)
        for column, width in {
            0: 140, 1: 75, 2: 75, 3: 75, 4: 85, 5: 200,
            6: 110, 7: 120, 8: 90, 9: 90, 10: 95, 11: 100,
            12: 120, 13: 110,
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
        minimum_widths = {
            0: 140, 1: 75, 2: 75, 3: 75, 4: 85, 5: 200,
            6: 110, 7: 120, 8: 90, 9: 90, 10: 95, 11: 100,
            12: 120, 13: 110,
        }
        available_width = self.table_salary.viewport().width()
        extra_width = max(0, available_width - sum(minimum_widths.values()))
        widths = dict(minimum_widths)
        if extra_width:
            name_extra = extra_width // 3
            widths[0] += name_extra
            finance_extra = extra_width - name_extra
            finance_columns = (6, 7, 8, 9, 10, 11, 12, 13)
            finance_weight = sum(minimum_widths[column] for column in finance_columns)
            distributed = 0
            for column in finance_columns[:-1]:
                addition = finance_extra * minimum_widths[column] // finance_weight
                widths[column] += addition
                distributed += addition
            widths[finance_columns[-1]] += finance_extra - distributed
        for column, width in widths.items():
            self.table_salary.setColumnWidth(column, width)

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
                    SUM(CASE WHEN entry_type = 'ob' THEN hours ELSE 0 END), 0
                FROM shifts
                WHERE date >= ? AND date <= ?
                GROUP BY employee
            """, (d_from, d_to))
        shift_rows = cursor.fetchall()
        conn.close()

        sorted_keys = sorted(list(rates_map.keys()))
        hours_map = {str(row[0]).strip().lower(): (row[1], row[2], row[3], row[4], row[5]) for row in shift_rows}

        self.table_salary.setRowCount(len(sorted_keys))
        sum_hrs_all = 0.0
        sum_atf_all = 0.0
        sum_sick_all = 0.0
        sum_ob_hours_all = 0.0
        sum_ob_amount_all = 0.0
        sum_net_all = 0.0
        sum_employer_cost_all = 0.0

        for row_idx, key in enumerate(sorted_keys):
            emp_display_name = orig_names.get(key, key.title())
            hrs, atf_hours, sick_hours, ob_hours, ob_amount = hours_map.get(key, (0.0, 0.0, 0.0, 0.0, 0.0))
            salary_rate = rates_map.get(key, 0.0)
            sum_hrs_all += hrs
            sum_atf_all += atf_hours
            sum_sick_all += sick_hours
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

            payroll_values = (
                payroll["base_salary"], payroll["vacation_pay"], payroll["atf_pay"],
                payroll["sick_pay"], payroll["tax"], payroll["net_pay"],
                payroll["employer_fee"], payroll["employer_total_cost"],
            )
            for col_i, val in enumerate(payroll_values, start=6):
                item_res = QTableWidgetItem(f"{val:.2f}")
                item_res.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                item_res.setFlags(item_res.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table_salary.setItem(row_idx, col_i, item_res)

        self.sal_summary_hours_lbl.setText(f"<b>Рабочие часы: {sum_hrs_all:.1f}</b>")
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
        for c_idx, val in enumerate(row_values, start=6):
            it = QTableWidgetItem(f"{val:.2f}")
            it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table_salary.setItem(row_idx, c_idx, it)

        self.table_salary.cellChanged.connect(self.on_salary_cell_changed)
        
        sum_hrs = sum_atf = sum_sick = sum_ob_hrs = sum_ob_amount = 0.0
        sum_net = sum_employer_total = 0.0
        for r in range(self.table_salary.rowCount()):
            h_it = self.table_salary.item(r, 1)
            atf_it = self.table_salary.item(r, 2)
            sick_it = self.table_salary.item(r, 3)
            ob_it = self.table_salary.item(r, 5)
            n_it = self.table_salary.item(r, 11)
            employer_it = self.table_salary.item(r, 13)
            if h_it: sum_hrs += parse_float(h_it.text(), 0.0)
            if atf_it: sum_atf += parse_float(atf_it.text(), 0.0)
            if sick_it: sum_sick += parse_float(sick_it.text(), 0.0)
            if n_it: sum_net += parse_float(n_it.text(), 0.0)
            if employer_it: sum_employer_total += parse_float(employer_it.text(), 0.0)
            if ob_it:
                sum_ob_hrs += float(ob_it.data(Qt.ItemDataRole.UserRole) or 0.0)
                sum_ob_amount += float(ob_it.data(Qt.ItemDataRole.UserRole + 1) or 0.0)
            
        self.sal_summary_hours_lbl.setText(f"<b>Рабочие часы: {sum_hrs:.1f}</b>")
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
                "OB timmar / specifikation", "Grundlön + OB (SEK)",
                "Semesterersättning (SEK)", "ATF/KLS (SEK)", "Sjuklön (SEK)",
                "Skatt (SEK)", "Nettolön (SEK)", "Arbetsgivaravgifter (SEK)",
                "Total kostnad (SEK)",
            ]
            table_data = [[Paragraph(escape(label), header_style) for label in labels]]
            totals = [0.0] * 14
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
                        if column in (1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 13):
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
            totals_row = ["Totalt:"] + [f"{totals[column]:.2f}" for column in range(1, 14)]
            totals_row[4] = ""
            totals_row[5] = f"OB totalt: {total_ob_hours:g} tim / {total_ob_amount:,.2f} SEK"
            table_data.append([
                Paragraph(escape(str(value)), cell_style) if index in (0, 5) else value
                for index, value in enumerate(totals_row)
            ])

            table = LongTable(
                table_data,
                colWidths=[75, 32, 24, 35, 40, 218, 50, 46, 40, 40, 40, 44, 54, 54],
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
        total_card.setStyleSheet("background-color: #242933; border: 2px solid #4299e1; border-radius: 10px;")
        
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

    def setup_expenses_tab(self):
        layout = QVBoxLayout(self.tab_expenses)
        self._updating_expense_dates = False
        today = QDate.currentDate()
        first_of_month = QDate(today.year(), today.month(), 1)
        last_of_month = first_of_month.addMonths(1).addDays(-1)
        period_card = QFrame()
        period_card.setObjectName("card")
        period_layout = QHBoxLayout(period_card)
        period_layout.addWidget(QLabel("<b>Период:</b>"))
        self.exp_date_from = create_date_field(first_of_month.toString("yyyy-MM-dd"))
        self.exp_date_to = create_date_field(last_of_month.toString("yyyy-MM-dd"))
        self.exp_period_preset = QComboBox()
        for label, key in (
            ("Произвольный", "custom"), ("Этот месяц", "this_month"),
            ("Прошлый месяц", "last_month"), ("Последние 30 дней", "last_30"),
            ("Этот год", "this_year"),
        ):
            self.exp_period_preset.addItem(label, key)
        self.exp_period_preset.setCurrentIndex(1)
        period_layout.addWidget(self.exp_date_from)
        period_layout.addWidget(QLabel("по"))
        period_layout.addWidget(self.exp_date_to)
        self.exp_period_basis = QComboBox()
        self.exp_period_basis.addItem("Дата документа", "document_date")
        self.exp_period_basis.addItem("Срок оплаты", "due_date")
        period_layout.addWidget(QLabel("Фильтр по:"))
        period_layout.addWidget(self.exp_period_basis)
        period_layout.addWidget(QLabel("Быстрый период:"))
        period_layout.addWidget(self.exp_period_preset)
        period_layout.addStretch()
        layout.addWidget(period_card)
        self.exp_period_label = QLabel("")
        self.exp_period_label.setStyleSheet("font-weight: bold; color: #a0aec0; border: none;")
        layout.addWidget(self.exp_period_label)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("Объект:"))
        self.exp_filter_object = QComboBox()
        self.exp_filter_object.addItem("Все объекты", None)
        filters.addWidget(self.exp_filter_object)
        filters.addWidget(QLabel("Поставщик:"))
        self.exp_filter_supplier = QComboBox()
        self.exp_filter_supplier.addItem("Все поставщики", None)
        filters.addWidget(self.exp_filter_supplier, 1)
        layout.addLayout(filters)

        self.exp_document_tabs = QTabWidget()
        self.exp_documents_page = QWidget()
        document_layout = QVBoxLayout(self.exp_documents_page)
        document_summary = QHBoxLayout()
        for attribute, title in (
            ("exp_docs_net_lbl", "Подтверждено без НДС"),
            ("exp_docs_vat_lbl", "НДС в документах (не к вычету)"),
            ("exp_docs_gross_lbl", "Подтверждённый итог"),
            ("exp_docs_unpaid_lbl", "Известный неоплаченный остаток"),
            ("exp_docs_review_lbl", "Требуют проверки"),
        ):
            card = QFrame()
            card.setObjectName("card")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(8, 7, 8, 7)
            card_layout.addWidget(QLabel(title))
            value = QLabel("—")
            value.setWordWrap(True)
            value.setStyleSheet("font-size: 14px; font-weight: bold; color: #63b3ed; border: none;")
            setattr(self, attribute, value)
            card_layout.addWidget(value)
            document_summary.addWidget(card, 1)
        document_layout.addLayout(document_summary)
        document_note = QLabel(
            "Черновики не входят в подтверждённые итоги. Валюты не суммируются между собой. "
            "НДС из документа не означает право на вычет; это не налоговая декларация."
        )
        document_note.setWordWrap(True)
        document_layout.addWidget(document_note)
        self.exp_docs_warning_lbl = QLabel("")
        self.exp_docs_warning_lbl.setWordWrap(True)
        self.exp_docs_warning_lbl.setStyleSheet("background: #4a3b18; color: #f6e05e; padding: 8px; border-radius: 4px;")
        self.exp_docs_warning_lbl.hide()
        document_layout.addWidget(self.exp_docs_warning_lbl)

        document_actions = QHBoxLayout()
        upload_button = QPushButton("Загрузить документ")
        upload_button.clicked.connect(self.upload_expense_document)
        manual_document_button = QPushButton("Добавить вручную")
        manual_document_button.clicked.connect(self.open_manual_document_dialog)
        check_document_button = QPushButton("Проверить")
        check_document_button.clicked.connect(self.review_selected_document)
        open_document_button = QPushButton("Открыть оригинал")
        open_document_button.clicked.connect(self.open_selected_document_original)
        payment_button = QPushButton("Отметить оплату")
        payment_button.clicked.connect(self.record_selected_document_payment)
        for button in (upload_button, manual_document_button, check_document_button, open_document_button, payment_button):
            document_actions.addWidget(button)
        document_layout.addLayout(document_actions)
        source_note = QLabel(
            "E-post и Kivra не подключены: официальная авторизация отсутствует. "
            "Скачанный оттуда PDF/скан можно загрузить вручную; распознавание выполняется локально."
        )
        source_note.setWordWrap(True)
        document_layout.addWidget(source_note)

        self.table_supplier_documents = QTableWidget()
        self.table_supplier_documents.setColumnCount(11)
        self.table_supplier_documents.setHorizontalHeaderLabels([
            "ID", "Fakturadatum", "Поставщик", "Номер документа", "Объект",
            "Netto", "Moms", "Итого", "Förfallodatum", "Проверка", "Оплата",
        ])
        self.table_supplier_documents.setColumnHidden(0, True)
        doc_header = self.table_supplier_documents.horizontalHeader()
        doc_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in {1: 105, 2: 170, 3: 140, 4: 170, 5: 100, 6: 95, 7: 100, 8: 110, 9: 145, 10: 125}.items():
            doc_header.resizeSection(column, width)
        doc_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table_supplier_documents.setAlternatingRowColors(True)
        self.table_supplier_documents.cellDoubleClicked.connect(self.review_selected_document)
        document_layout.addWidget(self.table_supplier_documents, 1)
        self.exp_document_tabs.addTab(self.exp_documents_page, "Leverantörsfakturor & Kvitton")

        self.exp_legacy_page = QWidget()
        legacy_layout = QVBoxLayout(self.exp_legacy_page)
        legacy_note = QLabel(
            "Это отдельный старый журнал ручных расходов и условных payroll-оценок. "
            "Он не складывается со счетами и чеками выше. Ставки и формулы не подтверждены как обязательства; OB исключён."
        )
        legacy_note.setWordWrap(True)
        legacy_layout.addWidget(legacy_note)
        legacy_summary = QHBoxLayout()
        for attribute, title in (
            ("exp_summary_expenses_lbl", "Старые ручные расходы"),
            ("exp_summary_fora_lbl", "Оценка FORA"),
            ("exp_summary_employer_lbl", "Оценка работодателя"),
            ("exp_summary_review_lbl", "Требуют проверки"),
        ):
            card = QFrame()
            card.setObjectName("card")
            card_layout = QVBoxLayout(card)
            card_layout.addWidget(QLabel(title))
            value = QLabel("0.00 SEK")
            setattr(self, attribute, value)
            card_layout.addWidget(value)
            legacy_summary.addWidget(card, 1)
        legacy_layout.addLayout(legacy_summary)
        self.exp_warning_lbl = QLabel("")
        self.exp_warning_lbl.setWordWrap(True)
        self.exp_warning_lbl.hide()
        legacy_layout.addWidget(self.exp_warning_lbl)
        legacy_actions = QHBoxLayout()
        add_exp_btn = QPushButton("Добавить расход")
        add_exp_btn.clicked.connect(self.open_add_expense_dialog)
        auto_calc_btn = QPushButton("Предварительно рассчитать payroll-оценку")
        auto_calc_btn.clicked.connect(self.auto_calculate_taxes_and_fora)
        legacy_actions.addWidget(add_exp_btn)
        legacy_actions.addWidget(auto_calc_btn)
        legacy_actions.addStretch()
        legacy_layout.addLayout(legacy_actions)
        self.exp_filter_category = QComboBox()
        self.exp_filter_category.addItem("Все категории")
        self.exp_filter_category.currentIndexChanged.connect(self.apply_expense_journal_filters)
        self.exp_filter_search = QLineEdit()
        self.exp_filter_search.setPlaceholderText("Поиск по описанию или категории")
        self.exp_filter_search.textChanged.connect(self.apply_expense_journal_filters)
        legacy_filters = QHBoxLayout()
        legacy_filters.addWidget(self.exp_filter_category)
        legacy_filters.addWidget(self.exp_filter_search, 1)
        legacy_layout.addLayout(legacy_filters)
        self.table_expenses = QTableWidget()
        self.table_expenses.setColumnCount(6)
        self.table_expenses.setHorizontalHeaderLabels(["ID", "Дата", "Категория", "Описание", "Сумма (SEK)", "Статус"])
        self.table_expenses.setColumnHidden(0, True)
        self.table_expenses.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table_expenses.cellDoubleClicked.connect(self.show_expense_details)
        legacy_layout.addWidget(self.table_expenses, 1)
        legacy_actions = QHBoxLayout()
        details_button = QPushButton("Подробности")
        details_button.clicked.connect(self.show_expense_details)
        edit_button = QPushButton("Изменить по ID")
        edit_button.clicked.connect(self.edit_company_expense)
        delete_button = QPushButton("Удалить по ID")
        delete_button.setObjectName("danger")
        delete_button.clicked.connect(self.delete_company_expense)
        for button in (details_button, edit_button, delete_button):
            legacy_actions.addWidget(button)
        legacy_layout.addLayout(legacy_actions)
        self.exp_document_tabs.addTab(self.exp_legacy_page, "Ручные расходы и payroll-оценки")
        layout.addWidget(self.exp_document_tabs, 1)

        self.exp_date_from.dateChanged.connect(self.on_expense_date_changed)
        self.exp_date_to.dateChanged.connect(self.on_expense_date_changed)
        self.exp_period_preset.currentIndexChanged.connect(self.apply_expense_period_preset)
        self.exp_period_basis.currentIndexChanged.connect(self.load_expense_documents)
        self.exp_filter_object.currentIndexChanged.connect(self.load_expense_documents)
        self.exp_filter_supplier.currentIndexChanged.connect(self.load_expense_documents)
        self.exp_document_tabs.currentChanged.connect(lambda _: (self.load_expense_documents(), self.load_expenses_table()))
        self.load_expense_document_filters()
        self.load_expense_documents()
        self.load_expenses_table()

    def apply_expense_period_preset(self, *_):
        if self._updating_expense_dates:
            return
        preset = self.exp_period_preset.currentData()
        today = QDate.currentDate()
        if preset == "custom":
            self.load_expenses_table()
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
        self.load_expenses_table()

    def on_expense_date_changed(self, *_):
        if not self._updating_expense_dates:
            custom_index = self.exp_period_preset.findData("custom")
            if custom_index >= 0 and self.exp_period_preset.currentIndex() != custom_index:
                self._updating_expense_dates = True
                self.exp_period_preset.setCurrentIndex(custom_index)
                self._updating_expense_dates = False
            self.load_expenses_table()

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

    def _format_currency_totals(self, totals):
        if not totals:
            return "0.00"
        return " | ".join(
            f"{currency} {amount:,.2f}"
            for currency, amount in sorted(totals.items())
        )

    def load_expense_document_filters(self, *_):
        object_selected = self.exp_filter_object.currentData() if hasattr(self, "exp_filter_object") else None
        supplier_selected = self.exp_filter_supplier.currentData() if hasattr(self, "exp_filter_supplier") else None
        conn = get_db_connection()
        objects = conn.execute("SELECT id, name, markning FROM objects ORDER BY name").fetchall()
        suppliers = conn.execute("SELECT id, name FROM expense_suppliers ORDER BY name COLLATE NOCASE").fetchall()
        conn.close()

        self.exp_filter_object.blockSignals(True)
        self.exp_filter_object.clear()
        self.exp_filter_object.addItem("Все объекты", None)
        for row in objects:
            label = str(row["name"])
            if row["markning"]:
                label += f" ({row['markning']})"
            self.exp_filter_object.addItem(label, int(row["id"]))
        object_index = self.exp_filter_object.findData(object_selected)
        self.exp_filter_object.setCurrentIndex(max(0, object_index))
        self.exp_filter_object.blockSignals(False)

        self.exp_filter_supplier.blockSignals(True)
        self.exp_filter_supplier.clear()
        self.exp_filter_supplier.addItem("Все поставщики", None)
        for row in suppliers:
            self.exp_filter_supplier.addItem(str(row["name"]), int(row["id"]))
        supplier_index = self.exp_filter_supplier.findData(supplier_selected)
        self.exp_filter_supplier.setCurrentIndex(max(0, supplier_index))
        self.exp_filter_supplier.blockSignals(False)

    def load_expense_documents(self, *_):
        if not hasattr(self, "table_supplier_documents"):
            return
        date_from = self.exp_date_from.text().strip()
        date_to = self.exp_date_to.text().strip()
        basis = self.exp_period_basis.currentData()
        basis_label = "дату документа" if basis == "document_date" else "срок оплаты"
        self.exp_period_label.setText(
            f"Документы за {date_from} — {date_to}; фильтр по {basis_label}"
        )
        if date_from > date_to:
            for label in (self.exp_docs_net_lbl, self.exp_docs_vat_lbl, self.exp_docs_gross_lbl, self.exp_docs_unpaid_lbl):
                label.setText("0.00")
            self.exp_docs_review_lbl.setText("0")
            self.table_supplier_documents.setRowCount(0)
            return

        filters = [f"d.{basis} >= ?", f"d.{basis} <= ?"]
        params: list[Any] = [date_from, date_to]
        object_id = self.exp_filter_object.currentData()
        supplier_id = self.exp_filter_supplier.currentData()
        if object_id is not None:
            filters.append("EXISTS (SELECT 1 FROM supplier_document_allocations a WHERE a.document_id = d.id AND a.object_id = ?)")
            params.append(int(object_id))
        if supplier_id is not None:
            filters.append("d.supplier_id = ?")
            params.append(int(supplier_id))

        conn = get_db_connection()
        rows = conn.execute(f"""
            SELECT d.*, COALESCE(s.name, d.supplier_name, '') AS display_supplier
            FROM supplier_documents d
            LEFT JOIN expense_suppliers s ON s.id = d.supplier_id
            WHERE {' AND '.join(filters)}
            ORDER BY d.{basis} DESC, d.id DESC
        """, params).fetchall()
        objects_by_document = {}
        for row in rows:
            objects_by_document[row["id"]] = conn.execute("""
                SELECT DISTINCT o.name
                FROM supplier_document_allocations a
                JOIN objects o ON o.id = a.object_id
                WHERE a.document_id = ? ORDER BY o.name
            """, (row["id"],)).fetchall()
        payments_by_document: dict[int, Decimal] = {}
        for payment in conn.execute("SELECT document_id, amount FROM supplier_document_payments").fetchall():
            document_id = int(payment["document_id"])
            payments_by_document[document_id] = payments_by_document.get(document_id, Decimal(0)) + (
                parse_decimal(payment["amount"]) or Decimal(0)
            )
        undated = conn.execute(
            f"SELECT COUNT(*) FROM supplier_documents d WHERE d.{basis} IS NULL"
        ).fetchone()[0]
        conn.close()

        net_totals: dict[str, Decimal] = {}
        vat_totals: dict[str, Decimal] = {}
        gross_totals: dict[str, Decimal] = {}
        unpaid_totals: dict[str, Decimal] = {}
        review_count = 0
        for row in rows:
            currency = str(row["currency_code"] or "UNKNOWN")
            if row["verification_status"] != "confirmed":
                review_count += 1
                continue
            for field, totals in (("net_amount", net_totals), ("vat_amount", vat_totals), ("gross_amount", gross_totals)):
                amount = parse_decimal(row[field])
                if amount is not None:
                    totals[currency] = totals.get(currency, Decimal(0)) + amount
            if row["payment_status"] in ("unpaid", "partially_paid"):
                due = parse_decimal(row["amount_due"]) or parse_decimal(row["gross_amount"])
                paid = payments_by_document.get(int(row["id"]), Decimal(0))
                outstanding = max(Decimal(0), (due or Decimal(0)) - paid)
                unpaid_totals[currency] = unpaid_totals.get(currency, Decimal(0)) + outstanding

        self.exp_docs_net_lbl.setText(self._format_currency_totals(net_totals))
        self.exp_docs_vat_lbl.setText(self._format_currency_totals(vat_totals))
        self.exp_docs_gross_lbl.setText(self._format_currency_totals(gross_totals))
        self.exp_docs_unpaid_lbl.setText(self._format_currency_totals(unpaid_totals))
        self.exp_docs_review_lbl.setText(str(review_count))

        self.table_supplier_documents.setRowCount(len(rows))
        today = QDate.currentDate().toString("yyyy-MM-dd")
        verification_labels = {
            "draft": "Черновик", "requires_review": "Требует проверки", "confirmed": "Подтверждён",
        }
        payment_labels = {
            "unknown": "Неизвестно", "unpaid": "Не оплачен",
            "partially_paid": "Частично оплачен", "paid": "Оплачен",
        }
        for row_index, row in enumerate(rows):
            object_names = [str(item[0]) for item in objects_by_document[row["id"]]]
            object_label = ", ".join(object_names) if object_names else "Без объекта"
            payment_label = payment_labels.get(str(row["payment_status"]), str(row["payment_status"]))
            overdue = bool(row["due_date"] and row["due_date"] < today and row["payment_status"] in ("unpaid", "partially_paid"))
            if overdue:
                payment_label = f"Просрочен: {payment_label.casefold()}"
            values = (
                row["id"], row["document_date"] or "—", row["display_supplier"] or "Не указан",
                row["invoice_number"] or "—", object_label,
                f"{row['net_amount']} {row['currency_code']}" if row["net_amount"] else "—",
                f"{row['vat_amount']} {row['currency_code']}" if row["vat_amount"] else "—",
                f"{row['gross_amount']} {row['currency_code']}" if row["gross_amount"] else "—",
                row["due_date"] or "—", verification_labels.get(str(row["verification_status"]), str(row["verification_status"])), payment_label,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column in (2, 3, 4, 9, 10):
                    item.setToolTip(str(value))
                self.table_supplier_documents.setItem(row_index, column, item)
            if overdue:
                for column in range(self.table_supplier_documents.columnCount()):
                    self.table_supplier_documents.item(row_index, column).setForeground(QColor("#fc8181"))

        if undated:
            self.exp_docs_warning_lbl.setText(
                f"{undated} документ(ов) без даты {basis_label} не включено в выбранный период. "
                "Откройте карточку и заполните дату."
            )
            self.exp_docs_warning_lbl.show()
        else:
            self.exp_docs_warning_lbl.clear()
            self.exp_docs_warning_lbl.hide()

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

    def upload_expense_document(self, *_):
        path, _ = QFileDialog.getOpenFileName(
            self, "Загрузить счёт или чек", "",
            "Документы (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp)",
        )
        if not path:
            return
        try:
            extraction = extract_local_document(path)
            fields = parse_document_text(extraction.text)
        except Exception as error:
            QMessageBox.warning(self, "Документ не прочитан", str(error))
            return
        fields["raw_text"] = extraction.text
        fields["expense_category"] = "Прочие накладные расходы"
        fields["ocr_method"] = extraction.method
        fields["ocr_confidence"] = extraction.confidence
        if extraction.confidence is not None and extraction.confidence < 0.65:
            fields["review_reasons"].append("низкая уверенность OCR; проверьте все поля")
        suggestions = match_objects(extraction.text, self._expense_object_candidates())
        self._expense_document_review_dialog(None, path, fields, suggestions, None)

    def open_manual_document_dialog(self, *_):
        if self.exp_date_from.date() > self.exp_date_to.date():
            QMessageBox.warning(self, "Расход", "Сначала выберите корректный период.")
            return
        fields = {
            "supplier_name": None, "supplier_org_number": None, "supplier_vat_number": None,
            "document_type": "manual", "document_date": QDate.currentDate().toString("yyyy-MM-dd"),
            "due_date": None, "invoice_number": None, "payment_reference": None,
            "currency_code": "UNKNOWN", "net_amount": None, "vat_amount": None,
            "gross_amount": None, "amount_due": None,
            "expense_category": "Прочие накладные расходы", "document_comment": "", "reverse_charge": False,
            "vat_lines": [], "review_reasons": ["ручная запись без оригинала; укажите валюту и суммы документа"],
            "raw_text": "", "payment_status": "unknown",
        }
        self._expense_document_review_dialog(None, None, fields, [], None)

    def review_selected_document(self, *_):
        row = self.table_supplier_documents.currentRow()
        id_item = self.table_supplier_documents.item(row, 0) if row >= 0 else None
        if id_item is None:
            QMessageBox.information(self, "Документ", "Выберите документ в реестре.")
            return
        document_id = int(id_item.text())
        conn = get_db_connection()
        document = conn.execute("SELECT * FROM supplier_documents WHERE id = ?", (document_id,)).fetchone()
        if document is None:
            conn.close()
            QMessageBox.warning(self, "Документ", "Запись не найдена. Обновите реестр.")
            self.load_expense_documents()
            return
        vat_lines = conn.execute(
            "SELECT vat_rate AS rate, taxable_base, vat_amount AS amount, note FROM supplier_document_vat_lines WHERE document_id = ? ORDER BY id",
            (document_id,),
        ).fetchall()
        allocations = conn.execute(
            "SELECT object_id, allocated_net AS net_amount, allocated_vat AS vat_amount, allocated_gross AS gross_amount, allocation_note AS note FROM supplier_document_allocations WHERE document_id = ? ORDER BY id",
            (document_id,),
        ).fetchall()
        conn.close()
        fields = dict(document)
        fields["supplier_name"] = document["supplier_name"] or ""
        fields["supplier_org_number"] = document["supplier_org_number"] or ""
        fields["supplier_vat_number"] = document["supplier_vat_number"] or ""
        fields["document_comment"] = document["document_comment"] or ""
        fields["vat_lines"] = [dict(item) for item in vat_lines]
        fields["review_reasons"] = [item for item in str(document["review_reasons"] or "").splitlines() if item]
        fields["allocations"] = [dict(item) for item in allocations]
        suggestions = match_objects(document["recognized_text"] or "", self._expense_object_candidates())
        self._expense_document_review_dialog(
            document_id, document["stored_path"], fields, suggestions, fields["allocations"]
        )

    def _expense_document_review_dialog(self, document_id, source_path, fields, suggestions, existing_allocations):
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
        splitter.addWidget(preview_tabs)

        form_widget = QWidget()
        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setWidget(form_widget)
        form = QFormLayout(form_widget)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        conn = get_db_connection()
        known_suppliers = conn.execute("SELECT name FROM expense_suppliers ORDER BY name COLLATE NOCASE").fetchall()
        object_rows = conn.execute("SELECT id, name, markning FROM objects ORDER BY name").fetchall()
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
            for object_row in object_rows:
                title = str(object_row["name"])
                if object_row["markning"]:
                    title += f" ({object_row['markning']})"
                object_combo.addItem(title, int(object_row["id"]))
            selected_object = allocation.get("object_id")
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
        cancel = QPushButton("Отмена")
        for button in (save_draft, save_review, confirm, cancel):
            action_row.addWidget(button)
        root_layout.addLayout(action_row)

        def save_document(status):
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
                    saved_id = self._update_supplier_document_record(
                        conn, document_id, source_path, values, allocation_rows, status, allow_duplicate
                    )
                conn.close()
            except Exception as error:
                if "conn" in locals():
                    conn.close()
                QMessageBox.warning(dialog, "Документ не сохранён", str(error))
                return
            dialog.accept()
            self.load_expense_document_filters()
            self.load_expense_documents()
            QMessageBox.information(self, "Документ", f"Запись #{saved_id} сохранена со статусом: {status}.")

        save_draft.clicked.connect(lambda: save_document("draft"))
        save_review.clicked.connect(lambda: save_document("requires_review"))
        confirm.clicked.connect(lambda: save_document("confirmed"))
        cancel.clicked.connect(dialog.reject)
        dialog.exec()

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

    def open_selected_document_original(self, *_):
        row = self.table_supplier_documents.currentRow()
        id_item = self.table_supplier_documents.item(row, 0) if row >= 0 else None
        if id_item is None:
            QMessageBox.information(self, "Документ", "Выберите документ.")
            return
        conn = get_db_connection()
        document = conn.execute("SELECT stored_path FROM supplier_documents WHERE id=?", (int(id_item.text()),)).fetchone()
        conn.close()
        if not document or not document["stored_path"] or not Path(document["stored_path"]).is_file():
            QMessageBox.warning(self, "Оригинал", "Оригинал отсутствует или путь недоступен.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(document["stored_path"])))

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

    def setup_object_expenses_tab(self):
        layout = QVBoxLayout(self.tab_object_expenses)
        selection = QHBoxLayout()
        selection.addWidget(QLabel("Объект проекта:"))
        self.object_expenses_object = QComboBox()
        self.object_expenses_object.setMinimumWidth(320)
        selection.addWidget(self.object_expenses_object, 1)
        self.object_expenses_period_enabled = QCheckBox("Показать также итог за период")
        selection.addWidget(self.object_expenses_period_enabled)
        layout.addLayout(selection)

        self.object_expenses_period_controls = QFrame()
        period_layout = QHBoxLayout(self.object_expenses_period_controls)
        self.object_expenses_date_from = create_date_field(QDate.currentDate().toString("yyyy-MM-dd"))
        self.object_expenses_date_to = create_date_field(QDate.currentDate().toString("yyyy-MM-dd"))
        period_layout.addWidget(QLabel("Период по дате документа:"))
        period_layout.addWidget(self.object_expenses_date_from)
        period_layout.addWidget(QLabel("по"))
        period_layout.addWidget(self.object_expenses_date_to)
        period_layout.addStretch()
        self.object_expenses_period_controls.setVisible(False)
        layout.addWidget(self.object_expenses_period_controls)

        self.object_expenses_selected_label = QLabel("Выберите объект проекта.")
        self.object_expenses_selected_label.setStyleSheet("font-weight: bold; color: #a0aec0; border: none;")
        layout.addWidget(self.object_expenses_selected_label)
        self.object_expenses_lifetime_frame, self.object_expenses_lifetime_labels = self._create_object_summary_frame("Весь срок проекта")
        layout.addWidget(self.object_expenses_lifetime_frame)
        self.object_expenses_period_frame, self.object_expenses_period_labels = self._create_object_summary_frame("За период")
        self.object_expenses_period_frame.setVisible(False)
        layout.addWidget(self.object_expenses_period_frame)

        note = QLabel(
            "Суммы показаны раздельно по валюте. Подтверждённые расходы берутся из счетов/чеков и allocation объекта; "
            "материалы определяются только категорией расхода. Остаток рассчитан по зарегистрированным платежам "
            "и не утверждает, что неизвестная оплата не производилась. Черновики не входят в подтверждённые суммы."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.object_expenses_table = QTableWidget()
        self.object_expenses_table.setColumnCount(12)
        self.object_expenses_table.setHorizontalHeaderLabels([
            "ID", "Fakturadatum", "Поставщик", "Номер", "Категория", "Netto", "Moms",
            "Доля итоговой суммы", "Оплачено по записям", "Остаток по записям", "Проверка", "Оплата",
        ])
        self.object_expenses_table.setColumnHidden(0, True)
        header = self.object_expenses_table.horizontalHeader()
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for column, width in {1: 105, 2: 165, 3: 125, 5: 110, 6: 100, 7: 125, 8: 130, 9: 145, 10: 130, 11: 140}.items():
            header.resizeSection(column, width)
        self.object_expenses_table.setAlternatingRowColors(True)
        layout.addWidget(self.object_expenses_table, 1)

        self.object_expenses_object.currentIndexChanged.connect(self.load_object_expense_overview)
        self.object_expenses_period_enabled.toggled.connect(self._set_object_expenses_period_enabled)
        self.object_expenses_date_from.dateChanged.connect(self.load_object_expense_overview)
        self.object_expenses_date_to.dateChanged.connect(self.load_object_expense_overview)
        self.load_object_expense_objects()

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
        entries = [
            f"{currency} {amount:,.2f}"
            for currency, values in sorted(totals.items())
            if (amount := values[key]) != 0
        ]
        return " · ".join(entries) if entries else "0.00"

    def load_object_expense_overview(self, *_):
        if not hasattr(self, "object_expenses_table"):
            return
        object_id = self.object_expenses_object.currentData()
        if object_id is None:
            self.object_expenses_selected_label.setText("Выберите объект проекта.")
            self.object_expenses_table.setRowCount(0)
            return
        object_name = self.object_expenses_object.currentText()
        conn = get_db_connection()
        summary = get_object_expense_summary(conn, int(object_id))
        period_enabled = self.object_expenses_period_enabled.isChecked()
        date_from = self.object_expenses_date_from.text().strip() if period_enabled else None
        date_to = self.object_expenses_date_to.text().strip() if period_enabled else None
        period_summary = get_object_expense_summary(conn, int(object_id), date_from, date_to) if period_enabled else None
        if period_enabled and date_from <= date_to:
            rows = conn.execute("""
                SELECT DISTINCT d.*
                FROM supplier_documents d
                JOIN supplier_document_allocations a ON a.document_id = d.id
                WHERE a.object_id = ? AND d.document_date >= ? AND d.document_date <= ?
                ORDER BY d.document_date DESC, d.id DESC
            """, (int(object_id), date_from, date_to)).fetchall()
            self.object_expenses_selected_label.setText(
                f"{object_name} · lifetime и период {date_from} — {date_to}"
            )
        elif period_enabled:
            rows = []
            self.object_expenses_selected_label.setText(
                f"{object_name} · некорректный период; lifetime сохранён отдельно"
            )
        else:
            rows = conn.execute("""
                SELECT DISTINCT d.*
                FROM supplier_documents d
                JOIN supplier_document_allocations a ON a.document_id = d.id
                WHERE a.object_id = ?
                ORDER BY d.document_date DESC, d.id DESC
            """, (int(object_id),)).fetchall()
            self.object_expenses_selected_label.setText(f"{object_name} · весь срок проекта")

        allocation_totals = {}
        allocations_by_document = {}
        for row in rows:
            document_id = int(row["id"])
            all_allocations = conn.execute(
                "SELECT object_id, allocated_net, allocated_vat, allocated_gross FROM supplier_document_allocations WHERE document_id=? ORDER BY id",
                (document_id,),
            ).fetchall()
            allocations_by_document[document_id] = all_allocations
            target_allocations = [item for item in all_allocations if item["object_id"] == int(object_id)]
            allocation_totals[document_id] = {
                "gross": sum((parse_decimal(item["allocated_gross"]) or Decimal(0) for item in target_allocations), Decimal(0)),
                "net_values": [parse_decimal(item["allocated_net"]) for item in target_allocations],
                "vat_values": [parse_decimal(item["allocated_vat"]) for item in target_allocations],
                "all_gross": sum((parse_decimal(item["allocated_gross"]) or Decimal(0) for item in all_allocations), Decimal(0)),
            }
        payment_totals = {}
        document_gross_totals = {}
        for row in rows:
            document_id = int(row["id"])
            payment_totals[document_id] = sum((
                parse_decimal(item[0]) or Decimal(0)
                for item in conn.execute(
                    "SELECT amount FROM supplier_document_payments WHERE document_id=?", (document_id,)
                ).fetchall()
            ), Decimal(0))
            document_gross_totals[document_id] = parse_decimal(row["gross_amount"]) or Decimal(0)
        conn.close()

        def apply_summary(labels, source, use_period=False):
            totals = source["period"] if use_period else source["lifetime"]
            labels["gross"].setText(self._format_object_currency_totals(totals, "gross"))
            labels["paid"].setText(self._format_object_currency_totals(totals, "paid"))
            labels["remaining"].setText(self._format_object_currency_totals(totals, "remaining"))
            labels["materials"].setText(self._format_object_currency_totals(totals, "materials_gross"))
            labels["other"].setText(self._format_object_currency_totals(totals, "other_gross"))
            labels["drafts"].setText(str(source["drafts_period"] if use_period else source["drafts_lifetime"]))
            labels["reviews"].setText(str(source["reviews_period"] if use_period else source["reviews_lifetime"]))

        apply_summary(self.object_expenses_lifetime_labels, summary)
        if period_summary is not None:
            apply_summary(self.object_expenses_period_labels, period_summary, use_period=True)
        self.object_expenses_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            currency = str(row["currency_code"] or "UNKNOWN")
            document_id = int(row["id"])
            allocation_info = allocation_totals[document_id]
            gross = allocation_info["gross"]
            document_gross = document_gross_totals[document_id]
            all_allocations = allocations_by_document[document_id]
            denominator = document_gross or allocation_info["all_gross"]
            due = parse_decimal(row["amount_due"]) or document_gross
            paid_assigned = due_assigned = Decimal(0)
            object_paid = object_remaining = Decimal(0)
            for index, allocation in enumerate(all_allocations):
                item_gross = parse_decimal(allocation["allocated_gross"]) or Decimal(0)
                is_last = index == len(all_allocations) - 1
                share = item_gross / denominator if denominator else Decimal(0)
                item_paid = payment_totals[document_id] - paid_assigned if is_last else (payment_totals[document_id] * share).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                item_due = due - due_assigned if is_last and due is not None else ((due * share).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if due is not None else item_gross)
                if allocation["object_id"] == int(object_id):
                    object_paid += item_paid
                    object_remaining += max(Decimal(0), item_due - item_paid)
                paid_assigned += item_paid
                due_assigned += item_due
            paid = object_paid
            remaining = object_remaining
            net_values = allocation_info["net_values"]
            vat_values = allocation_info["vat_values"]
            net = sum((value for value in net_values if value is not None), Decimal(0)) if all(value is not None for value in net_values) else (
                ((parse_decimal(row["net_amount"]) or Decimal(0)) * gross / denominator).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if denominator else Decimal(0)
            )
            vat = sum((value for value in vat_values if value is not None), Decimal(0)) if all(value is not None for value in vat_values) else (
                ((parse_decimal(row["vat_amount"]) or Decimal(0)) * gross / denominator).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if denominator else Decimal(0)
            )
            values = (
                document_id, row["document_date"] or "—", row["supplier_name"] or "Не указан",
                row["invoice_number"] or "—", row["expense_category"],
                f"{currency} {net:,.2f}", f"{currency} {vat:,.2f}", f"{currency} {gross:,.2f}",
                f"{currency} {paid:,.2f}", f"{currency} {remaining:,.2f}",
                row["verification_status"], row["payment_status"],
            )
            for column, value in enumerate(values):
                self.object_expenses_table.setItem(row_index, column, QTableWidgetItem(str(value)))

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
        self.load_objects_for_company(self.inv_comp_cb.currentText(), self.inv_obj_cb)
        self.inv_comp_cb.currentTextChanged.connect(lambda comp: self.load_objects_for_company(comp, self.inv_obj_cb))

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
        gen_btn = QPushButton("📊 Сформировать отчёт для счёта")
        gen_btn.clicked.connect(self.generate_invoice)
        
        export_pdf_inv_btn = QPushButton("📄 Экспорт отчета в PDF (SV)")
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
        layout.addWidget(QLabel("<b>Результат по объекту за период (зеленые строки уже закрыты):</b>"))
        
        self.table_invoice = QTableWidget()
        self.table_invoice.setColumnCount(9)
        self.table_invoice.setHorizontalHeaderLabels(["Сотрудник", "Рабочие часы", "Ставка", "Сумма (работа)", "Resa (SEK)", "Итого (SEK)", "Статус", "OB часы / спецификация", "OB сумма отдельно"])
        invoice_header = self.table_invoice.horizontalHeader()
        invoice_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in {
            0: 145, 1: 95, 2: 80, 3: 110, 4: 90,
            5: 95, 6: 105, 8: 125,
        }.items():
            self.table_invoice.setColumnWidth(column, width)
        invoice_header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.table_invoice.setWordWrap(True)
        self.table_invoice.setAlternatingRowColors(True)
        layout.addWidget(self.table_invoice)

        summary_box = QHBoxLayout()
        self.inv_total_hours_lbl = QLabel("<b>Всего часов: 0</b>")
        self.inv_total_ob_lbl = QLabel("<b>OB отдельно: 0 ч / 0.00 kr</b>")
        self.inv_total_sum_lbl = QLabel("<b>Общая сумма: 0.00 kr</b>")
        summary_box.addWidget(self.inv_total_hours_lbl)
        summary_box.addWidget(self.inv_total_ob_lbl)
        summary_box.addStretch()
        summary_box.addWidget(self.inv_total_sum_lbl)
        layout.addLayout(summary_box)

    def generate_invoice(self):
        d_from = self.inv_date_from.text().strip()
        d_to = self.inv_date_to.text().strip()
        comp = self.inv_comp_cb.currentText()
        obj_data = self.inv_obj_cb.currentData()
        obj = obj_data if obj_data else self.inv_obj_cb.currentText()

        if not comp or not obj:
            QMessageBox.warning(self, "Ошибка", "Выберите фирму и объект!")
            return

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT rate, transport_rate FROM objects WHERE name = ?", (obj,))
        obj_row = cursor.fetchone()
        object_default_rate = obj_row[0] if obj_row and obj_row[0] else 0.0
        object_transport_rate = obj_row[1] if obj_row and obj_row[1] else 0.0

        is_fast_pris = self.fast_pris_check.isChecked()
        fast_pris_amount = parse_float(self.fast_pris_input.text(), 0.0) if is_fast_pris else 0.0

        has_ob_schema = ob_schema_ready(cursor)
        ob_details_by_employee = collect_invoice_ob_details(
            cursor, comp, obj, d_from, d_to
        ) if has_ob_schema else {}
        if has_ob_schema:
            cursor.execute('''
                SELECT employee,
                    SUM(CASE WHEN entry_type = 'work' THEN hours ELSE 0 END) as work_hours,
                    SUM(CASE WHEN entry_type = 'work' THEN transport ELSE 0 END) as work_transport,
                    MAX(CASE WHEN entry_type = 'work' THEN invoiced ELSE 0 END) as invoiced_status,
                    SUM(CASE WHEN entry_type = 'ob' THEN hours ELSE 0 END) as ob_hours,
                    SUM(CASE WHEN entry_type = 'ob' THEN COALESCE(ob_segment.segment_amount, 0) ELSE 0 END) as ob_amount
                FROM shifts
                LEFT JOIN (
                    SELECT shift_id, SUM(amount) AS segment_amount
                    FROM ob_segments GROUP BY shift_id
                ) ob_segment ON ob_segment.shift_id = shifts.sync_id
                WHERE company = ? AND object_name = ? AND date >= ? AND date <= ?
                GROUP BY employee
                ORDER BY employee
            ''', (comp, obj, d_from, d_to))
        else:
            cursor.execute('''
                SELECT employee,
                    SUM(CASE WHEN entry_type = 'work' THEN hours ELSE 0 END) as work_hours,
                    SUM(CASE WHEN entry_type = 'work' THEN transport ELSE 0 END) as work_transport,
                    MAX(CASE WHEN entry_type = 'work' THEN invoiced ELSE 0 END) as invoiced_status,
                    0 as ob_hours, 0 as ob_amount
                FROM shifts
                WHERE company = ? AND object_name = ? AND date >= ? AND date <= ?
                GROUP BY employee
                ORDER BY employee
            ''', (comp, obj, d_from, d_to))
        rows = cursor.fetchall()
        conn.close()

        self.table_invoice.setRowCount(len(rows))
        total_hours_all = 0.0
        total_ob_hours_all = 0.0
        total_ob_amount_all = 0.0
        total_sum_all = 0.0
        total_transport_sum_all = 0.0
        total_work_sum_all = 0.0

        for row_idx, row in enumerate(rows):
            emp = row[0]
            hrs = row[1] or 0.0
            trans_count = row[2] or 0.0
            is_inv = row[3] == 1
            ob_hours, ob_amount = row[4] or 0.0, row[5] or 0.0
            status_str = "✅ Выставлен" * is_inv or "⏳ В работе"
            
            total_hours_all += hrs
            total_ob_hours_all += ob_hours
            total_ob_amount_all += ob_amount
            transport_total_sum = trans_count * object_transport_rate
            
            if is_fast_pris:
                rate = 0.0
                work_sum = 0.0
                row_total = transport_total_sum
            else:
                rate = object_default_rate
                work_sum = hrs * rate
                row_total = work_sum + transport_total_sum

            total_sum_all += row_total
            total_transport_sum_all += transport_total_sum
            total_work_sum_all += work_sum

            ob_details = ob_details_by_employee.get(emp, {})
            ob_cell_text = f"{ob_hours:g} tim" + (" · intervaller" if ob_details else "")
            row_values = [emp, f"{hrs:.1f}", f"{rate:.2f}", f"{work_sum:.2f}", f"{transport_total_sum:.2f}", f"{row_total:.2f}", status_str, ob_cell_text, f"{ob_amount:.2f}"]
            for col_idx, val in enumerate(row_values):
                it = QTableWidgetItem(str(val))
                if col_idx == 7 and ob_details:
                    it.setToolTip(ob_details["tooltip"])
                if is_inv:
                    it.setBackground(QColor("#1c4532"))
                self.table_invoice.setItem(row_idx, col_idx, it)

        if is_fast_pris:
            total_sum_all = fast_pris_amount
            self.inv_total_hours_lbl.setText(f"<b>Рабочие часы: {total_hours_all:.1f} (Fast pris)</b>")
            self.inv_total_sum_lbl.setText(f"<b>Общая сумма по договору: {fast_pris_amount:,.2f} kr</b>")
        else:
            self.inv_total_hours_lbl.setText(f"<b>Рабочие часы: {total_hours_all:.1f}</b>")
            self.inv_total_sum_lbl.setText(f"<b>Общая сумма: {total_sum_all:,.2f} kr</b>")
        self.inv_total_ob_lbl.setText(f"<b>OB: {total_ob_hours_all:g} tim / {total_ob_amount_all:,.2f} kr</b>")

    def export_invoice_to_pdf(self):
        d_from = self.inv_date_from.text().strip()
        d_to = self.inv_date_to.text().strip()
        comp = self.inv_comp_cb.currentText()
        obj_data = self.inv_obj_cb.currentData()
        obj = obj_data if obj_data else self.inv_obj_cb.currentText()

        if not comp or not obj:
            QMessageBox.warning(self, "Ошибка", "Выберите фирму и объект для формирования PDF-счета!")
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT rate, transport_rate FROM objects WHERE name = ?", (obj,))
        obj_row = cursor.fetchone()
        object_default_rate = obj_row[0] if obj_row and obj_row[0] else 0.0
        object_transport_rate = obj_row[1] if obj_row and obj_row[1] else 0.0

        is_fast_pris = self.fast_pris_check.isChecked()
        fast_pris_amount = parse_float(self.fast_pris_input.text(), 0.0) if is_fast_pris else 0.0

        has_ob_schema = ob_schema_ready(cursor)
        ob_details_by_employee = collect_invoice_ob_details(
            cursor, comp, obj, d_from, d_to
        ) if has_ob_schema else {}
        if has_ob_schema:
            cursor.execute('''
                SELECT employee,
                    SUM(CASE WHEN entry_type = 'work' THEN hours ELSE 0 END) as work_hours,
                    SUM(CASE WHEN entry_type = 'work' THEN transport ELSE 0 END) as work_transport,
                    SUM(CASE WHEN entry_type = 'ob' THEN hours ELSE 0 END) as ob_hours,
                    SUM(CASE WHEN entry_type = 'ob' THEN COALESCE(ob_segment.segment_amount, 0) ELSE 0 END) as ob_amount
                FROM shifts
                LEFT JOIN (SELECT shift_id, SUM(amount) AS segment_amount FROM ob_segments GROUP BY shift_id) ob_segment
                    ON ob_segment.shift_id = shifts.sync_id
                WHERE company = ? AND object_name = ? AND date >= ? AND date <= ?
                GROUP BY employee ORDER BY employee
            ''', (comp, obj, d_from, d_to))
        else:
            cursor.execute('''
                SELECT employee,
                    SUM(CASE WHEN entry_type = 'work' THEN hours ELSE 0 END) as work_hours,
                    SUM(CASE WHEN entry_type = 'work' THEN transport ELSE 0 END) as work_transport,
                    0 as ob_hours, 0 as ob_amount
                FROM shifts
                WHERE company = ? AND object_name = ? AND date >= ? AND date <= ?
                GROUP BY employee ORDER BY employee
            ''', (comp, obj, d_from, d_to))
        rows = cursor.fetchall()
        conn.close()

        if not rows and not is_fast_pris:
            QMessageBox.warning(self, "Внимание", "За выбранный период нет данных для отчета!")
            return

        if not os.path.exists(REPORTS_DIR):
            os.makedirs(REPORTS_DIR)

        base_filename = f"Fakturaunderlag_{comp.replace(' ', '_')}_{obj.replace(' ', '_')}_{d_from}_till_{d_to}.pdf"
        filepath = os.path.join(REPORTS_DIR, base_filename)

        try:
            doc = SimpleDocTemplate(filepath, pagesize=A4, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
            story = []
            styles = getSampleStyleSheet()
            
            title_style = ParagraphStyle('TitleSV', parent=styles['Heading1'], fontName=DEFAULT_FONT_BOLD, fontSize=15, spaceAfter=4)
            subtitle_style = ParagraphStyle('SubTitleSV', parent=styles['Normal'], fontName=DEFAULT_FONT, fontSize=10, spaceAfter=14)

            story.append(Paragraph("<b>Bygger och renoverar i Sthlm AB</b>", title_style))
            story.append(Paragraph(f"<b>Fakturaunderlag (Underlag för faktura)</b><br/>Beställare (Firma): <b>{comp}</b><br/>Objekt: <b>{obj}</b><br/>Period: {d_from} till {d_to}", subtitle_style))
            story.append(Spacer(1, 5))

            pdf_header_style = ParagraphStyle(
                "InvoiceHeader", parent=styles['Normal'], fontName=DEFAULT_FONT_BOLD,
                fontSize=7, leading=8, wordWrap='CJK'
            )
            table_data = [[
                Paragraph(escape(label), pdf_header_style)
                for label in (
                    "Anställd", "Arbete h", "Pris/tim", "Arbete (SEK)",
                    "Resa (SEK)", "Totalt (SEK)", "OB h / specifikation",
                    "OB separat (SEK)",
                )
            ]]
            total_hours_all = 0.0
            total_ob_hours_all = 0.0
            total_ob_amount_all = 0.0
            total_sum_all = 0.0
            total_work_sum_all = 0.0
            total_transport_sum_all = 0.0

            for row in rows:
                emp = row[0]
                hrs = row[1] or 0.0
                trans_count = row[2] or 0.0
                ob_hours, ob_amount = row[3] or 0.0, row[4] or 0.0
                total_hours_all += hrs
                total_ob_hours_all += ob_hours
                total_ob_amount_all += ob_amount

                transport_total_sum = trans_count * object_transport_rate

                if is_fast_pris:
                    rate = 0.0
                    work_sum = 0.0
                    row_total = transport_total_sum
                else:
                    rate = object_default_rate
                    work_sum = hrs * rate
                    row_total = work_sum + transport_total_sum

                total_sum_all += row_total
                total_work_sum_all += work_sum
                total_transport_sum_all += transport_total_sum
                ob_details = ob_details_by_employee.get(emp, {}).get("display", "")
                ob_spec = Paragraph(
                    f"{ob_hours:g} tim<br/>{escape(ob_details).replace(chr(10), '<br/>')}",
                    ParagraphStyle(
                        f"InvoiceObCell{len(table_data)}",
                        parent=styles['Normal'],
                        fontName=DEFAULT_FONT,
                        fontSize=7,
                        leading=9,
                        wordWrap='CJK',
                    ),
                )
                table_data.append([
                    Paragraph(escape(str(emp)), ParagraphStyle(
                        f"InvoiceEmployee{len(table_data)}", parent=styles['Normal'],
                        fontName=DEFAULT_FONT, fontSize=8, leading=10, wordWrap='CJK'
                    )),
                    f"{hrs:.1f}", f"{rate:.2f}", f"{work_sum:.2f}",
                    f"{transport_total_sum:.2f}", f"{row_total:.2f}", ob_spec,
                    f"{ob_amount:.2f}"
                ])

            if is_fast_pris:
                total_sum_all = fast_pris_amount
                table_data.append([
                    "Fast pris (Enligt avtal)", "", "", "", "",
                    f"{fast_pris_amount:.2f}", f"{total_ob_hours_all:g} tim",
                    f"{total_ob_amount_all:.2f}",
                ])

            table_data.append([
                "Totalt:", f"{total_hours_all:.1f}", "",
                f"{total_work_sum_all:,.2f}", f"{total_transport_sum_all:,.2f}",
                f"{total_sum_all:,.2f}", f"{total_ob_hours_all:g} tim",
                f"{total_ob_amount_all:,.2f}",
            ])

            t = Table(table_data, colWidths=[78, 35, 34, 55, 48, 55, 170, 60])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2b6cb0')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
                ('FONTNAME', (0, 0), (-1, -1), DEFAULT_FONT),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
                ('TOPPADDING', (0, 0), (-1, 0), 6),
                ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.HexColor('#f9f9f9'), colors.white]),
                ('GRID', (0, 0), (-1, -2), 0.5, colors.HexColor('#dddddd')),
                ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#eeeeee')),
                ('FONTNAME', (0, -1), (-1, -1), DEFAULT_FONT_BOLD),
            ]))

            story.append(t)
            doc.build(story)
            QMessageBox.information(self, "Успех", f"PDF-отчет для счета сохранен:\n{base_filename}")
            
            if sys.platform == 'win32':
                os.startfile(filepath)
            elif sys.platform == 'darwin':
                subprocess.run(['open', filepath])
            else:
                subprocess.run(['xdg-open', filepath])
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось создать PDF счета: {e}")

    def mark_period_as_invoiced(self):
        d_from = self.inv_date_from.text().strip()
        d_to = self.inv_date_to.text().strip()
        comp = self.inv_comp_cb.currentText()
        obj_data = self.inv_obj_cb.currentData()
        obj = obj_data if obj_data else self.inv_obj_cb.currentText()

        if not comp or not obj:
            QMessageBox.warning(self, "Ошибка", "Выберите фирму и объект для закрытия периода!")
            return

        reply = QMessageBox.question(
            self, "Подтверждение",
            f"Отметить все смены по объекту <b>{obj}</b> за период <b>{d_from} — {d_to}</b> как выставленные в счет?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE shifts 
                SET invoiced = 1 
                WHERE company = ? AND object_name = ? AND date >= ? AND date <= ? AND entry_type = 'work'
            ''', (comp, obj, d_from, d_to))
            enqueue_matching_shifts(cursor, "company = ? AND object_name = ? AND date >= ? AND date <= ? AND entry_type = 'work'", (comp, obj, d_from, d_to))
            conn.commit()
            conn.close()

            QMessageBox.information(self, "Успех", "Период успешно закрыт!")
            self.generate_invoice()
            self.load_shifts_history()
            self.background_sync_with_cloud()

    def unmark_period_as_invoiced(self):
        d_from = self.inv_date_from.text().strip()
        d_to = self.inv_date_to.text().strip()
        comp = self.inv_comp_cb.currentText()
        obj_data = self.inv_obj_cb.currentData()
        obj = obj_data if obj_data else self.inv_obj_cb.currentText()

        if not comp or not obj:
            QMessageBox.warning(self, "Ошибка", "Выберите фирму и объект для отмены закрытия!")
            return

        reply = QMessageBox.question(
            self, "Подтверждение",
            f"Вернуть в работу все смены по объекту <b>{obj}</b> за период <b>{d_from} — {d_to}</b>?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE shifts 
                SET invoiced = 0 
                WHERE company = ? AND object_name = ? AND date >= ? AND date <= ? AND entry_type = 'work'
            ''', (comp, obj, d_from, d_to))
            enqueue_matching_shifts(cursor, "company = ? AND object_name = ? AND date >= ? AND date <= ? AND entry_type = 'work'", (comp, obj, d_from, d_to))
            conn.commit()
            conn.close()

            QMessageBox.information(self, "Успех", "Статус сброшен!")
            self.generate_invoice()
            self.load_shifts_history()
            self.background_sync_with_cloud()

    def load_objects_for_company(self, comp_name, obj_cb, record_type=None):
        selected_name = obj_cb.currentData()
        obj_cb.clear()
        if not comp_name:
            return False
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, COALESCE(markning, ''), COALESCE(rate, 0.0), COALESCE(transport_rate, 0.0) FROM objects WHERE company = ? ORDER BY name", (comp_name,))
        rows = cursor.fetchall()
        conn.close()
        if record_type in SPECIAL_OBJECTS:
            ref_name, ref_mark = SPECIAL_OBJECTS[record_type]
            rows = [row for row in rows if row[0] == ref_name and row[1] == ref_mark]
            if len(rows) != 1:
                return False
        elif record_type == RECORD_TYPE_WORK:
            special_refs = set(SPECIAL_OBJECTS.values())
            rows = [row for row in rows if (row[0], row[1]) not in special_refs]
        for row in rows:
            name, mark, rate, tr_rate = row[0], row[1], row[2], row[3]
            display = f"{mark} | {name} ({rate} kr/ч, тр: {tr_rate})" if mark else f"{name} ({rate} kr/ч)"
            obj_cb.addItem(display, userData=name)
        if selected_name:
            selected_index = obj_cb.findData(selected_name)
            if selected_index >= 0:
                obj_cb.setCurrentIndex(selected_index)
        return bool(rows)

    def setup_dagbok_tab(self):
        layout = QVBoxLayout(self.tab_dagbok)
        card = QFrame()
        card.setObjectName("card")
        card_layout = QFormLayout(card)
        card_layout.setSpacing(8)

        self.dag_date_from = create_date_field("2026-10-01")
        self.dag_date_to = create_date_field("2026-10-31")
        
        self.dag_comp_cb = QComboBox()
        self.load_companies_into_combobox(self.dag_comp_cb)
        
        self.dag_obj_cb = QComboBox()
        self.load_objects_for_company(self.dag_comp_cb.currentText(), self.dag_obj_cb)
        self.dag_comp_cb.currentTextChanged.connect(lambda comp: self.load_objects_for_company(comp, self.dag_obj_cb))

        card_layout.addRow("Период с:", self.dag_date_from)
        card_layout.addRow("Период по:", self.dag_date_to)
        card_layout.addRow("Фирма:", self.dag_comp_cb)
        card_layout.addRow("Объект:", self.dag_obj_cb)

        btn_layout = QHBoxLayout()
        gen_dag_btn = QPushButton("📖 Сформировать журнал работ")
        gen_dag_btn.clicked.connect(self.generate_dagbok)
        
        export_pdf_btn = QPushButton("📄 Экспорт в PDF (SV)")
        export_pdf_btn.clicked.connect(self.export_dagbok_to_pdf)

        btn_layout.addWidget(gen_dag_btn)
        btn_layout.addWidget(export_pdf_btn)
        card_layout.addRow(btn_layout)

        layout.addWidget(card)
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
        self.dag_total_ob_lbl = QLabel("<b>OB отдельно: 0.0 ч / 0.00 kr</b>")
        dag_summary_box.addWidget(self.dag_total_hours_lbl)
        dag_summary_box.addWidget(self.dag_total_atf_lbl)
        dag_summary_box.addWidget(self.dag_total_sick_lbl)
        dag_summary_box.addWidget(self.dag_total_ob_lbl)
        dag_summary_box.addStretch()
        layout.addLayout(dag_summary_box)

    def generate_dagbok(self):
        d_from = self.dag_date_from.text().strip()
        d_to = self.dag_date_to.text().strip()
        comp = self.dag_comp_cb.currentText()
        obj_data = self.dag_obj_cb.currentData()
        obj = obj_data if obj_data else self.dag_obj_cb.currentText()

        if not comp or not obj:
            QMessageBox.warning(self, "Ошибка", "Выберите фирму и объект!")
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        has_ob_schema = ob_schema_ready(cursor)
        if has_ob_schema:
            cursor.execute('''
                SELECT date, employee, entry_type, hours, comment, sync_id, start_time, end_time
                FROM shifts
                WHERE company = ? AND object_name = ? AND date >= ? AND date <= ?
                ORDER BY date ASC, employee ASC
            ''', (comp, obj, d_from, d_to))
        else:
            cursor.execute('''
                SELECT date, employee, entry_type, hours, comment, sync_id
                FROM shifts
                WHERE company = ? AND object_name = ? AND date >= ? AND date <= ?
                ORDER BY date ASC, employee ASC
            ''', (comp, obj, d_from, d_to))
        rows = cursor.fetchall()
        ob_segments_by_id = {
            row["sync_id"]: get_ob_segments(cursor, row["sync_id"])
            for row in rows if has_ob_schema and row["entry_type"] == RECORD_TYPE_OB
        }
        conn.close()

        self.table_dagbok.setRowCount(len(rows))
        totals = {RECORD_TYPE_WORK: 0.0, RECORD_TYPE_ATF: 0.0, RECORD_TYPE_SICK: 0.0, RECORD_TYPE_OB: 0.0}
        ob_amount_total = 0.0

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
                ob_amount = sum(segment["amount"] for segment in segments)
                ob_amount_total += ob_amount
                start_t = row["start_time"] if "start_time" in row.keys() else None
                end_t = row["end_time"] if "end_time" in row.keys() else None
                detalization = format_ob_detail(start_t, end_t, hrs, segments)
                if not detalization and segments:
                    detalization = f"{segments[0]['start_time']}–{segments[-1]['end_time']} ({hrs:.1f} ч)"
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
            
            it_date = QTableWidgetItem(str(date))
            it_date.setTextAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
            it_emp = QTableWidgetItem(str(emp))
            it_type = QTableWidgetItem(record_type_short_label(record_type))
            it_type.setTextAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
            it_hrs = QTableWidgetItem(f"{hrs:.1f} ч")
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

        self.dag_total_hours_lbl.setText(f"<b>Работа: {totals[RECORD_TYPE_WORK]:.1f} ч</b>")
        self.dag_total_atf_lbl.setText(f"<b>АТФ: {totals[RECORD_TYPE_ATF]:.1f} ч</b>")
        self.dag_total_sick_lbl.setText(f"<b>Больничный: {totals[RECORD_TYPE_SICK]:.1f} ч</b>")
        self.dag_total_ob_lbl.setText(f"<b>OB отдельно: {totals[RECORD_TYPE_OB]:.2f} ч / {ob_amount_total:,.2f} kr</b>")

    def export_dagbok_to_pdf(self):
        d_from = self.dag_date_from.text().strip()
        d_to = self.dag_date_to.text().strip()
        comp = self.dag_comp_cb.currentText()
        obj_data = self.dag_obj_cb.currentData()
        obj = obj_data if obj_data else self.dag_obj_cb.currentText()

        if not comp or not obj:
            QMessageBox.warning(self, "Ошибка", "Выберите фирму и объект!")
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        has_ob_schema = ob_schema_ready(cursor)
        cursor.execute('''
            SELECT date, employee, entry_type, hours, comment, sync_id
            FROM shifts
            WHERE company = ? AND object_name = ? AND date >= ? AND date <= ?
              AND entry_type IN ('work', 'ob')
            ORDER BY date ASC, employee ASC
        ''', (comp, obj, d_from, d_to))
        rows = cursor.fetchall()
        ob_segments_by_id = {
            row["sync_id"]: get_ob_segments(cursor, row["sync_id"])
            for row in rows if has_ob_schema and row["entry_type"] == RECORD_TYPE_OB
        }
        conn.close()

        if not rows:
            QMessageBox.warning(self, "Внимание", "За выбранный период нет записей!")
            return

        if not os.path.exists(REPORTS_DIR):
            os.makedirs(REPORTS_DIR)

        base_filename = f"Arbetsdagbok_{comp.replace(' ', '_')}_{obj.replace(' ', '_')}_{d_from}_till_{d_to}.pdf"
        filepath = os.path.join(REPORTS_DIR, base_filename)
        
        try:
            doc = SimpleDocTemplate(filepath, pagesize=A4, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
            story = []
            styles = getSampleStyleSheet()
            
            title_style = ParagraphStyle('TitleSV', parent=styles['Heading1'], fontName=DEFAULT_FONT_BOLD, fontSize=15, spaceAfter=4)
            subtitle_style = ParagraphStyle('SubTitleSV', parent=styles['Normal'], fontName=DEFAULT_FONT, fontSize=10, spaceAfter=14)
            cell_text_style = ParagraphStyle(
                'CellTextSV', parent=styles['Normal'], fontName=DEFAULT_FONT,
                fontSize=9, leading=12, wordWrap='CJK'
            )
            cell_bold_style = ParagraphStyle(
                'CellBoldSV', parent=cell_text_style, fontName=DEFAULT_FONT_BOLD
            )

            story.append(Paragraph("<b>Bygger och renoverar i Sthlm AB</b>", title_style))
            story.append(Paragraph(f"<b>Arbetsdagbok (Dagbok)</b><br/>Beställare: <b>{comp}</b><br/>Objekt: <b>{obj}</b><br/>Period: {d_from} till {d_to}", subtitle_style))
            story.append(Spacer(1, 5))

            table_data = [["Datum", "Anställd", "Typ", "Timmar", "Arbetsbeskrivning"]]
            totals = {RECORD_TYPE_WORK: 0.0, RECORD_TYPE_OB: 0.0}

            swedish_type_labels = {
                RECORD_TYPE_WORK: "Arbete",
                RECORD_TYPE_OB: "OB / Övertid",
            }

            for row in rows:
                date, emp, record_type, hrs, comment = row[0], row[1], row[2], row[3], row[4]
                if record_type in totals:
                    totals[record_type] += hrs
                if record_type == RECORD_TYPE_OB:
                    segments = ob_segments_by_id.get(row["sync_id"], [])
                    segment_text = "; ".join(
                        f"{segment['start_time']}–{segment['end_time']} ({segment['hours']:.2f} tim)"
                        for segment in segments
                    )
                    comment = f"{comment or ''} | OB-intervaller: {segment_text}".strip(" |")
                translated_comment = translate_to_swedish(comment)
                type_display = swedish_type_labels.get(record_type, "Arbete")
                
                comment_p = Paragraph(str(translated_comment or ""), cell_text_style)
                emp_p = Paragraph(str(emp), cell_text_style)
                
                table_data.append([
                    str(date),
                    emp_p,
                    type_display,
                    f"{hrs:.2f} tim",
                    comment_p
                ])

            table_data.extend([
                ["Totalt arbete:", "", "", f"{totals[RECORD_TYPE_WORK]:.2f} tim", ""],
                ["Totalt OB:", "", "", f"{totals[RECORD_TYPE_OB]:.2f} tim", ""],
            ])

            t = Table(table_data, colWidths=[70, 95, 80, 65, 225])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4299e1')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('FONTNAME', (0, 0), (-1, -1), DEFAULT_FONT),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
                ('TOPPADDING', (0, 0), (-1, 0), 6),
                ('ROWBACKGROUNDS', (0, 1), (-1, -3), [colors.HexColor('#f9f9f9'), colors.white]),
                ('GRID', (0, 0), (-1, -3), 0.5, colors.HexColor('#dddddd')),
                ('BACKGROUND', (0, -2), (-1, -1), colors.HexColor('#eeeeee')),
                ('FONTNAME', (0, -2), (-1, -1), DEFAULT_FONT_BOLD),
                ('LINEABOVE', (0, -2), (-1, -2), 1.5, colors.HexColor('#2b6cb0')),
            ]))

            story.append(t)
            doc.build(story)
            QMessageBox.information(self, "Успех", f"PDF-отчет сохранен:\n{base_filename}")
            
            if sys.platform == 'win32':
                os.startfile(filepath)
            elif sys.platform == 'darwin':
                subprocess.run(['open', filepath])
            else:
                subprocess.run(['xdg-open', filepath])
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось создать PDF: {e}")

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

        self.table_employees = QTableWidget()
        self.table_employees.setColumnCount(3)
        self.table_employees.setHorizontalHeaderLabels(["ФИО", "kr/ч", "Статус ПИН-кода"])
        self.table_employees.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        emp_card_layout.addWidget(self.table_employees, stretch=1)

        emp_btn_layout = QHBoxLayout()
        del_emp_btn = QPushButton("🗑 Удалить сотрудника")
        del_emp_btn.setObjectName("danger")
        del_emp_btn.clicked.connect(self.delete_employee)
        
        set_pin_btn = QPushButton("🔑 Задать ПИН")
        set_pin_btn.clicked.connect(self.reset_employee_pin_desktop)

        emp_btn_layout.addWidget(del_emp_btn)
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
        self.table_objects.setColumnCount(5)
        self.table_objects.setHorizontalHeaderLabels(["Фирма", "Объект / Адрес", "Markning", "Факт. ставка (kr/ч)", "Транспорт (kr/выезд)"])
        
        obj_header = self.table_objects.horizontalHeader()
        obj_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        obj_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        obj_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        obj_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        obj_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        
        self.table_objects.setColumnWidth(0, 150)
        self.table_objects.setColumnWidth(2, 110)
        self.table_objects.setColumnWidth(3, 140)
        self.table_objects.setColumnWidth(4, 150)

        self.table_objects.cellChanged.connect(self.on_object_cell_changed)
        obj_card_layout.addWidget(self.table_objects, stretch=1)

        del_obj_btn = QPushButton("🗑 Удалить выбранный объект")
        del_obj_btn.setObjectName("danger")
        del_obj_btn.clicked.connect(self.delete_object)
        obj_card_layout.addWidget(del_obj_btn)

        layout.addWidget(obj_card, stretch=1)

    def load_companies_into_combobox(self, cb):
        current_val = cb.currentText()
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
            if idx >= 0:
                cb.setCurrentIndex(idx)

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
            cursor.execute('''
                INSERT INTO objects (name, markning, company, rate, transport_rate) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET markning = ?, company = ?, rate = ?, transport_rate = ?
            ''', (name, mark, comp, rate_val, trans_rate_val, mark, comp, rate_val, trans_rate_val))
            conn.commit()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось добавить объект: {e}")
        conn.close()
        self.new_obj_name_input.clear()
        self.new_obj_mark_input.clear()
        self.new_obj_rate_input.clear()
        self.new_obj_trans_input.clear()
        self.load_dropdowns()
        self.background_sync_with_cloud()

    def on_object_cell_changed(self, row, col):
        if col in [3, 4]:
            try:
                name_item = self.table_objects.item(row, 1)
                rate_item = self.table_objects.item(row, 3)
                trans_item = self.table_objects.item(row, 4)
                if not name_item or not rate_item or not trans_item:
                    return
                obj_name = name_item.text().strip()
                new_rate = parse_float(rate_item.text(), 0.0)
                new_trans = parse_float(trans_item.text(), 0.0)

                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("UPDATE objects SET rate = ?, transport_rate = ? WHERE name = ?", (new_rate, new_trans, obj_name))
                conn.commit()
                conn.close()
                self.background_sync_with_cloud()
            except Exception:
                pass

    def delete_object(self):
        selected = self.table_objects.currentRow()
        if selected < 0:
            return
        obj_name = self.table_objects.item(selected, 1).text()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM objects WHERE name = ?', (obj_name,))
        conn.commit()
        conn.close()
        self.load_dropdowns()
        self.background_sync_with_cloud()

    def load_dropdowns(self):
        conn = get_db_connection()
        cursor = conn.cursor()

        if hasattr(self, 'emp_cb'):
            self.emp_cb.clear()
            cursor.execute("SELECT name FROM employees ORDER BY name")
            for row in cursor.fetchall():
                self.emp_cb.addItem(row[0])

        if hasattr(self, 'bal_emp_cb'):
            selected_balance_person = self.bal_emp_cb.currentText()
            self.bal_emp_cb.blockSignals(True)
            self.bal_emp_cb.clear()
            cursor.execute("SELECT name FROM employees ORDER BY name")
            for row in cursor.fetchall():
                self.bal_emp_cb.addItem(row[0])
            if selected_balance_person:
                self.bal_emp_cb.setCurrentText(selected_balance_person)
            self.bal_emp_cb.blockSignals(False)

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
            cursor.execute("SELECT name FROM objects ORDER BY name")
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
            self.load_objects_for_company(self.inv_comp_cb.currentText(), self.inv_obj_cb)

        if hasattr(self, 'dag_obj_cb') and hasattr(self, 'dag_comp_cb'):
            self.load_objects_for_company(self.dag_comp_cb.currentText(), self.dag_obj_cb)

        conn.close()

        if hasattr(self, 'table_employees'):
            self.load_employees_table()
        if hasattr(self, 'table_objects'):
            self.load_objects_table()
        if hasattr(self, 'table_companies'):
            self.load_companies_table()

    def load_objects_table(self):
        try:
            self.table_objects.cellChanged.disconnect(self.on_object_cell_changed)
        except TypeError:
            pass

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT company, name, markning, COALESCE(rate, 0.0), COALESCE(transport_rate, 0.0) FROM objects ORDER BY company, name")
        rows = cursor.fetchall()
        conn.close()

        self.table_objects.setRowCount(len(rows))
        for row_idx, row in enumerate(rows):
            self.table_objects.setItem(row_idx, 0, QTableWidgetItem(row[0] or ""))
            self.table_objects.setItem(row_idx, 1, QTableWidgetItem(row[1]))
            self.table_objects.setItem(row_idx, 2, QTableWidgetItem(row[2] or ""))
            
            rate_item = QTableWidgetItem(f"{row[3]:.2f}")
            rate_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table_objects.setItem(row_idx, 3, rate_item)

            trans_item = QTableWidgetItem(f"{row[4]:.2f}")
            trans_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table_objects.setItem(row_idx, 4, trans_item)

        self.table_objects.cellChanged.connect(self.on_object_cell_changed)

    def load_employees_table(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, salary_rate FROM employees ORDER BY name")
        rows = cursor.fetchall()
        conn.close()

        pin_statuses = {}
        try:
            headers = {"X-Sync-Key": SYNC_API_KEY}
            res = requests.get(f"{CLOUD_URL}/admin/get-users-status", headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                for u in data.get("users", []):
                    pin_statuses[u["username"].strip().lower()] = u.get("has_pin", False)
        except Exception:
            pass

        self.table_employees.setRowCount(len(rows))
        for row_idx, row in enumerate(rows):
            name, rate = row[0], row[1]
            has_pin = pin_statuses.get(name.strip().lower(), None)

            if has_pin is None:
                pin_text = "❓ Неизвестно"
            elif has_pin:
                pin_text = "✅ Пин задан"
            else:
                pin_text = "⏳ Не задан"

            self.table_employees.setItem(row_idx, 0, QTableWidgetItem(name))
            self.table_employees.setItem(row_idx, 1, QTableWidgetItem(f"{rate:.2f}" if rate else "0.00"))
            
            pin_item = QTableWidgetItem(pin_text)
            if has_pin:
                pin_item.setForeground(QColor("#68D391"))
            elif has_pin is False:
                pin_item.setForeground(QColor("#F6AD55"))
            self.table_employees.setItem(row_idx, 2, pin_item)

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
                headers = {"X-Sync-Key": SYNC_API_KEY}
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
        cursor.execute('''
            INSERT INTO employees (name, salary_rate) VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET salary_rate = ?
        ''', (name, rate_val, rate_val))
        conn.commit()
        conn.close()
        self.new_emp_input.clear()
        self.new_emp_rate_input.clear()
        self.load_dropdowns()
        self.background_sync_with_cloud()

    def delete_employee(self):
        selected = self.table_employees.currentRow()
        if selected < 0:
            return
        emp_name = self.table_employees.item(selected, 0).text()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM employees WHERE name = ?', (emp_name,))
        conn.commit()
        conn.close()
        self.load_dropdowns()
        self.background_sync_with_cloud()

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
            QMessageBox.warning(self, "Тип записи", "Необходимо выбрать Работа, АТФ или Больничный.")
            return
        else:
            cursor.execute("SELECT COALESCE(markning, '') FROM objects WHERE company = ? AND name = ?", (comp, obj_name))
            object_row = cursor.fetchone()
            if object_row and (obj_name, object_row[0]) in set(SPECIAL_OBJECTS.values()):
                conn.close()
                QMessageBox.warning(self, "Тип записи", "Справочные объекты АТФ и больничного нельзя сохранить как обычную работу.")
                return
        
        hours_val = ob_result["total_hours"] if ob_result else parse_float(self.hours_input.text(), -1)
        
        rate_input_str = self.rate_input.text().strip()
        if ob_result:
            rate_val = 0.0
        elif rate_input_str:
            rate_val = parse_float(rate_input_str, -1)
        else:
            cursor.execute("SELECT rate FROM objects WHERE name = ?", (obj_name,))
            obj_row = cursor.fetchone()
            rate_val = obj_row[0] if obj_row and obj_row[0] else 0.0

        trans_val = parse_float(self.trans_input.text(), 0.0)
        comment = self.comment_input.text().strip()
        if record_type in (RECORD_TYPE_WORK, RECORD_TYPE_OB):
            rate_val = 0.0
            trans_val = 0.0

        if hours_val < 0 or rate_val < 0:
            conn.close()
            QMessageBox.warning(self, "Ошибка", "Проверьте правильность часов и ставки!")
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
        
        if record_type in (RECORD_TYPE_WORK, RECORD_TYPE_OB):
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
        cursor.execute("SELECT name FROM employees ORDER BY name")
        employees = [row[0] for row in cursor.fetchall()]
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
        employee_field.addItems(employees)
        employee_field.setCurrentText(shift['employee'])
        company_field = QComboBox()
        company_field.setEditable(True)
        company_field.addItems(companies)
        company_field.setCurrentText(shift['company'])
        record_type_field = QComboBox()
        for record_type in (RECORD_TYPE_WORK, RECORD_TYPE_OB, RECORD_TYPE_ATF, RECORD_TYPE_SICK):
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
        self.load_objects_for_company(shift['company'], object_field, current_type)
        object_index = object_field.findData(shift['object_name'])
        if object_index >= 0:
            object_field.setCurrentIndex(object_index)
        company_field.currentTextChanged.connect(lambda company: self.load_objects_for_company(company, object_field, record_type_field.currentData()))
        record_type_field.currentIndexChanged.connect(lambda *_: self.load_objects_for_company(company_field.currentText(), object_field, record_type_field.currentData()))
        hours_field = QDoubleSpinBox()
        hours_field.setRange(0.5, 24)
        hours_field.setSingleStep(0.5)
        hours_field.setDecimals(1)
        hours_field.setValue(shift['hours'])
        rate_field = QDoubleSpinBox()
        rate_field.setRange(0, 100000)
        rate_field.setDecimals(2)
        rate_field.setValue(shift['rate'])
        transport_field = QDoubleSpinBox()
        transport_field.setRange(0, 10000)
        transport_field.setDecimals(2)
        transport_field.setValue(shift['transport'] or 0)
        comment_field = QLineEdit(shift['comment'] or '')
        ob_start_field = create_half_hour_time_field((shift["start_time"] if has_ob_schema else None) or "07:00")
        ob_end_field = create_half_hour_time_field((shift["end_time"] if has_ob_schema else None) or "18:00")
        ob_preview_field = QLabel("")
        ob_preview_field.setWordWrap(True)

        def update_type_values():
            new_type = record_type_field.currentData()
            if previous_type[0] == RECORD_TYPE_WORK and new_type != RECORD_TYPE_WORK:
                work_values[0] = rate_field.value()
                work_values[1] = transport_field.value()
                rate_field.setValue(0.0)
                transport_field.setValue(0.0)
            elif previous_type[0] != RECORD_TYPE_WORK and new_type == RECORD_TYPE_WORK:
                rate_field.setValue(work_values[0])
                transport_field.setValue(work_values[1])
            elif new_type != RECORD_TYPE_WORK:
                rate_field.setValue(0.0)
                transport_field.setValue(0.0)
            previous_type[0] = new_type

        def refresh_dialog_ob_preview(*_):
            if record_type_field.currentData() != RECORD_TYPE_OB:
                ob_preview_field.setText("")
                return
            preview_conn = get_db_connection()
            try:
                base_rate = get_local_employee_ob_base_rate(
                    preview_conn.cursor(), employee_field.currentText().strip()
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
            for widget in (hours_field, rate_field, transport_field):
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
            QMessageBox.warning(self, "Тип записи", "Необходимо выбрать Работа, АТФ или Больничный.")
            return
        else:
            check_cursor.execute("SELECT COALESCE(markning, '') FROM objects WHERE company = ? AND name = ?", (company_field.currentText().strip(), object_name))
            object_row = check_cursor.fetchone()
            if object_row and (object_name, object_row[0]) in set(SPECIAL_OBJECTS.values()):
                check_conn.close()
                QMessageBox.warning(self, "Тип записи", "Справочные объекты АТФ и больничного нельзя сохранить как обычную работу.")
                return
        check_conn.close()

        if record_type == RECORD_TYPE_OB:
            schema_conn = get_db_connection()
            schema_ready = ob_schema_ready(schema_conn.cursor())
            rate_cursor = schema_conn.cursor()
            try:
                employee_base_rate = get_local_employee_ob_base_rate(
                    rate_cursor, employee_field.currentText().strip()
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
            "date": payload_date, "employee": employee_field.currentText().strip(),
            "company": company_field.currentText().strip(), "object_name": object_name,
            "hours": ob_result["total_hours"] if ob_result else hours_field.value(),
            "rate": rate_field.value() if record_type == RECORD_TYPE_WORK else 0.0,
            "transport": transport_field.value() if record_type == RECORD_TYPE_WORK else 0.0,
            "comment": comment_field.text().strip(),
            "invoiced": shift['invoiced'] or 0, "record_type": record_type
        }
        if ob_result:
            payload.update({
                "start_time": ob_result["start_time"], "end_time": ob_result["end_time"],
            })
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
                    transport_field.value() if record_type == RECORD_TYPE_WORK else work_values[1],
                    ob_result["start_time"] if ob_result else None,
                    ob_result["end_time"] if ob_result else None,
                    ob_result["segments"][0]["base_rate"] if ob_result else None,
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
                    transport_field.value() if record_type == RECORD_TYPE_WORK else work_values[1],
                    local_id
                ))
            if ob_result:
                replace_ob_segments(cursor, sync_id, ob_result["segments"])
            elif ob_schema_ready(cursor) and shift['entry_type'] == RECORD_TYPE_OB:
                cursor.execute("DELETE FROM ob_segments WHERE shift_id = ?", (sync_id,))
            enqueue_shift_operation(cursor, sync_id, 'upsert', shift['sync_version'], payload)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self.load_shifts_history()
        if record_type in (RECORD_TYPE_WORK, RECORD_TYPE_OB):
            self.background_sync_with_cloud()
            QMessageBox.information(self, "Изменение сохранено", "Запись сохранена.")
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
        if shift and shift['entry_type'] in (RECORD_TYPE_WORK, RECORD_TYPE_OB):
            self.background_sync_with_cloud()
        elif shift:
            QMessageBox.information(self, "Удалено локально", "Запись удалена из десктопной базы. Сервер не поддерживает синхронизацию удаления АТФ/больничного.")

if __name__ == "__main__":
    try:
        app = QApplication(sys.argv)
        if os.path.exists("logo.ico"):
            app.setWindowIcon(QIcon("logo.ico"))
        window = SmartReportApp()
        window.show()
        sys.exit(app.exec())
    except Exception as e:
        print("\n--- ОШИБКА ---")
        traceback.print_exc()
        input("\nНажмите Enter...")