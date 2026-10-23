import sqlite3
import ast
import shutil
import tempfile
import os
import json
import hashlib
import hmac
import logging
import math
import csv
import io
import uuid
from pathlib import Path
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape
from flask import Flask, render_template, request, jsonify, session, make_response, send_file, redirect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from expense_documents import build_project_order_allocations, decimal_text, parse_decimal
from expense_documents import migrate_invoice_net_schema, invoice_net_schema_ready, net_invoice_issues, normalize_net_allocations, parse_invoice_money, invoice_context
from expense_documents import due_date_issues
from ob_calculator import calculate_work_interval
from api_auth import LoginThrottle, ensure_token_table, issue_token, lookup_token, revoke_token
from web_login_throttle import SQLiteLoginThrottle
from object_reports import register_object_reports
from customer_pricing import agreed_prices_for_date, ensure_pricing_schema, pricing_json, store_pricing, require_final_prices
from fixed_pricing import validate_fixed_storage
import payroll_history
from mail_intake import register_mail_intake
import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

# Настройка системного логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

SUPPORTED_SUBMISSION_TYPES = {"work", "atf", "sick", "vacation", "holiday", "ob"}
SPECIAL_SUBMISSION_OBJECTS = {
    "atf": ("Arbetstidsförkortning", "p1000"),
    "sick": ("Sjuk", "p1001"),
    "vacation": ("Semestertimmar", "p1002"),
    "holiday": (None, "p1004"),
}


def matches_special_submission_object(name, marking, reference):
    expected_name, expected_marking = reference
    return marking == expected_marking and (
        expected_name is None or name == expected_name
    )


def resolve_submission_type(data, default="work"):
    provided = [data[key] for key in ("submission_type", "record_type") if key in data]
    if len(provided) == 2 and provided[0] != provided[1]:
        raise ValueError("Поля submission_type и record_type не совпадают.")
    submission_type = str(provided[0] if provided else default).strip()
    if submission_type not in SUPPORTED_SUBMISSION_TYPES:
        raise ValueError("Неизвестный тип записи. Допустимы work, atf, sick, vacation, holiday, ob.")
    return submission_type


def ob_schema_ready(cursor):
    cursor.execute("PRAGMA table_info(incoming_submissions)")
    columns = {row[1] for row in cursor.fetchall()}
    required = {"start_time", "end_time", "ob_base_rate"}
    cursor.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ob_segments'")
    return required.issubset(columns) and cursor.fetchone() is not None


def normalize_employee_name(name):
    return " ".join(str(name or "").split()).casefold()


def find_employee_profiles(cursor, name, active_only=False):
    query = "SELECT id, name, salary_rate, is_active FROM meta_employees WHERE normalize_employee_name(name) = ?"
    params = [normalize_employee_name(name)]
    if active_only:
        query += " AND is_active = 1"
    cursor.execute(query, params)
    return cursor.fetchall()


def select_employee_profile(profiles, preferred_id=None, preferred_name=None):
    if not profiles:
        return None
    preferred = next((row for row in profiles if row["id"] == preferred_id), None)
    if preferred and float(preferred["salary_rate"] or 0.0) > 0:
        return preferred
    configured = [row for row in profiles if float(row["salary_rate"] or 0.0) > 0]
    candidates = configured or profiles
    exact = next((row for row in candidates if row["name"] == preferred_name), None)
    return exact or max(candidates, key=lambda row: row["id"])


def get_employee_ob_base_rate(cursor, employee):
    sql = """SELECT id, name, salary_rate, is_active
             FROM meta_employees
             WHERE normalize_employee_name(name) = ? AND is_active = 1"""
    normalized_name = normalize_employee_name(employee)
    logger.info(
        "OB salary lookup employee_name=%r normalized_name=%r SQL=%s params=%r",
        employee, normalized_name, sql, (normalized_name,),
    )
    cursor.execute(sql, (normalized_name,))
    profiles = cursor.fetchall()
    profile = select_employee_profile(profiles, preferred_name=employee)
    if not profile:
        logger.warning(
            "OB salary lookup found no active meta_employees row for employee_name=%r",
            employee,
        )
        raise ValueError("Для OB не найден активный профиль сотрудника")
    salary_rate = float(profile["salary_rate"] or 0.0)
    logger.info(
        "OB salary lookup result employee_name=%r profile_id=%s matched_name=%r salary_rate=%s",
        employee, profile["id"], profile["name"], salary_rate,
    )
    return salary_rate


def calculate_ob_payload(cursor, work_date, start_time, end_time, employee, existing=None,
                         company=None, object_name=None):
    if (existing and not existing["is_deleted"] and existing["submission_type"] == "ob"
            and (work_date, start_time, end_time, employee) == (
                existing["date"], existing["start_time"], existing["end_time"], existing["employee"])):
        segments = get_ob_segments(cursor, existing["sync_id"])
        if segments:
            return {
                "date": existing["date"], "start_time": existing["start_time"], "end_time": existing["end_time"],
                "ob_base_rate": existing["ob_base_rate"], "hours": existing["hours"],
                "rate": existing["rate"], "transport": existing["transport"], "segments": segments,
                "total_amount": round(sum(segment["amount"] for segment in segments), 2),
                "base_rate_configured": True, "preserved": True,
            }
    base_rate = get_employee_ob_base_rate(cursor, employee)
    if existing:
        company = company or existing["company"]
        object_name = object_name or existing["object_name"]
    result = payroll_history.entry_interval(cursor, employee, company, object_name,
                                             work_date, start_time, end_time, base_rate)
    base_rate = result["segments"][0]["base_rate"]
    return {
        "date": result["start_date"],
        "start_time": result["start_time"],
        "end_time": result["end_time"],
        "ob_base_rate": float(base_rate),
        "hours": result["total_hours"],
        "rate": 0.0,
        "transport": 0.0,
        "segments": result["segments"],
        "total_amount": result["total_amount"],
        "rules_version": result["rules_version"],
        "base_rate_configured": payroll_history.rate_known(cursor, employee, work_date, base_rate),
    }


def submission_transport(value, submission_type):
    try:
        transport = round(float(value), 2)
    except (TypeError, ValueError) as error:
        raise ValueError("Некорректное значение транспорта.") from error
    if not math.isfinite(transport) or not 0 <= transport <= 10000:
        raise ValueError("Транспорт должен быть конечным числом от 0 до 10000.")
    return transport if submission_type in ("work", "ob") else 0.0

def public_ob_result(result):
    public_segments = [
        {key: segment[key] for key in ("start_time", "end_time", "hours")}
        for segment in result["segments"]
    ]
    return {
        "date": result["date"],
        "start_time": result["start_time"],
        "end_time": result["end_time"],
        "hours": result["hours"],
        "total_hours": result["hours"],
        "segments": public_segments,
    }


def replace_ob_segments(cursor, shift_id, segments):
    cursor.execute("DELETE FROM ob_segments WHERE shift_id = ?", (shift_id,))
    now = datetime.now(timezone.utc).isoformat()
    cursor.executemany('''
        INSERT INTO ob_segments (
            shift_id, segment_date, start_time, end_time, hours, category,
            premium_percent, multiplier, base_rate, calculated_rate, amount,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', [(
        shift_id, segment["segment_date"], segment["start_time"], segment["end_time"],
        segment["hours"], segment["category"], segment["premium_percent"],
        segment["multiplier"], segment["base_rate"], segment["calculated_rate"],
        segment["amount"], now, now,
    ) for segment in segments])


def get_ob_segments(cursor, shift_id):
    cursor.execute("""
        SELECT segment_date, start_time, end_time, hours, category, premium_percent,
               multiplier, base_rate, calculated_rate, amount
        FROM ob_segments WHERE shift_id = ? ORDER BY segment_date, start_time, id
    """, (shift_id,))
    return [dict(row) for row in cursor.fetchall()]


def validate_submission_object(cursor, submission_type, company, object_name, allow_inactive=False):
    reference = SPECIAL_SUBMISSION_OBJECTS.get(submission_type)
    if not reference:
        cursor.execute(
            "SELECT id, name, markning FROM meta_objects WHERE name = ? AND company = ? "
            "AND (is_active = 1 OR ? = 1)",
            (object_name, company, int(allow_inactive)),
        )
    elif reference[0] is None:
        cursor.execute(
            "SELECT id, name, markning FROM meta_objects WHERE company = ? AND markning = ? "
            "AND (is_active = 1 OR ? = 1)",
            (company, reference[1], int(allow_inactive)),
        )
    else:
        cursor.execute(
            "SELECT id, name, markning FROM meta_objects WHERE name = ? AND company = ? AND markning = ? "
            "AND (is_active = 1 OR ? = 1)",
            (reference[0], company, reference[1], int(allow_inactive))
        )
    matches = cursor.fetchall()
    if len(matches) != 1:
        if reference and not matches:
            expected = f"{reference[0]} / {reference[1]}" if reference[0] else reference[1]
            raise ValueError(f"Для типа {submission_type} не найдена точная запись справочника {expected}.")
        if len(matches) > 1:
            raise ValueError("Запись объекта неоднозначна в справочнике.")
        raise ValueError("Объект не принадлежит выбранной фирме или не существует.")
    if reference and reference[0] is not None and object_name != reference[0]:
        raise ValueError(f"Для типа {submission_type} нужно выбрать объект {reference[0]}.")
    if reference and object_name != matches[0]["name"]:
        raise ValueError(f"Для типа {submission_type} нужно выбрать объект {matches[0]['name']}.")
    if not reference and any(
        matches_special_submission_object(
            matches[0]["name"], matches[0]["markning"], special_reference
        )
        for special_reference in SPECIAL_SUBMISSION_OBJECTS.values()
    ):
        raise ValueError("Справочный объект АТФ/больничного/отпуска/праздничных часов требует соответствующий тип.")
    return matches[0]['id']


def infer_submission_type_from_object(cursor, company, object_name):
    cursor.execute(
        "SELECT name, markning FROM meta_objects WHERE company = ? AND name = ? AND is_active = 1",
        (company, object_name)
    )
    matches = cursor.fetchall()
    if len(matches) > 1:
        raise ValueError("Объект неоднозначен; требуется проверить название и отметку справочника.")
    if matches:
        for submission_type, reference in SPECIAL_SUBMISSION_OBJECTS.items():
            if matches_special_submission_object(
                matches[0]['name'], matches[0]['markning'], reference
            ):
                return submission_type
    return "work"


def resolve_session_employee(cursor, requested_employee=None):
    if session.get('role') == 'admin':
        employee = str(requested_employee or '').strip()
        if not employee:
            raise ValueError("Не указан сотрудник.")
        profiles = find_employee_profiles(cursor, employee, active_only=True)
        profile = select_employee_profile(profiles, preferred_name=employee)
        if not profile:
            raise ValueError("Указанный сотрудник не найден или неактивен.")
        employee = profile['name']
    else:
        employee_name = session.get('username')
        profiles = find_employee_profiles(cursor, employee_name, active_only=True) if employee_name else []
        profile = select_employee_profile(
            profiles,
            preferred_id=session.get('employee_id'),
            preferred_name=employee_name,
        )
        if not profile:
            employee_id = session.get('employee_id')
            if not employee_id:
                raise PermissionError("Доступ запрещен.")
            cursor.execute(
                "SELECT id, name, salary_rate, is_active FROM meta_employees WHERE id = ? AND is_active = 1",
                (employee_id,),
            )
            profile = cursor.fetchone()
        if not profile:
            raise PermissionError("Сотрудник не найден или деактивирован.")
        employee = profile['name']
        session['employee_id'] = profile['id']
        if requested_employee and normalize_employee_name(requested_employee) != normalize_employee_name(employee):
            raise PermissionError("Нельзя изменять записи другого сотрудника.")
    return employee

app = Flask(__name__)
app.permanent_session_lifetime = timedelta(hours=8)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    logger.critical("КРИТИЧЕСКАЯ ОШИБКА: Переменная окружения SECRET_KEY не задана!")
    raise RuntimeError("SECRET_KEY environment variable is required.")
app.secret_key = SECRET_KEY

SYNC_API_KEY = os.environ.get('SYNC_API_KEY')
if not SYNC_API_KEY:
    logger.critical("КРИТИЧЕСКАЯ ОШИБКА: Переменная окружения SYNC_API_KEY не задана!")
    raise RuntimeError("SYNC_API_KEY environment variable is required.")

def configure_session_security(application, environment=None):
    """HTTPS by default; local HTTP explicitly sets USE_SECURE_COOKIES=false.

    TRUSTED_PROXY_HOPS is opt-in: the server must only be reachable through that
    many trusted proxies, which overwrite forwarded headers from clients.
    """
    environment = os.environ if environment is None else environment
    secure_value = environment.get("USE_SECURE_COOKIES", "true").strip().lower()
    if secure_value not in {"true", "false"}:
        raise ValueError("USE_SECURE_COOKIES must be true or false")
    proxy_hops = int(environment.get("TRUSTED_PROXY_HOPS", "0"))
    if proxy_hops < 0:
        raise ValueError("TRUSTED_PROXY_HOPS must be nonnegative")
    application.config.update(
        SESSION_COOKIE_SECURE=secure_value == "true",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if proxy_hops:
        application.wsgi_app = ProxyFix(
            application.wsgi_app, x_for=proxy_hops, x_proto=proxy_hops,
            x_host=0, x_port=0, x_prefix=0,
        )

    @application.after_request
    def secure_transport_headers(response):
        if application.config["SESSION_COOKIE_SECURE"] and request.is_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response


configure_session_security(app)

DB_FILE = os.environ.get('DATABASE_PATH', "cloud_database.db")

register_object_reports(app, lambda: DB_FILE)

mail_intake_store = register_mail_intake(app, review_authorized=lambda: session.get("role") == "admin")

def get_db_connection():
    if os.path.exists(DB_FILE) and os.path.isdir(DB_FILE):
        try:
            os.rmdir(DB_FILE)
        except Exception:
            pass

    db_dir = os.path.dirname(os.path.abspath(DB_FILE))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA foreign_keys = ON;')
    conn.create_function("normalize_employee_name", 1, normalize_employee_name, deterministic=True)
    conn.row_factory = sqlite3.Row
    return conn

def create_database_backup():
    """Создание резервной копии базы данных при запуске сервера"""
    try:
        if not os.path.exists(DB_FILE):
            return

        backup_dir = os.path.join(os.path.dirname(os.path.abspath(DB_FILE)), "backups")
        os.makedirs(backup_dir, exist_ok=True)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        backup_path = os.path.join(backup_dir, f"backup_{timestamp}.db")

        source = sqlite3.connect(f"file:{os.path.abspath(DB_FILE)}?mode=ro", uri=True, timeout=30.0)
        destination = sqlite3.connect(backup_path, timeout=30.0)
        try:
            source.backup(destination)
            integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise sqlite3.DatabaseError(f"Backup integrity check failed: {integrity}")
        finally:
            destination.close()
            source.close()

        logger.info(f"Резервная копия базы данных успешно создана: {backup_path}")

        backups = sorted([
            os.path.join(backup_dir, f) for f in os.listdir(backup_dir)
            if f.startswith('backup_') and f.endswith('.db')
        ])
        if len(backups) > 10:
            for old_backup in backups[:-10]:
                try:
                    os.remove(old_backup)
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Не удалось создать резервную копию базы данных: {e}")
        raise

def next_change_sequence(cursor):
    cursor.execute("UPDATE sync_metadata SET value = value + 1 WHERE name = 'change_sequence'")
    cursor.execute("SELECT value FROM sync_metadata WHERE name = 'change_sequence'")
    return cursor.fetchone()[0]

def safe_parse_obj(val):
    if not val:
        return "", "", "Privat"
    if isinstance(val, dict):
        name = str(val.get('name') or val.get('title') or val.get('namn') or '').strip()
        mark = str(val.get('markning') or val.get('mark') or '').strip()
        comp = str(val.get('company') or val.get('customer') or 'Privat').strip()
        return name, mark, comp
    
    val_str = str(val).strip()
    if "{" in val_str and ("'name'" in val_str or "'title'" in val_str):
        try:
            d = ast.literal_eval(val_str)
            if isinstance(d, dict):
                name = str(d.get('name') or d.get('title') or '').strip()
                mark = str(d.get('markning') or d.get('mark') or '').strip()
                comp = str(d.get('company') or d.get('customer') or 'Privat').strip()
                return name, mark, comp
        except Exception:
            pass
    return val_str, "", "Privat"

def init_cloud_db():
    create_database_backup()

    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            role TEXT NOT NULL DEFAULT 'user',
            employee_id INTEGER,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cloud_shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT UNIQUE,
            date TEXT NOT NULL,
            employee TEXT NOT NULL,
            company TEXT NOT NULL,
            object_name TEXT NOT NULL,
            hours REAL NOT NULL,
            rate REAL NOT NULL,
            transport REAL DEFAULT 0.0,
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            synced INTEGER DEFAULT 0
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sync_metadata (
            name TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        )
    ''')
    cursor.execute("INSERT OR IGNORE INTO sync_metadata (name, value) VALUES ('change_sequence', 0)")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS incoming_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            sync_id TEXT NOT NULL UNIQUE,
            submission_type TEXT NOT NULL DEFAULT 'work',
            date TEXT NOT NULL,
            employee TEXT NOT NULL,
            company TEXT NOT NULL,
            object_name TEXT NOT NULL,
            hours REAL NOT NULL,
            rate REAL NOT NULL DEFAULT 0,
            transport REAL NOT NULL DEFAULT 0,
            comment TEXT,
            invoiced INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1,
            change_seq INTEGER NOT NULL DEFAULT 0,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            desktop_acked INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'web',
            legacy_cloud_id INTEGER UNIQUE,
            last_operation_id TEXT,
            payload_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_incoming_employee_date ON incoming_submissions(employee, date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_incoming_change_seq ON incoming_submissions(change_seq)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_incoming_pending ON incoming_submissions(desktop_acked, source, is_deleted)")
    ensure_pricing_schema(cursor, "incoming_submissions")
    payroll_history.ensure_schema(cursor)
    cursor.execute("PRAGMA table_info(incoming_submissions)")
    submission_columns = {column[1] for column in cursor.fetchall()}
    if 'submission_type' not in submission_columns:
        cursor.execute("ALTER TABLE incoming_submissions ADD COLUMN submission_type TEXT NOT NULL DEFAULT 'work'")
    if 'invoiced' not in submission_columns:
        cursor.execute("ALTER TABLE incoming_submissions ADD COLUMN invoiced INTEGER NOT NULL DEFAULT 0")
    
    cursor.execute("PRAGMA table_info(cloud_shifts)")
    shift_cols = [c[1] for c in cursor.fetchall()]
    if 'request_id' not in shift_cols:
        cursor.execute("ALTER TABLE cloud_shifts ADD COLUMN request_id TEXT")
    try:
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cloud_shifts_request_id ON cloud_shifts(request_id) WHERE request_id IS NOT NULL")
    except sqlite3.IntegrityError:
        logger.warning("В legacy cloud_shifts найдены повторяющиеся request_id; старые строки сохранены без уникального индекса.")

    cursor.execute("SELECT id, request_id, date, employee, company, object_name, hours, rate, transport, comment, synced, created_at FROM cloud_shifts")
    legacy_rows = cursor.fetchall()
    for legacy in legacy_rows:
        legacy_request_id = legacy['request_id'] or f"legacy-cloud:{legacy['id']}"
        cursor.execute("SELECT id FROM incoming_submissions WHERE legacy_cloud_id = ? OR request_id = ?", (legacy['id'], legacy_request_id))
        if cursor.fetchone():
            continue
        sequence = next_change_sequence(cursor)
        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        payload = {
            "date": legacy['date'], "employee": legacy['employee'], "company": legacy['company'],
            "object_name": legacy['object_name'], "hours": legacy['hours'], "rate": legacy['rate'],
            "transport": legacy['transport'], "comment": legacy['comment'] or ""
        }
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()
        cursor.execute('''
            INSERT INTO incoming_submissions (
                request_id, sync_id, submission_type, date, employee, company, object_name,
                hours, rate, transport, comment, version, change_seq, desktop_acked,
                source, legacy_cloud_id, payload_hash, created_at, updated_at
            ) VALUES (?, ?, 'work', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'legacy', ?, ?, ?, ?)
        ''', (
            legacy_request_id, legacy_request_id, legacy['date'], legacy['employee'], legacy['company'],
            legacy['object_name'], legacy['hours'], legacy['rate'], legacy['transport'], legacy['comment'],
            sequence, int(bool(legacy['synced'])), legacy['id'], payload_hash,
            legacy['created_at'] or now, now
        ))

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meta_employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            salary_rate REAL DEFAULT 0.0,
            is_active INTEGER NOT NULL DEFAULT 1
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS employee_sync_events (
            operation_id TEXT PRIMARY KEY,
            payload_hash TEXT NOT NULL,
            employee_key TEXT NOT NULL,
            revision INTEGER NOT NULL,
            state_hash TEXT,
            operation TEXT NOT NULL,
            profile_id INTEGER,
            created_at TEXT NOT NULL
        )
    ''')
    cursor.execute("PRAGMA table_info(employee_sync_events)")
    employee_event_columns = {column[1] for column in cursor.fetchall()}
    if "state_hash" not in employee_event_columns:
        cursor.execute("ALTER TABLE employee_sync_events ADD COLUMN state_hash TEXT")
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meta_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meta_objects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sync_id TEXT,
            name TEXT NOT NULL,
            markning TEXT,
            company TEXT,
            rate REAL NOT NULL DEFAULT 0.0,
            transport_rate REAL NOT NULL DEFAULT 0.0,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
            sync_version INTEGER NOT NULL DEFAULT 0,
            UNIQUE(name, company)
        )
    ''')
    cursor.execute("PRAGMA table_info(meta_objects)")
    object_columns = {column[1] for column in cursor.fetchall()}
    for column_name, column_sql in (
        ("sync_id", "TEXT"),
        ("rate", "REAL NOT NULL DEFAULT 0.0"),
        ("transport_rate", "REAL NOT NULL DEFAULT 0.0"),
        ("is_active", "INTEGER NOT NULL DEFAULT 1"),
        ("sync_version", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column_name not in object_columns:
            cursor.execute(f"ALTER TABLE meta_objects ADD COLUMN {column_name} {column_sql}")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_meta_objects_sync_id ON meta_objects(sync_id) WHERE sync_id IS NOT NULL")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS object_sync_events (
            operation_id TEXT PRIMARY KEY,
            payload_hash TEXT NOT NULL,
            sync_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            state_hash TEXT,
            operation TEXT NOT NULL,
            server_object_id INTEGER,
            created_at TEXT NOT NULL
        )
    ''')
    cursor.execute("PRAGMA table_info(object_sync_events)")
    event_columns = {column[1] for column in cursor.fetchall()}
    if "revision" not in event_columns:
        cursor.execute("ALTER TABLE object_sync_events ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
    if "state_hash" not in event_columns:
        cursor.execute("ALTER TABLE object_sync_events ADD COLUMN state_hash TEXT")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS expense_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT NOT NULL UNIQUE,
            supplier_name TEXT NOT NULL DEFAULT '',
            invoice_number TEXT NOT NULL DEFAULT '',
            payment_reference TEXT,
            document_date TEXT,
            due_date TEXT,
            due_date_kind TEXT NOT NULL DEFAULT 'absent',
            due_date_review_required INTEGER NOT NULL DEFAULT 0,
            due_date_reviewed INTEGER NOT NULL DEFAULT 0,
            due_date_evidence_json TEXT NOT NULL DEFAULT '[]',
            currency_code TEXT NOT NULL DEFAULT 'SEK',
            net_amount TEXT,
            vat_amount TEXT,
            gross_amount TEXT,
            expense_category TEXT NOT NULL DEFAULT 'Прочие расходы',
            document_comment TEXT NOT NULL DEFAULT '',
            order_proposals_json TEXT NOT NULL DEFAULT '[]',
            verification_status TEXT NOT NULL DEFAULT 'draft'
                CHECK (verification_status IN ('draft','requires_review','confirmed')),
            recognized_text TEXT NOT NULL DEFAULT '',
            original_name TEXT,
            stored_name TEXT,
            file_sha256 TEXT,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            deleted_at TEXT,
            sync_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    cursor.execute("PRAGMA table_info(expense_documents)")
    expense_document_columns = {column[1] for column in cursor.fetchall()}
    for column_name, column_sql in (
        ("is_deleted", "INTEGER NOT NULL DEFAULT 0"),
        ("deleted_at", "TEXT"),
        ("sync_version", "INTEGER NOT NULL DEFAULT 1"),
        ("order_proposals_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("payment_reference", "TEXT"),
        ("due_date", "TEXT"),
        ("due_date_kind", "TEXT NOT NULL DEFAULT 'absent'"),
        ("due_date_review_required", "INTEGER NOT NULL DEFAULT 0"),
        ("due_date_reviewed", "INTEGER NOT NULL DEFAULT 0"),
        ("due_date_evidence_json", "TEXT NOT NULL DEFAULT '[]'"),
    ):
        if column_name not in expense_document_columns:
            cursor.execute(f"ALTER TABLE expense_documents ADD COLUMN {column_name} {column_sql}")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS expense_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES expense_documents(id) ON DELETE CASCADE,
            object_id INTEGER REFERENCES meta_objects(id) ON DELETE RESTRICT,
            allocated_gross TEXT NOT NULL,
            allocated_net TEXT,
            allocated_vat TEXT,
            allocation_note TEXT NOT NULL DEFAULT ''
        )
    ''')
    cursor.execute("PRAGMA table_info(expense_allocations)")
    allocation_columns = {column[1]: column for column in cursor.fetchall()}
    if allocation_columns and allocation_columns["object_id"][3]:
        cursor.execute("ALTER TABLE expense_allocations RENAME TO expense_allocations_before_company_costs")
        cursor.execute('''
            CREATE TABLE expense_allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES expense_documents(id) ON DELETE CASCADE,
                object_id INTEGER REFERENCES meta_objects(id) ON DELETE RESTRICT,
                allocated_gross TEXT NOT NULL,
                allocated_net TEXT,
                allocated_vat TEXT,
                allocation_note TEXT NOT NULL DEFAULT ''
            )
        ''')
        cursor.execute('''
            INSERT INTO expense_allocations
                (id, document_id, object_id, allocated_gross, allocated_net, allocated_vat, allocation_note)
            SELECT id, document_id, object_id, allocated_gross, allocated_net, allocated_vat, allocation_note
            FROM expense_allocations_before_company_costs
        ''')
        cursor.execute("DROP TABLE expense_allocations_before_company_costs")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_expense_allocations_object ON expense_allocations(object_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_expense_documents_date ON expense_documents(document_date)")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_expense_documents_file_hash ON expense_documents(file_sha256) WHERE file_sha256 IS NOT NULL")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS expense_document_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES expense_documents(id) ON DELETE CASCADE,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL UNIQUE,
            file_sha256 TEXT NOT NULL UNIQUE,
            ordinal INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        INSERT OR IGNORE INTO expense_document_attachments
            (document_id, original_name, stored_name, file_sha256, ordinal, created_at)
        SELECT id, COALESCE(original_name, stored_name), stored_name, file_sha256, 0, created_at
        FROM expense_documents
        WHERE stored_name IS NOT NULL AND file_sha256 IS NOT NULL
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS expense_project_code_links (
            supplier_key TEXT NOT NULL,
            supplier_name TEXT NOT NULL,
            project_code_key TEXT NOT NULL,
            project_code TEXT NOT NULL,
            object_id INTEGER NOT NULL REFERENCES meta_objects(id) ON DELETE RESTRICT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (supplier_key, project_code_key)
        )
    ''')

    cursor.execute("SELECT COUNT(*) FROM meta_employees")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT OR IGNORE INTO meta_employees (id, name, salary_rate, is_active) VALUES (?, ?, ?, ?)", (1, "Aliaksei Patonich", 0.0, 1))

    cursor.execute("SELECT COUNT(*) FROM meta_companies")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT OR IGNORE INTO meta_companies (name) VALUES (?)", ("Privat",))

    cursor.execute("SELECT COUNT(*) FROM meta_objects")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT OR IGNORE INTO meta_objects (name, markning, company) VALUES (?, ?, ?)", ("Bygg och renovering", "Sthlm", "Privat"))

    admin_user = os.environ.get('ADMIN_USERNAME', 'admin')
    admin_pass = os.environ.get('ADMIN_PASSWORD')
    if not admin_pass:
        logger.critical("КРИТИЧЕСКАЯ ОШИБКА: Переменная окружения ADMIN_PASSWORD обязательна!")
        raise RuntimeError("ADMIN_PASSWORD environment variable is required.")
        
    now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")
    if cursor.fetchone()[0] == 0:
        hashed_pw = generate_password_hash(admin_pass)
        cursor.execute('''
            INSERT INTO users (username, password_hash, role, employee_id, is_active, created_at)
            VALUES (?, ?, 'admin', NULL, 1, ?)
        ''', (admin_user, hashed_pw, now_str))

    cursor.execute("SELECT id, name FROM meta_employees WHERE is_active = 1")
    active_emps = cursor.fetchall()
    for emp in active_emps:
        cursor.execute("SELECT id FROM users WHERE username = ?", (emp['name'],))
        existing_user = cursor.fetchone()
        if not existing_user:
            cursor.execute('''
                INSERT INTO users (username, role, employee_id, is_active, created_at)
                VALUES (?, 'user', ?, 1, ?)
            ''', (emp['name'], emp['id'], now_str))
        else:
            cursor.execute("UPDATE users SET employee_id = COALESCE(employee_id, ?) WHERE username = ?", (emp['id'], emp['name']))

    ensure_token_table(cursor)
    cursor.execute("""CREATE TABLE IF NOT EXISTS expense_processing (
        document_id INTEGER PRIMARY KEY REFERENCES expense_documents(id),
        state TEXT NOT NULL, details_json TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")

    conn.commit()
    conn.close()

    # Перестройка таблицы распределений выполняется только по явному флагу; резервная копия создана выше.
    if os.environ.get('SMARTREPORT_UPGRADE_NET_SCHEMA') == '1':
        upgrade_connection = sqlite3.connect(DB_FILE, timeout=30.0, isolation_level=None)
        try:
            upgrade_connection.execute("PRAGMA foreign_keys=ON")
            migrate_invoice_net_schema(upgrade_connection)
        finally:
            upgrade_connection.close()

init_cloud_db()

@app.before_request
def enforce_dataset_boundary():
    from sync_dataset import HEADER, file_dataset
    expected = file_dataset(DB_FILE)
    if not expected:
        return None
    guarded = (
        request.path.startswith(("/sync/", "/api/sync/", "/api/expense-", "/admin/"))
        or request.path in {"/sync-desktop-data", "/get-unsynced", "/mark-synced"}
    )
    if guarded:
        supplied = request.headers.get(HEADER)
        # Browser expense operations use a new session in the prepared dataset.
        if not supplied and session.get("role") == "admin":
            supplied = session.get("dataset_id")
        if supplied != expected:
            return jsonify({
                "ok": False, "error": "Прежний или другой набор данных. Требуется согласованный переход.",
                "code": "dataset_mismatch",
            }), 409
    if (session.get("user_id") and session.get("dataset_id") != expected
            and request.path not in {"/login", "/logout"}):
        session.clear()
        return jsonify({"ok": False, "error": "После перехода выполните вход заново."}), 401
    return None


@app.after_request
def advertise_dataset_boundary(response):
    from sync_dataset import HEADER, file_dataset
    expected = file_dataset(DB_FILE)
    if expected:
        response.headers[HEADER] = expected
    return response


@app.before_request
def enforce_active_session():
    user_id = session.get("user_id")
    if not user_id:
        return None
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, username, role, employee_id, is_active FROM users WHERE id = ?",
            (user_id,),
        )
        user = cursor.fetchone()
        if user is None or not user["is_active"]:
            session.clear()
            return None
        session["username"] = user["username"]
        session["role"] = user["role"]
        session["employee_id"] = user["employee_id"]
        if user["role"] != "admin":
            if user["employee_id"]:
                cursor.execute(
                    "SELECT id FROM meta_employees WHERE id = ? AND is_active = 1",
                    (user["employee_id"],),
                )
                active_profile = cursor.fetchone()
            else:
                active_profile = select_employee_profile(
                    find_employee_profiles(cursor, user["username"], active_only=True),
                    preferred_name=user["username"],
                )
            if active_profile is None:
                session.clear()
    finally:
        conn.close()
    return None

def bearer_admin_user():
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        return None
    conn = get_db_connection()
    try:
        user = lookup_token(conn, header[7:].strip())
    finally:
        conn.close()
    return user if user and user['role'] == 'admin' else None

def verify_sync_key():
    sync_key = request.headers.get('X-Sync-Key') or request.headers.get('X-API-Key')
    if sync_key and hmac.compare_digest(sync_key, SYNC_API_KEY):
        return True
    if not sync_key and bearer_admin_user() is not None:
        return True
    logger.warning(f"Несанкционированная попытка доступа с IP: {request.remote_addr}")
    return False


@app.route("/sync/payroll", methods=["GET", "POST"])
def payroll_history_api():
    if not verify_sync_key():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    conn = get_db_connection()
    try:
        if request.method == "GET":
            return jsonify({"ok": True, "events": payroll_history.events(conn.cursor()),
                            "writes_enabled": payroll_history.WRITES_ENABLED})
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("Ожидается объект запроса.")
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        if data.get("kind") == "zone":
            if not cursor.execute("SELECT 1 FROM meta_objects WHERE company=? AND name=?",
                                  (data.get("company"), data.get("object_name"))).fetchone():
                raise ValueError("Объект не найден.")
        elif not cursor.execute("SELECT 1 FROM meta_employees WHERE name=?",
                                (data.get("employee"),)).fetchone():
            raise ValueError("Сотрудник не найден.")
        if data.get("kind") == "preview":
            draft = payroll_history.preview(cursor, "incoming_submissions", payroll_history.events(cursor),
                                            data["employee"], data["date_from"], data["date_to"],
                                            data["parameters"], data.get("overrides"),
                                            bool(data.get("reprice")))
            return jsonify({"ok": True, "preview": draft, "events": payroll_history.events(cursor)})
        admin = bearer_admin_user()
        result = payroll_history.apply_action(cursor, "incoming_submissions", data,
                                               admin["username"] if admin else "sync-admin")
        conn.commit()
        return jsonify({"ok": True, "result": result, "events": payroll_history.events(cursor)})
    except (ValueError, KeyError, TypeError) as error:
        conn.rollback()
        return jsonify({"ok": False, "error": str(error)}), 400
    except Exception:
        conn.rollback()
        logger.exception("Ошибка зарплатной истории")
        return jsonify({"ok": False, "error": "Зарплатная операция не подтверждена; ошибка сервера."}), 500
    finally:
        conn.close()


auth_throttle = LoginThrottle()
web_login_throttle = SQLiteLoginThrottle(lambda: get_db_connection())

@app.route('/api/auth/token', methods=['POST'])
def api_auth_token():
    data = request.get_json(silent=True) or {}
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))
    if not username or not password or len(username) > 100 or len(password) > 200:
        return jsonify({"ok": False, "error": "Укажите логин и пароль."}), 400
    throttle_key = username.lower()
    if auth_throttle.blocked(throttle_key):
        return jsonify({"ok": False, "error": "Слишком много неудачных попыток. Повторите через несколько минут."}), 429
    conn = get_db_connection()
    try:
        user = conn.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (username,)).fetchone()
        stored_hash = (user['password_hash'] if user else None) or generate_password_hash('invalid-user-placeholder')
        password_ok = check_password_hash(stored_hash, password)
        if not user or not user['password_hash'] or not password_ok or user['role'] != 'admin':
            auth_throttle.record_failure(throttle_key)
            return jsonify({"ok": False, "error": "Неверный логин или пароль."}), 401
        auth_throttle.reset(throttle_key)
        token, expires_at = issue_token(conn, user['id'], data.get('device_name', ''))
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), user['id']))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "token": token, "expires_at": expires_at,
                    "user": {"username": user['username'], "role": user['role']}})

@app.route('/api/auth/me', methods=['GET'])
def api_auth_me():
    user = bearer_admin_user()
    if user is None:
        return jsonify({"ok": False, "error": "Не авторизован"}), 401
    return jsonify({"ok": True, "user": {"username": user['username'], "role": user['role']}})

@app.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    header = request.headers.get('Authorization', '')
    token = header[7:].strip() if header.startswith('Bearer ') else ''
    conn = get_db_connection()
    try:
        revoked = revoke_token(conn, token)
    finally:
        conn.close()
    return jsonify({"ok": True, "revoked": revoked})


EXPENSE_DOCUMENT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
EXPENSE_DOCUMENT_STATUSES = {"draft", "requires_review", "confirmed"}


def expense_api_authorized():
    if session.get("role") == "admin":
        if request.method in {"POST", "PUT", "DELETE"}:
            supplied = request.headers.get("X-CSRF-Token", "")
            expected = str(session.get("expense_csrf_token") or "")
            return bool(expected and supplied and hmac.compare_digest(supplied, expected))
        return True
    return verify_sync_key()


def expense_documents_root():
    configured = os.environ.get("SMARTREPORT_DOCUMENTS_DIR", "").strip()
    if not configured:
        raise RuntimeError("SMARTREPORT_DOCUMENTS_DIR должен указывать на постоянное хранилище документов.")
    root = Path(configured).expanduser()
    if not root.is_absolute():
        raise ValueError("SMARTREPORT_DOCUMENTS_DIR должен быть абсолютным путём.")
    return root.resolve()


def expense_request_payload():
    if request.is_json:
        payload = request.get_json(silent=True)
    else:
        raw_payload = request.form.get("payload")
        payload = json.loads(raw_payload) if raw_payload else request.form.to_dict()
    if not isinstance(payload, dict):
        raise ValueError("Ожидается объект с полями документа.")
    return payload


def expense_due_date_schema_ready(connection):
    columns = {row[1] for row in connection.execute("PRAGMA table_info(expense_documents)")}
    return {"due_date", "due_date_kind", "due_date_review_required", "due_date_reviewed", "due_date_evidence_json"}.issubset(columns)


def validate_expense_fields(payload):
    document_type = payload.get("document_type") or "invoice"
    object_cost = payload.get("usage") == "object_cost"
    # A saved or user-supplied invoice label cannot override current reminder classification.
    from mail_import import classify_mail_document
    safety = {"document_type": document_type, "raw_text": payload.get("recognized_text") or "",
              "invoice_number": payload.get("invoice_number"), "net_amount": payload.get("net_amount")}
    classify_mail_document(safety)
    if safety.get("suggested_document_type") == "payment_reminder":
        raise ValueError("Напоминание об оплате нельзя переносить как новую фактуру или чек.")
    reference = payload.get("payment_reference")
    if reference is not None and (not isinstance(reference, str) or len(reference) > 200):
        raise ValueError("OCR/reference должен быть строкой не длиннее 200 символов.")
    reference = reference.strip() if reference is not None else None
    status = str(payload.get("verification_status") or "draft")
    if status not in EXPENSE_DOCUMENT_STATUSES:
        raise ValueError("Недопустимый статус документа.")
    context = invoice_context(str(payload.get("recognized_text") or ""))
    if status == "confirmed" and context["prepaid"] and not object_cost:
        raise ValueError("Предоплаченный чек: подтверждение пока не поддерживается; сохраните черновик, не заменяя остаток общей суммой.")
    if status == "confirmed" and context["reverse_charge"] and not object_cost and payload.get("tax_reviewed") is not True:
        raise ValueError("Обратное начисление НДС требует явной сверки по оригиналу.")

    document_date = str(payload.get("document_date") or "").strip() or None
    if document_date:
        try:
            datetime.strptime(document_date, "%Y-%m-%d")
        except ValueError as error:
            raise ValueError("Дата документа должна быть в формате YYYY-MM-DD.") from error
    due_date = str(payload.get("due_date") or "").strip() or None
    if due_date:
        try:
            due_date = datetime.strptime(due_date, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError as error:
            raise ValueError("Срок оплаты должен быть корректной датой ГГГГ-ММ-ДД.") from error
    due_kind = str(payload.get("due_date_kind") or ("manual" if due_date else "absent"))
    if due_kind not in {"explicit", "calculated", "manual", "ambiguous", "absent"}:
        raise ValueError("Неизвестный источник срока оплаты.")
    due_required = payload.get("due_date_review_required") is True or due_kind in {"calculated", "ambiguous"}
    due_reviewed = payload.get("due_date_reviewed") is True
    due_evidence = payload.get("due_date_evidence") or []
    if (not isinstance(due_evidence, list) or len(due_evidence) > 50
            or any(not isinstance(item, dict) for item in due_evidence)
            or len(json.dumps(due_evidence, ensure_ascii=False)) > 20000):
        raise ValueError("Некорректные свидетельства срока оплаты.")
    if status == "confirmed" and not object_cost:
        date_issues = due_date_issues({"due_date": due_date, "due_date_kind": due_kind,
                                      "due_date_review_required": due_required, "due_date_reviewed": due_reviewed})
        if date_issues:
            raise ValueError(" ".join(date_issues))

    amount_values = {}
    net_basis = payload.get("allocation_basis") == "net"
    for key in (("net_amount", "vat_amount", "gross_amount", "rounding_amount", "amount_due") if net_basis else ("vat_amount", "gross_amount")):
        raw_value = payload.get(key)
        value = parse_invoice_money(raw_value) if net_basis else parse_decimal(raw_value)
        if raw_value not in (None, "") and value is None:
            raise ValueError(f"Некорректная сумма: {key}.")
        amount_values[key] = Decimal(decimal_text(value)) if value is not None else None
    if not net_basis:
        amount_values["net_amount"] = (
        amount_values["gross_amount"] - amount_values["vat_amount"]
        if amount_values["gross_amount"] is not None and amount_values["vat_amount"] is not None
        else None
        )
        if object_cost and payload.get("net_amount") not in (None, "") and payload.get("rounding_amount") not in (None, ""):
            net = parse_invoice_money(payload["net_amount"])
            rounding = parse_invoice_money(payload["rounding_amount"])
            if (net is None or rounding is None or amount_values["vat_amount"] is None
                    or net + amount_values["vat_amount"] + rounding != amount_values["gross_amount"]):
                raise ValueError("Нетто + НДС + указанное округление не равны сумме расхода.")
            amount_values.update(net_amount=net, rounding_amount=rounding)
    elif amount_values["net_amount"] is not None and amount_values["vat_amount"] is not None:
        expected_gross = amount_values["net_amount"] + amount_values["vat_amount"]
        if amount_values["gross_amount"] is not None and amount_values["gross_amount"] != expected_gross:
            raise ValueError("Сумма с НДС до округления не равна сумме без НДС плюс НДС.")
        amount_values["gross_amount"] = expected_gross

    supplier_name = str(payload.get("supplier_name") or "").strip()
    invoice_number = str(payload.get("invoice_number") or "").strip()
    currency_code = str(payload.get("currency_code") or "SEK").strip().upper()
    if len(supplier_name) > 250 or len(invoice_number) > 120:
        raise ValueError("Поставщик или номер документа слишком длинный.")
    if currency_code != "UNKNOWN" and (len(currency_code) != 3 or not currency_code.isalpha()):
        raise ValueError("Укажите код валюты из трёх букв.")
    if len(str(payload.get("document_comment") or "")) > 4000:
        raise ValueError("Комментарий документа слишком длинный.")

    if status == "confirmed":
        if not supplier_name or not document_date or amount_values["gross_amount"] is None:
            raise ValueError("Для подтверждения заполните поставщика, дату и сумму с НДС.")
        if currency_code == "UNKNOWN":
            raise ValueError("Для подтверждения выберите валюту документа.")

    expense_category = str(payload.get("expense_category") or "Прочие расходы").strip()[:100]
    is_transfer_receipt = expense_category.casefold() == "квитанция перевода"
    if is_transfer_receipt and status == "confirmed":
        raise ValueError("Квитанцию перевода нельзя подтвердить как фактуру; сохраните её с проверкой.")
    if is_transfer_receipt and status == "draft":
        status = "requires_review"

    return {
        "document_type": document_type,
        "usage": "object_cost" if object_cost else None,
        "supplier_name": supplier_name,
        "invoice_number": invoice_number,
        "payment_reference": reference or None,
        "payment_reference_provided": "payment_reference" in payload,
        "document_date": document_date,
        "due_date": due_date,
        "due_date_kind": due_kind,
        "due_date_review_required": due_required,
        "due_date_reviewed": due_reviewed,
        "due_date_evidence": due_evidence,
        "due_date_provided": any(key in payload for key in ("due_date", "due_date_kind", "due_date_review_required", "due_date_reviewed", "due_date_evidence")),
        "currency_code": currency_code,
        "net_amount": decimal_text(amount_values["net_amount"]),
        "vat_amount": decimal_text(amount_values["vat_amount"]),
        "gross_amount": decimal_text(amount_values["gross_amount"]),
        "expense_category": expense_category,
        "document_comment": str(payload.get("document_comment") or "").strip(),
        "verification_status": status,
        "recognized_text": str(payload.get("recognized_text") or "")[:100000],
        "tax_reviewed": payload.get("tax_reviewed") is True,
        "allocation_basis": "net" if net_basis else "gross",
        "rounding_amount": decimal_text(amount_values.get("rounding_amount")),
        "amount_due": decimal_text(amount_values.get("amount_due")),
    }


def resolve_expense_allocations(cursor, raw_allocations, fields):
    if fields.get("due_date_provided") and not expense_due_date_schema_ready(cursor):
        raise ValueError("Сервер не поддерживает сохранение срока оплаты; обновление схемы требует отдельного согласования.")
    if not isinstance(raw_allocations, list) or len(raw_allocations) > 100:
        raise ValueError("Распределение должно быть списком не более чем из 100 строк.")
    if fields.get("allocation_basis") == "net":
        if not invoice_net_schema_ready(cursor):
            raise ValueError("Схема распределения без НДС ещё не согласована и не применена; очередь сохранена.")
        for item in raw_allocations:
            if not isinstance(item, dict):
                raise ValueError("Некорректная строка распределения.")
            destination = item.get("destination") or "unselected"
            if destination not in {"object", "company", "unselected"}:
                raise ValueError("Некорректное назначение расходов.")
            for key in ("net_amount", "vat_amount"):
                if item.get(key) not in (None, "") and parse_invoice_money(item[key]) is None:
                    raise ValueError("Некорректная сумма распределения.")
            if destination == "object" and item.get("object_id") is not None:
                if cursor.execute("SELECT 1 FROM meta_objects WHERE id = ? AND is_active = 1", (item["object_id"],)).fetchone() is None:
                    raise ValueError("Активный объект не найден в серверном справочнике.")
        issues = net_invoice_issues(fields, raw_allocations)
        if fields["verification_status"] == "confirmed" and issues:
            raise ValueError(" ".join(issues))
        return normalize_net_allocations(raw_allocations)
    if fields["gross_amount"] is None:
        if raw_allocations:
            raise ValueError("Сначала укажите итоговую сумму документа.")
        return []

    normalized = []
    allocation_vat_unknown = False
    for item in raw_allocations:
        if not isinstance(item, dict):
            raise ValueError("Некорректная строка распределения.")
        object_id = item.get("object_id")
        if object_id is not None:
            try:
                object_id = int(object_id)
            except (TypeError, ValueError) as error:
                raise ValueError("Некорректный объект распределения.") from error
            cursor.execute("SELECT id FROM meta_objects WHERE id = ? AND (is_active = 1 OR ? = 'object_cost')",
                           (object_id, fields.get("usage")))
            if cursor.fetchone() is None:
                raise ValueError("Активный объект не найден в серверном справочнике.")
        allocated_gross = parse_decimal(item.get("gross_amount"))
        if allocated_gross is None:
            raise ValueError("Для каждой части укажите сумму с НДС.")
        allocated_gross = Decimal(decimal_text(allocated_gross))
        vat_amount = parse_decimal(item.get("vat_amount"))
        vat_amount = Decimal(decimal_text(vat_amount)) if vat_amount is not None else None
        if fields["vat_amount"] is None:
            if vat_amount is not None:
                raise ValueError("НДС документа неизвестен; суммы НДС распределений тоже оставьте неизвестными.")
        elif vat_amount is None:
            if len(raw_allocations) == 1 and allocated_gross == Decimal(fields["gross_amount"]):
                vat_amount = Decimal(fields["vat_amount"])
            else:
                allocation_vat_unknown = True
        if vat_amount is not None and allocated_gross and (vat_amount > allocated_gross if allocated_gross > 0 else vat_amount < allocated_gross):
            raise ValueError("НДС части не может превышать её сумму с НДС.")
        normalized.append({
            "object_id": object_id,
            "gross_amount": decimal_text(allocated_gross),
            "vat_amount": decimal_text(vat_amount),
            "net_amount": decimal_text(allocated_gross - vat_amount) if vat_amount is not None else None,
            "note": str(item.get("note") or "")[:500],
        })

    total_gross = Decimal(fields["gross_amount"])
    allocated_gross = sum((Decimal(item["gross_amount"]) for item in normalized), Decimal(0))
    if allocated_gross and (total_gross == 0 or (allocated_gross > 0) != (total_gross > 0)):
        raise ValueError("Распределение должно иметь тот же знак, что и сумма фактуры.")
    if abs(allocated_gross) > abs(total_gross):
        raise ValueError("Распределённая сумма не может превышать сумму фактуры.")
    total_vat = fields["vat_amount"]
    known_allocation_vat = [Decimal(item["vat_amount"]) for item in normalized if item["vat_amount"] is not None]
    if total_vat is not None:
        allocated_vat = sum(known_allocation_vat, Decimal(0))
        if abs(allocated_vat) > abs(Decimal(total_vat)):
            raise ValueError("Распределённый НДС не может превышать НДС документа.")
        if allocated_gross == total_gross and not allocation_vat_unknown and allocated_vat != Decimal(total_vat):
            raise ValueError("При полном распределении суммы частей должны покрыть весь НДС документа.")
    if fields.get("usage") == "object_cost" and fields["verification_status"] == "confirmed":
        if not normalized or any(item["object_id"] is None or Decimal(item["gross_amount"]) <= 0
                                 for item in normalized):
            raise ValueError("Для расхода выберите объекты и положительные суммы частей.")
        if allocated_gross != total_gross:
            raise ValueError("Сумма частей должна точно совпадать с общей суммой документа.")
        if len({item["object_id"] for item in normalized}) != len(normalized):
            raise ValueError("Укажите каждый объект одной строкой.")
    if allocation_vat_unknown and fields["verification_status"] == "confirmed" and fields.get("usage") != "object_cost":
        fields["verification_status"] = "requires_review"
    return normalized


def normalize_project_code_link_part(value):
    return " ".join(str(value or "").split()).casefold()


def validate_expense_order_proposals(cursor, raw_proposals):
    if not isinstance(raw_proposals, list) or len(raw_proposals) > 100:
        raise ValueError("Предложения по заказам должны быть списком не более чем из 100 строк.")
    proposals = []
    for item in raw_proposals:
        if not isinstance(item, dict):
            raise ValueError("Некорректная строка предложения по заказу.")
        project_code = str(item.get("project_code") or "").strip()[:100]
        order_id = str(item.get("order_id") or "").strip()[:100]
        net_amount = parse_decimal(item.get("net_amount"))
        vat_amount = parse_decimal(item.get("vat_amount"))
        gross_amount = parse_decimal(item.get("gross_amount"))
        if gross_amount is not None and net_amount is not None and vat_amount is not None:
            if Decimal(decimal_text(net_amount)) + Decimal(decimal_text(vat_amount)) != Decimal(decimal_text(gross_amount)):
                raise ValueError("В предложении по заказу gross должен равняться net плюс явно указанный VAT.")
        if net_amount is not None and vat_amount is not None:
            gross_amount = Decimal(decimal_text(net_amount)) + Decimal(decimal_text(vat_amount))
        object_id = item.get("object_id")
        if object_id is not None:
            try:
                object_id = int(object_id)
            except (TypeError, ValueError) as error:
                raise ValueError("Некорректный объект в предложении по заказу.") from error
            cursor.execute("SELECT id FROM meta_objects WHERE id = ? AND is_active = 1", (object_id,))
            if cursor.fetchone() is None:
                raise ValueError("Объект предложения отсутствует в активном серверном справочнике.")
        sources = item.get("sources") or []
        if not isinstance(sources, list) or len(sources) > 50:
            raise ValueError("Некорректные источники предложения по заказу.")
        normalized_sources = []
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("Некорректная ссылка на страницу документа.")
            try:
                page = int(source.get("page"))
            except (TypeError, ValueError) as error:
                raise ValueError("Некорректный номер страницы в предложении.") from error
            if page < 1:
                raise ValueError("Номер страницы предложения должен быть положительным.")
            try:
                line = int(source.get("line") or 0)
                attachment_index = int(source.get("attachment_index") or 0)
            except (TypeError, ValueError) as error:
                raise ValueError("Некорректный индекс источника предложения.") from error
            if line < 0 or attachment_index < 0 or attachment_index >= 20:
                raise ValueError("Индекс источника предложения вне допустимого диапазона.")
            normalized_sources.append({
                "source": str(source.get("source") or "")[:255],
                "page": page,
                "line": line,
                "text": str(source.get("text") or "")[:500],
                "attachment_index": attachment_index,
            })
        proposals.append({
            "project_code": project_code,
            "order_id": order_id,
            "net_amount": decimal_text(net_amount),
            "vat_amount": decimal_text(vat_amount),
            "gross_amount": decimal_text(gross_amount),
            "object_id": object_id,
            "sources": normalized_sources,
            "ambiguous": bool(item.get("ambiguous")),
        })
        if "destination" in item or "original_marker" in item:
            proposals[-1].update({
                "original_marker": str(item.get("original_marker") or "")[:250],
                "destination": str(item.get("destination") or "unselected"),
                "expense_category": str(item.get("expense_category") or "")[:100],
            })
    return proposals


def save_expense_project_code_links(cursor, supplier_name, proposals, now):
    supplier_key = normalize_project_code_link_part(supplier_name)
    for proposal in proposals:
        if not proposal.get("project_code") or proposal.get("object_id") is None or proposal.get("destination") == "company":
            continue
        cursor.execute("""
            INSERT INTO expense_project_code_links (
                supplier_key, supplier_name, project_code_key, project_code, object_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(supplier_key, project_code_key) DO UPDATE SET
                supplier_name = excluded.supplier_name,
                project_code = excluded.project_code,
                object_id = excluded.object_id,
                updated_at = excluded.updated_at
        """, (
            supplier_key, supplier_name, normalize_project_code_link_part(proposal["project_code"]),
            proposal["project_code"], proposal["object_id"], now,
        ))


def store_expense_allocation_rows(cursor, document_id, allocations, fields):
    if fields.get("payment_reference_provided"):
        cursor.execute("UPDATE expense_documents SET payment_reference=? WHERE id=?",
                       (fields["payment_reference"], document_id))
    if fields.get("due_date_provided"):
        cursor.execute("""UPDATE expense_documents SET due_date=?, due_date_kind=?, due_date_review_required=?,
                          due_date_reviewed=?, due_date_evidence_json=? WHERE id=?""",
                       (fields["due_date"], fields["due_date_kind"], int(fields["due_date_review_required"]),
                        int(fields["due_date_reviewed"]), json.dumps(fields["due_date_evidence"], ensure_ascii=False), document_id))
    columns = ["document_id", "object_id", "allocated_gross", "allocated_net", "allocated_vat", "allocation_note"]
    net_basis = fields.get("allocation_basis") == "net"
    net_schema = invoice_net_schema_ready(cursor)
    if net_schema:
        columns.extend(["destination", "expense_category"])
    if net_basis:
        cursor.execute("UPDATE expense_documents SET allocation_basis='net', rounding_amount=?, amount_due=? WHERE id=?",
                       (fields.get("rounding_amount"), fields.get("amount_due"), document_id))
    elif invoice_net_schema_ready(cursor):
        cursor.execute("UPDATE expense_documents SET allocation_basis='gross',rounding_amount=? WHERE id=?",
                       (fields.get("rounding_amount"), document_id))
    rows = []
    for item in allocations:
        row = [document_id, item["object_id"], item["gross_amount"], item["net_amount"], item["vat_amount"], item["note"]]
        if net_schema:
            row.extend([item.get("destination") or ("company" if item["object_id"] is None else "object"), item.get("expense_category") or ""])
        rows.append(row)
    cursor.executemany(f"INSERT INTO expense_allocations ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", rows)


def serialize_expense_document(cursor, document, include_text=False):
    net_schema = invoice_net_schema_ready(cursor)
    allocation_extras = ", a.destination, a.expense_category" if net_schema else ""
    cursor.execute(f"""
        SELECT a.object_id, o.name AS object_name, o.company, o.markning,
               a.allocated_gross AS gross_amount, a.allocated_net AS net_amount,
               a.allocated_vat AS vat_amount, a.allocation_note AS note {allocation_extras}
        FROM expense_allocations a
        LEFT JOIN meta_objects o ON o.id = a.object_id
        WHERE a.document_id = ? ORDER BY a.id
    """, (document["id"],))
    serialized_allocations = []
    for row in cursor.fetchall():
        item = dict(row)
        item["object_id"] = int(item["object_id"]) if item["object_id"] is not None else None
        item["object_name"] = item["object_name"] or ("Общие расходы фирмы" if item.get("destination", "company") == "company" else "Не выбрано")
        item["company_expense"] = item.get("destination", "company" if item["object_id"] is None else "object") == "company"
        for field in ("gross_amount", "net_amount", "vat_amount"):
            item[field] = decimal_text(item[field])
        serialized_allocations.append(item)

    document_gross = parse_decimal(document["gross_amount"])
    allocated_gross = sum(
        (parse_decimal(item["gross_amount"]) or Decimal(0) for item in serialized_allocations),
        Decimal(0),
    ) if all(item["gross_amount"] is not None for item in serialized_allocations) else None
    unallocated_gross = document_gross - allocated_gross if document_gross is not None and allocated_gross is not None else None
    document_vat = parse_decimal(document["vat_amount"])
    allocated_vat_values = [parse_decimal(item["vat_amount"]) for item in serialized_allocations]
    allocated_vat = (
        sum(allocated_vat_values, Decimal(0))
        if document_vat is not None and all(value is not None for value in allocated_vat_values)
        else None
    )
    unallocated_vat = document_vat - allocated_vat if document_vat is not None and allocated_vat is not None else None
    document_net = parse_decimal(document["net_amount"])
    allocated_net_values = [parse_decimal(item["net_amount"]) for item in serialized_allocations]
    allocated_net = (
        sum(allocated_net_values, Decimal(0))
        if document_net is not None and all(value is not None for value in allocated_net_values)
        else None
    )
    unallocated_net = document_net - allocated_net if document_net is not None and allocated_net is not None else None
    cursor.execute("""
        SELECT id, original_name, file_sha256 AS sha256 FROM expense_document_attachments
        WHERE document_id = ? ORDER BY ordinal, id
    """, (document["id"],))
    attachments = [dict(row) | {"has_original": True} for row in cursor.fetchall()]
    if not attachments and document["stored_name"]:
        attachments = [{
            "id": None, "original_name": document["original_name"], "has_original": True,
            "sha256": document["file_sha256"],
        }]
    serialized = {
        "id": int(document["id"]),
        "client_id": document["client_id"],
        "supplier_name": document["supplier_name"],
        "invoice_number": document["invoice_number"],
        "payment_reference": document["payment_reference"] if "payment_reference" in document.keys() else None,
        "document_date": document["document_date"],
        "due_date": document["due_date"] if "due_date" in document.keys() else None,
        "due_date_kind": document["due_date_kind"] if "due_date_kind" in document.keys() else "absent",
        "due_date_review_required": bool(document["due_date_review_required"]) if "due_date_review_required" in document.keys() else False,
        "due_date_reviewed": bool(document["due_date_reviewed"]) if "due_date_reviewed" in document.keys() else False,
        "due_date_evidence": json.loads(document["due_date_evidence_json"] or "[]") if "due_date_evidence_json" in document.keys() else [],
        "currency_code": document["currency_code"],
        "net_amount": decimal_text(document["net_amount"]),
        "vat_amount": decimal_text(document["vat_amount"]),
        "gross_amount": decimal_text(document["gross_amount"]),
        "expense_category": document["expense_category"],
        "document_comment": document["document_comment"],
        "order_proposals": json.loads(document["order_proposals_json"] or "[]"),
        **invoice_context(document["recognized_text"] or ""),
        "verification_status": document["verification_status"],
        "original_name": document["original_name"],
        "has_original": bool(attachments),
        "attachments": attachments,
        "allocations": serialized_allocations,
        "allocated_gross": decimal_text(allocated_gross),
        "unallocated_gross": decimal_text(unallocated_gross),
        "allocated_net": decimal_text(allocated_net),
        "unallocated_net": decimal_text(unallocated_net),
        "allocated_vat": decimal_text(allocated_vat),
        "unallocated_vat": decimal_text(unallocated_vat),
        "fully_allocated": (document_net is not None and unallocated_net == 0) if net_schema and document["allocation_basis"] == "net" else document_gross is not None and unallocated_gross == 0,
        "allocation_basis": document["allocation_basis"] if net_schema else "gross",
        "rounding_amount": document["rounding_amount"] if net_schema else None,
        "amount_due": document["amount_due"] if net_schema else None,
        "is_deleted": bool(document["is_deleted"]),
        "sync_version": int(document["sync_version"] or 1),
        "created_at": document["created_at"],
        "updated_at": document["updated_at"],
    }
    processing = cursor.execute("SELECT state, details_json FROM expense_processing WHERE document_id=?", (document["id"],)).fetchone()
    serialized["processing"] = ({"state": processing["state"], **json.loads(processing["details_json"])}
                                if processing else None)
    if serialized["processing"] and serialized["processing"].get("usage") == "object_cost":
        serialized["usage"] = "object_cost"
    if include_text:
        serialized["recognized_text"] = document["recognized_text"]
    return serialized


def stage_expense_original(upload):
    if not upload or not upload.filename:
        return None
    original_name = secure_filename(upload.filename)
    suffix = Path(original_name).suffix.casefold()
    if suffix not in EXPENSE_DOCUMENT_EXTENSIONS:
        raise ValueError("Поддерживаются PDF и распространённые форматы изображений.")
    root = expense_documents_root()
    root.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    temporary = root / f".{stored_name}.part"
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as destination:
            while True:
                chunk = upload.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > app.config["MAX_CONTENT_LENGTH"]:
                    raise ValueError("Размер оригинала превышает 25 МБ.")
                digest.update(chunk)
                destination.write(chunk)
        return root, temporary, stored_name, original_name, digest.hexdigest()
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


@app.route("/expenses", methods=["GET"])
def expenses_page():
    if session.get("role") != "admin":
        return redirect("/")
    csrf_token = session.setdefault("expense_csrf_token", uuid.uuid4().hex)
    return render_template("expenses.html", csrf_token=csrf_token,
                           mail_intake_enabled=mail_intake_store is not None)


@app.route("/api/expense-objects", methods=["GET"])
def expense_objects_api():
    if not expense_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    conn = get_db_connection()
    try:
        include_inactive = request.args.get("include_inactive") == "1"
        query = "SELECT id, sync_id, name, markning, company, rate, transport_rate, is_active, sync_version FROM meta_objects"
        if not include_inactive:
            query += " WHERE is_active = 1"
        rows = conn.execute(query + " ORDER BY company, name, id").fetchall()
        return jsonify({"ok": True, "objects": [dict(row) for row in rows]})
    finally:
        conn.close()


@app.route("/api/expense-documents/capabilities", methods=["GET"])
def expense_document_capabilities_api():
    if not expense_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    connection = get_db_connection()
    try:
        net_features = ["net_allocations_v1"] if invoice_net_schema_ready(connection) else []
        if expense_due_date_schema_ready(connection):
            net_features.append("due_date_v1")
        if "payment_reference" in {row[1] for row in connection.execute("PRAGMA table_info(expense_documents)")}:
            net_features.append("payment_reference_v1")
        net_features.append("object_costs_v1")
        net_features.append("object_cost_allocations_v1")
    finally:
        connection.close()
    return jsonify({
        "ok": True,
        "api_version": 2,
        "features": [
            "client_id", "soft_delete", "attachments",
            "nullable_company_allocations", "versioned_updates",
            "order_proposals", "supplier_project_code_links",
        ] + net_features,
    })


@app.route("/api/expense-project-code-links", methods=["GET", "POST"])
def expense_project_code_links_api():
    if not expense_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    if request.method == "GET":
        supplier_key = normalize_project_code_link_part(request.args.get("supplier_name"))
        conn = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT supplier_name, project_code, object_id FROM expense_project_code_links "
                "WHERE supplier_key = ? ORDER BY project_code_key",
                (supplier_key,),
            ).fetchall()
            return jsonify({"ok": True, "links": [dict(row) for row in rows]})
        finally:
            conn.close()

    payload = expense_request_payload()
    supplier_name = " ".join(str(payload.get("supplier_name") or "").split())[:200]
    project_code = " ".join(str(payload.get("project_code") or "").split())[:100]
    supplier_key = normalize_project_code_link_part(supplier_name)
    project_code_key = normalize_project_code_link_part(project_code)
    try:
        object_id = int(payload.get("object_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Выберите существующий объект."}), 400
    if not supplier_key or not project_code_key:
        return jsonify({"ok": False, "error": "Поставщик и код проекта обязательны."}), 400
    conn = get_db_connection()
    try:
        if conn.execute("SELECT 1 FROM meta_objects WHERE id = ? AND is_active = 1", (object_id,)).fetchone() is None:
            return jsonify({"ok": False, "error": "Объект отсутствует в активном серверном справочнике."}), 400
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            INSERT INTO expense_project_code_links (
                supplier_key, supplier_name, project_code_key, project_code, object_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(supplier_key, project_code_key) DO UPDATE SET
                supplier_name = excluded.supplier_name,
                project_code = excluded.project_code,
                object_id = excluded.object_id,
                updated_at = excluded.updated_at
        """, (supplier_key, supplier_name, project_code_key, project_code, object_id, now))
        conn.commit()
        return jsonify({"ok": True, "supplier_name": supplier_name, "project_code": project_code, "object_id": object_id})
    finally:
        conn.close()


@app.route("/api/expense-documents", methods=["GET"])
def expense_documents_list_api():
    if not expense_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    object_id = request.args.get("object_id", "").strip()
    query = "SELECT * FROM expense_documents"
    conditions = ["is_deleted = 0", """NOT EXISTS (
        SELECT 1 FROM expense_processing p
        WHERE p.document_id=expense_documents.id AND p.state='split')"""]
    params = []
    if date_from:
        conditions.append("(document_date IS NULL OR document_date = '' OR document_date >= ?)")
        params.append(date_from)
    if date_to:
        conditions.append("(document_date IS NULL OR document_date = '' OR document_date <= ?)")
        params.append(date_to)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY document_date DESC, id DESC LIMIT 1000"

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        documents = [serialize_expense_document(cursor, row) for row in rows]
        if object_id:
            try:
                selected_object = int(object_id)
            except ValueError:
                return jsonify({"ok": False, "error": "Некорректный ID объекта."}), 400
            documents = [
                document for document in documents
                if any(item["object_id"] == selected_object for item in document["allocations"])
            ]
        supplier_names = sorted({
            document["supplier_name"] for document in documents if document["supplier_name"]
        }, key=str.casefold)
        return jsonify({"ok": True, "documents": documents, "suppliers": supplier_names})
    finally:
        conn.close()


@app.route("/api/expense-documents/<int:document_id>", methods=["GET"])
def expense_document_get_api(document_id):
    if not expense_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        document = cursor.execute(
            "SELECT * FROM expense_documents WHERE id = ? AND is_deleted = 0", (document_id,)
        ).fetchone()
        if document is None:
            return jsonify({"ok": False, "error": "Документ не найден."}), 404
        return jsonify({"ok": True, "document": serialize_expense_document(cursor, document, include_text=True)})
    finally:
        conn.close()


@app.route("/api/expense-documents/by-client/<string:client_id>", methods=["DELETE"])
def expense_document_delete_by_client_api(client_id):
    if not expense_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    conn = get_db_connection()
    try:
        payload = expense_request_payload() if request.get_data(cache=True) else {}
        now = datetime.now(timezone.utc).isoformat()
        existing = conn.execute(
            "SELECT id, is_deleted, sync_version FROM expense_documents WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        if existing is None or existing["is_deleted"]:
            return jsonify({"ok": True, "deleted": True})
        expected_version = payload.get("expected_version")
        if expected_version is not None and int(expected_version) != int(existing["sync_version"]):
            return jsonify({
                "ok": False, "conflict": True, "error": "Документ изменился на сервере; обновите данные перед удалением.",
                "current_version": int(existing["sync_version"]),
            }), 409
        conn.execute("""
            UPDATE expense_documents
            SET is_deleted = 1, deleted_at = ?, updated_at = ?, sync_version = sync_version + 1
            WHERE id = ? AND is_deleted = 0 AND sync_version = ?
        """, (now, now, int(existing["id"]), int(existing["sync_version"])))
        conn.commit()
        return jsonify({"ok": True, "deleted": True})
    finally:
        conn.close()


@app.route("/api/expense-documents/<int:document_id>", methods=["DELETE"])
def expense_document_delete_api(document_id):
    if not expense_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    conn = get_db_connection()
    try:
        payload = expense_request_payload() if request.get_data(cache=True) else {}
        now = datetime.now(timezone.utc).isoformat()
        existing = conn.execute(
            "SELECT is_deleted, sync_version FROM expense_documents WHERE id = ?",
            (int(document_id),),
        ).fetchone()
        if existing is None or existing["is_deleted"]:
            return jsonify({"ok": True, "deleted": True})
        expected_version = payload.get("expected_version")
        if expected_version is not None and int(expected_version) != int(existing["sync_version"]):
            return jsonify({
                "ok": False, "conflict": True, "error": "Документ изменился на сервере; обновите данные перед удалением.",
                "current_version": int(existing["sync_version"]),
            }), 409
        conn.execute("""
            UPDATE expense_documents
            SET is_deleted = 1, deleted_at = ?, updated_at = ?, sync_version = sync_version + 1
            WHERE id = ? AND is_deleted = 0 AND sync_version = ?
        """, (now, now, int(document_id), int(existing["sync_version"])))
        conn.commit()
        return jsonify({"ok": True, "deleted": True})
    finally:
        conn.close()


@app.route("/api/expense-documents", methods=["POST"])
def expense_document_create_api():
    if not expense_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    conn = None
    staged_files = []
    targets = []
    committed = False
    try:
        payload = expense_request_payload()
        client_id = str(payload.get("client_id") or "").strip()
        if not client_id or len(client_id) > 100:
            raise ValueError("Не указан client_id для безопасного повтора запроса.")
        fields = validate_expense_fields(payload)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")

        existing = cursor.execute(
            "SELECT * FROM expense_documents WHERE client_id = ?", (client_id,)
        ).fetchone()
        if existing is not None:
            if existing["is_deleted"]:
                return jsonify({"ok": False, "error": "Документ с этим client_id уже удалён."}), 410
            document = serialize_expense_document(cursor, existing)
            return jsonify({"ok": True, "duplicate_request": True, "document": document}), 200

        if payload.get("usage") == "object_cost":
            matching = cursor.execute("""SELECT id FROM expense_documents
                WHERE supplier_name=? COLLATE NOCASE AND document_date=? AND currency_code=?
                AND gross_amount=? LIMIT 1""",
                (fields["supplier_name"], fields["document_date"], fields["currency_code"],
                 fields["gross_amount"])).fetchone()
            if matching:
                return jsonify({"ok": False, "error": "Возможный повтор покупки; требуется ручная сверка.",
                                "existing_document_id": matching["id"]}), 409

        if fields["supplier_name"] and fields["invoice_number"] and fields["document_date"] and fields["gross_amount"]:
            matching_document = cursor.execute("""
                SELECT id FROM expense_documents
                WHERE supplier_name = ? COLLATE NOCASE AND invoice_number = ?
                  AND document_date = ? AND currency_code = ? AND gross_amount = ?
                ORDER BY id LIMIT 1
            """, (
                fields["supplier_name"], fields["invoice_number"], fields["document_date"],
                fields["currency_code"], fields["gross_amount"],
            )).fetchone()
            if matching_document:
                return jsonify({
                    "ok": False, "error": "Такая фактура уже есть в архиве.",
                    "existing_document_id": int(matching_document["id"]),
                }), 409

        order_proposals = validate_expense_order_proposals(cursor, payload.get("order_proposals", []))
        raw_allocations = payload.get("allocations", [])
        if order_proposals and fields["verification_status"] == "confirmed" and fields["allocation_basis"] != "net":
            raw_allocations = build_project_order_allocations(
                order_proposals, fields["net_amount"], fields["vat_amount"], fields["gross_amount"],
            )
        allocations = resolve_expense_allocations(cursor, raw_allocations, fields)
        uploads = [upload for upload in request.files.getlist("originals") if upload and upload.filename]
        if not uploads:
            upload = request.files.get("original")
            uploads = [upload] if upload and upload.filename else []
        if len(uploads) > 20:
            raise ValueError("К одному документу можно прикрепить не более 20 файлов.")
        total_upload_size = 0
        seen_hashes = set()
        for ordinal, upload in enumerate(uploads):
            staged = stage_expense_original(upload)
            if staged is None:
                continue
            root, temporary, stored_name, original_name, file_sha256 = staged
            total_upload_size += temporary.stat().st_size
            if total_upload_size > app.config["MAX_CONTENT_LENGTH"]:
                temporary.unlink(missing_ok=True)
                raise ValueError("Общий размер оригиналов превышает 25 МБ.")
            if file_sha256 in seen_hashes:
                temporary.unlink(missing_ok=True)
                raise ValueError("Один и тот же файл добавлен к документу несколько раз.")
            seen_hashes.add(file_sha256)
            duplicate = cursor.execute("""
                SELECT d.id FROM expense_documents d
                LEFT JOIN expense_document_attachments a ON a.document_id = d.id
                WHERE d.is_deleted = 0 AND (d.file_sha256 = ? OR a.file_sha256 = ?)
                ORDER BY d.id LIMIT 1
            """, (file_sha256, file_sha256)).fetchone()
            if duplicate:
                temporary.unlink(missing_ok=True)
                return jsonify({
                    "ok": False, "error": "Этот оригинал уже находится в архиве.",
                    "existing_document_id": int(duplicate["id"]),
                }), 409
            deleted_duplicate = cursor.execute("""
                SELECT d.id,d.client_id,d.sync_version FROM expense_documents d
                LEFT JOIN expense_document_attachments a ON a.document_id=d.id
                WHERE d.is_deleted=1 AND (d.file_sha256=? OR a.file_sha256=?)
                ORDER BY d.id LIMIT 1
            """, (file_sha256, file_sha256)).fetchone()
            if deleted_duplicate:
                temporary.unlink(missing_ok=True)
                return jsonify({"ok": False, "code": "deleted_original",
                                "error": "Оригинал принадлежит ранее удалённому документу; восстановите исходную запись, повторная загрузка не создаётся.",
                                "existing_document_id": int(deleted_duplicate["id"]),
                                "existing_client_id": deleted_duplicate["client_id"],
                                "current_version": int(deleted_duplicate["sync_version"])}), 409
            target = root / stored_name
            temporary.replace(target)
            staged_files.append({
                "temporary": None, "target": target, "stored_name": stored_name,
                "original_name": original_name, "file_sha256": file_sha256,
                "ordinal": ordinal,
            })

        primary = staged_files[0] if staged_files else None

        now = datetime.now(timezone.utc).isoformat()
        cursor.execute("""
            INSERT INTO expense_documents (
                client_id, supplier_name, invoice_number, document_date, currency_code,
                net_amount, vat_amount, gross_amount, expense_category, document_comment,
                verification_status, recognized_text, order_proposals_json, original_name, stored_name,
                file_sha256, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            client_id, fields["supplier_name"], fields["invoice_number"], fields["document_date"],
            fields["currency_code"], fields["net_amount"], fields["vat_amount"],
            fields["gross_amount"], fields["expense_category"], fields["document_comment"],
            fields["verification_status"], fields["recognized_text"],
            json.dumps(order_proposals, ensure_ascii=False),
            primary["original_name"] if primary else None,
            primary["stored_name"] if primary else None,
            primary["file_sha256"] if primary else None, now, now,
        ))
        document_id = int(cursor.lastrowid)
        store_expense_allocation_rows(cursor, document_id, allocations, fields)
        if payload.get("usage") == "object_cost":
            cursor.execute("INSERT INTO expense_processing VALUES (?,?,?,?)",
                           (document_id, "reviewed", json.dumps({"usage": "object_cost"}), now))
        cursor.executemany("""
            INSERT INTO expense_document_attachments (
                document_id, original_name, stored_name, file_sha256, ordinal, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, [(
            document_id, item["original_name"], item["stored_name"],
            item["file_sha256"], item["ordinal"], now,
        ) for item in staged_files])
        if order_proposals and fields["verification_status"] == "confirmed":
            save_expense_project_code_links(cursor, fields["supplier_name"], order_proposals, now)
        conn.commit()
        committed = True
        document = cursor.execute(
            "SELECT * FROM expense_documents WHERE id = ?", (document_id,)
        ).fetchone()
        return jsonify({"ok": True, "document": serialize_expense_document(cursor, document)}), 201
    except RuntimeError as error:
        return jsonify({"ok": False, "error": str(error)}), 503
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    except Exception:
        logger.exception("Не удалось сохранить документ расхода.")
        return jsonify({"ok": False, "error": "Не удалось сохранить документ."}), 500
    finally:
        if conn is not None:
            conn.close()
        for item in staged_files:
            temporary_path = item.get("temporary")
            if temporary_path:
                temporary_path.unlink(missing_ok=True)
            if item.get("target") and not committed:
                item["target"].unlink(missing_ok=True)


@app.route("/api/expense-documents/<int:document_id>", methods=["PUT"])
def expense_document_update_api(document_id):
    if not expense_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    conn = get_db_connection()
    try:
        payload = expense_request_payload()
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        existing = cursor.execute(
            "SELECT * FROM expense_documents WHERE id = ?", (document_id,)
        ).fetchone()
        restore = payload.get("restore_deleted") is True
        restore_operation = str(payload.get("restore_operation_id") or "")
        restore_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                 separators=(",", ":")).encode("utf-8")).hexdigest() if restore else None
        processing = cursor.execute("SELECT state,details_json FROM expense_processing WHERE document_id=?", (document_id,)).fetchone()
        details = json.loads(processing["details_json"]) if processing else {}
        if details.get("usage") == "object_cost" or payload.get("usage") == "object_cost":
            payload["usage"] = "object_cost"
            if payload.get("expected_version") is None:
                raise ValueError("Для изменения расхода обязательна актуальная версия документа.")
            if "recognized_text" not in payload and existing is not None:
                payload["recognized_text"] = existing["recognized_text"]
            if (existing is not None and "rounding_amount" in existing.keys()
                    and existing["rounding_amount"] is not None and "rounding_amount" not in payload
                    and parse_invoice_money(payload.get("gross_amount")) == parse_invoice_money(existing["gross_amount"])
                    and parse_invoice_money(payload.get("vat_amount")) == parse_invoice_money(existing["vat_amount"])):
                payload["net_amount"] = existing["net_amount"]
                payload["rounding_amount"] = existing["rounding_amount"]
        fields = validate_expense_fields(payload)
        cost_operation = str(payload.get("cost_operation_id") or "")
        cost_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":")).encode("utf-8")).hexdigest()
        cost_receipt = details.get("cost_operation") or {}
        if (existing is not None and not existing["is_deleted"] and cost_operation
                and cost_receipt.get("id") == cost_operation
                and cost_receipt.get("sha256") == cost_hash
                and cost_receipt.get("version") == int(existing["sync_version"])):
            return jsonify({"ok": True, "duplicate_request": True,
                            "document": serialize_expense_document(cursor, existing)})
        receipt = details.get("restoration") or {}
        if (existing is not None and not existing["is_deleted"] and restore
            and receipt.get("operation_id") == restore_operation and receipt.get("payload_sha256") == restore_hash
            and receipt.get("restored_version") == int(existing["sync_version"])):
            return jsonify({"ok": True, "restored": True, "duplicate_request": True,
                            "document": serialize_expense_document(cursor, existing)})
        if existing is None or existing["is_deleted"] and not restore:
            conn.rollback()
            return jsonify({"ok": False, "error": "Документ не найден."}), 404
        if restore:
            if not existing["is_deleted"]:
                return jsonify({"ok": False, "conflict": True, "error": "Документ уже восстановлен или изменён; обновите данные.",
                                "current_version": int(existing["sync_version"])}), 409
            if not restore_operation or len(restore_operation) > 100 or payload.get("client_id") != existing["client_id"]:
                raise ValueError("Для восстановления нужны исходный client_id и уникальный ID операции.")
            if payload.get("expected_version") is None:
                raise ValueError("Для восстановления обязательна версия исходного документа.")
            if existing["is_deleted"] and existing["verification_status"] == "confirmed":
                raise ValueError("Удалённый подтверждённый документ требует отдельной проверки перед восстановлением.")
            originals = cursor.execute("SELECT stored_name,file_sha256 FROM expense_document_attachments WHERE document_id=?", (document_id,)).fetchall()
            hashes = {row["file_sha256"] for row in originals}
            uploads = [upload for upload in request.files.getlist("originals") if upload and upload.filename]
            if not uploads:
                upload = request.files.get("original")
                uploads = [upload] if upload and upload.filename else []
            uploaded_hashes = {hashlib.sha256(upload.stream.read()).hexdigest() for upload in uploads}
            if not hashes or uploaded_hashes != hashes or len(uploads) != len(hashes):
                raise ValueError("Восстановление требует тех же оригиналов по SHA-256; запись не изменена.")
            root = expense_documents_root()
            for original in originals:
                path = (root / original["stored_name"]).resolve()
                if path.parent != root or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != original["file_sha256"]:
                    raise ValueError("Архивный оригинал недоступен или повреждён; восстановление остановлено.")
        if "allocation_basis" in existing.keys() and existing["allocation_basis"] == "net" and fields["allocation_basis"] != "net":
            conn.rollback()
            return jsonify({"ok": False, "conflict": True, "error": "Для изменения распределения без НДС обновите клиент."}), 409
        expected_version = payload.get("expected_version")
        if expected_version is not None and int(expected_version) != int(existing["sync_version"]):
            conn.rollback()
            return jsonify({
                "ok": False, "conflict": True,
                "error": "Документ изменился на сервере; обновите данные перед сохранением.",
                "current_version": int(existing["sync_version"]),
            }), 409
        if "recognized_text" not in payload:
            fields["recognized_text"] = existing["recognized_text"]
        if not fields["due_date_provided"] and "due_date" in existing.keys() and fields["verification_status"] == "confirmed":
            date_errors = due_date_issues({"due_date": existing["due_date"], "due_date_kind": existing["due_date_kind"],
                                          "due_date_review_required": bool(existing["due_date_review_required"]),
                                          "due_date_reviewed": bool(existing["due_date_reviewed"])})
            if date_errors:
                raise ValueError(" ".join(date_errors))
        if "order_proposals" in payload:
            order_proposals = validate_expense_order_proposals(cursor, payload.get("order_proposals"))
        else:
            order_proposals = json.loads(existing["order_proposals_json"] or "[]")
        if fields["supplier_name"] and fields["invoice_number"] and fields["document_date"] and fields["gross_amount"]:
            matching_document = cursor.execute("""
                SELECT id FROM expense_documents
                WHERE supplier_name = ? COLLATE NOCASE AND invoice_number = ?
                  AND document_date = ? AND currency_code = ? AND gross_amount = ?
                  AND id <> ?
                ORDER BY id LIMIT 1
            """, (
                fields["supplier_name"], fields["invoice_number"], fields["document_date"],
                fields["currency_code"], fields["gross_amount"], document_id,
            )).fetchone()
            if matching_document:
                conn.rollback()
                return jsonify({
                    "ok": False, "error": "Такая фактура уже есть в архиве.",
                    "existing_document_id": int(matching_document["id"]),
                }), 409
        raw_allocations = payload.get("allocations", [])
        if order_proposals and fields["verification_status"] == "confirmed" and fields["allocation_basis"] != "net":
            raw_allocations = build_project_order_allocations(
                order_proposals, fields["net_amount"], fields["vat_amount"], fields["gross_amount"],
            )
        allocations = resolve_expense_allocations(cursor, raw_allocations, fields)
        now = datetime.now(timezone.utc).isoformat()
        cursor.execute("""
            UPDATE expense_documents SET supplier_name=?, invoice_number=?, document_date=?,
                currency_code=?, net_amount=?, vat_amount=?, gross_amount=?, expense_category=?,
                document_comment=?, verification_status=?, recognized_text=?, order_proposals_json=?, updated_at=?,
                sync_version=sync_version+1, is_deleted=0, deleted_at=NULL
            WHERE id=? AND sync_version=?
        """, (
            fields["supplier_name"], fields["invoice_number"], fields["document_date"],
            fields["currency_code"], fields["net_amount"], fields["vat_amount"],
            fields["gross_amount"], fields["expense_category"], fields["document_comment"],
            fields["verification_status"], fields["recognized_text"],
            json.dumps(order_proposals, ensure_ascii=False), now, document_id,
            int(existing["sync_version"]),
        ))
        if cursor.rowcount != 1:
            conn.rollback()
            return jsonify({"ok": False, "conflict": True, "error": "Документ изменился на сервере."}), 409
        cursor.execute("DELETE FROM expense_allocations WHERE document_id = ?", (document_id,))
        store_expense_allocation_rows(cursor, document_id, allocations, fields)
        if order_proposals and fields["verification_status"] == "confirmed":
            save_expense_project_code_links(cursor, fields["supplier_name"], order_proposals, now)
        if restore:
            details["restoration"] = {"operation_id": restore_operation, "payload_sha256": restore_hash,
                                      "previous_version": int(existing["sync_version"]),
                                      "restored_version": int(existing["sync_version"]) + 1, "restored_at": now}
            cursor.execute("INSERT OR REPLACE INTO expense_processing VALUES (?,?,?,?)",
                           (document_id, "restored", json.dumps(details, ensure_ascii=False), now))
        if payload.get("usage") == "object_cost":
            details["usage"] = "object_cost"
            if cost_operation:
                if len(cost_operation) > 150:
                    raise ValueError("Слишком длинный идентификатор изменения расхода.")
                details["cost_operation"] = {"id": cost_operation, "sha256": cost_hash,
                                             "version": int(existing["sync_version"]) + 1}
            cursor.execute("INSERT OR REPLACE INTO expense_processing VALUES (?,?,?,?)",
                           (document_id, processing["state"] if processing else "reviewed",
                            json.dumps(details, ensure_ascii=False), now))
        conn.commit()
        document = cursor.execute(
            "SELECT * FROM expense_documents WHERE id = ?", (document_id,)
        ).fetchone()
        return jsonify({"ok": True, "document": serialize_expense_document(cursor, document)})
    except (ValueError, json.JSONDecodeError) as error:
        conn.rollback()
        return jsonify({"ok": False, "error": str(error)}), 400
    except Exception:
        conn.rollback()
        logger.exception("Не удалось обновить документ расхода.")
        return jsonify({"ok": False, "error": "Не удалось обновить документ."}), 500
    finally:
        conn.close()


@app.route("/api/expense-documents/<int:document_id>/allocation-vat", methods=["POST"])
def expense_allocation_vat_api(document_id):
    if not expense_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    from expense_documents import allocation_vat_from_items, extract_local_document

    payload = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        document = conn.execute("SELECT * FROM expense_documents WHERE id=? AND is_deleted=0", (document_id,)).fetchone()
        if document is None:
            return jsonify({"ok": False, "error": "Документ не найден."}), 404
        version = int(document["sync_version"] or 1)
        if payload.get("expected_version") != version:
            return jsonify({"ok": False, "error": "Документ изменился; обновите карточку."}), 409
        if not invoice_net_schema_ready(conn) or document["allocation_basis"] != "net":
            raise ValueError("Расчёт по позициям поддерживается для распределения без НДС.")
        root = expense_documents_root()
        originals = conn.execute("SELECT stored_name,file_sha256 FROM expense_document_attachments WHERE document_id=? ORDER BY ordinal,id", (document_id,)).fetchall()
        if not originals:
            raise ValueError("Нет сохранённого оригинала для проверки ставок позиций.")
        pages = []
        for attachment_index, original in enumerate(originals):
            path = (root / original["stored_name"]).resolve()
            if path.parent != root or not path.is_file():
                raise ValueError("Оригинал недоступен; НДС частей не изменён.")
            if hashlib.sha256(path.read_bytes()).hexdigest() != original["file_sha256"]:
                raise ValueError("Контрольная сумма оригинала не совпадает; НДС частей не изменён.")
            extraction = extract_local_document(path)
            pages.extend({"page": index + 1, "attachment_index": attachment_index, "elements": elements}
                         for index, elements in enumerate(extraction.page_elements))
        orders = allocation_vat_from_items(pages, json.loads(document["order_proposals_json"] or "[]"),
                                          document["net_amount"], document["vat_amount"])
        computed = normalize_net_allocations(orders)
        existing = conn.execute("SELECT * FROM expense_allocations WHERE document_id=? ORDER BY id", (document_id,)).fetchall()

        def allocation_key(row):
            return row["destination"], row["object_id"], row["expense_category"]

        by_key = {allocation_key(row): row for row in computed}
        if (len(existing) != len(computed) or len(by_key) != len(computed)
                or any(allocation_key(row) not in by_key or parse_invoice_money(row["allocated_net"]) !=
                       parse_invoice_money(by_key[allocation_key(row)]["net_amount"]) for row in existing)):
            raise ValueError("Распределения изменены относительно заказов. Сначала согласуйте net и назначения; НДС не перезаписан.")
        if any(row["allocated_vat"] is not None or row["allocated_gross"] is not None for row in existing):
            if not payload.get("replace_existing"):
                raise ValueError("НДС частей уже заполнен; автоматическая перезапись запрещена.")
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT sync_version,is_deleted FROM expense_documents WHERE id=?", (document_id,)).fetchone()
        if current["is_deleted"] or current["sync_version"] != version:
            conn.rollback()
            return jsonify({"ok": False, "error": "Документ изменился во время расчёта."}), 409
        for row in existing:
            amounts = by_key[allocation_key(row)]
            conn.execute("UPDATE expense_allocations SET allocated_vat=?,allocated_gross=? WHERE id=?",
                         (amounts["vat_amount"], amounts["gross_amount"], row["id"]))
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("UPDATE expense_documents SET order_proposals_json=?,sync_version=sync_version+1,updated_at=? WHERE id=?",
                     (json.dumps(orders, ensure_ascii=False), now, document_id))
        previous = conn.execute("SELECT state,details_json FROM expense_processing WHERE document_id=?", (document_id,)).fetchone()
        details = json.loads(previous["details_json"]) if previous else {}
        details["vat_allocation"] = {"basis": "item_rates", "rounding": "ROUND_HALF_UP_per_item",
                                     "version": version + 1, "orders": orders}
        conn.execute("INSERT OR REPLACE INTO expense_processing VALUES (?,?,?,?)",
                     (document_id, previous["state"] if previous else "allocated", json.dumps(details, ensure_ascii=False), now))
        conn.commit()
        saved = conn.execute("SELECT * FROM expense_documents WHERE id=?", (document_id,)).fetchone()
        return jsonify({"ok": True, "document": serialize_expense_document(conn.cursor(), saved)})
    except (ValueError, RuntimeError) as error:
        conn.rollback()
        return jsonify({"ok": False, "error": str(error)}), 400
    finally:
        conn.close()


@app.route("/api/expense-documents/<int:document_id>/recognize", methods=["POST"])
def expense_document_recognize_api(document_id):
    if not expense_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    from invoice_processing import recognize_invoice

    payload = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        document = conn.execute("SELECT * FROM expense_documents WHERE id=? AND is_deleted=0", (document_id,)).fetchone()
        if document is None:
            return jsonify({"ok": False, "error": "Документ не найден."}), 404
        version = int(document["sync_version"] or 1)
        if payload.get("expected_version") != version:
            return jsonify({"ok": False, "error": "Документ изменился; обновите карточку."}), 409
        if document["verification_status"] == "confirmed" or conn.execute(
                "SELECT 1 FROM expense_allocations WHERE document_id=?", (document_id,)).fetchone():
            return jsonify({"ok": False, "error": "Подтверждённые суммы и распределения не перезаписываются автоматически."}), 409
        attachments = conn.execute("SELECT stored_name FROM expense_document_attachments WHERE document_id=? ORDER BY ordinal,id", (document_id,)).fetchall()
        if len(attachments) != 1:
            return jsonify({"ok": False, "error": "Выберите разделение пакета: обработка нескольких независимых фактур как одной запрещена."}), 409
        if not payload.get("replace_fields") and any(document[key] not in (None, "") for key in (
                "supplier_name", "invoice_number", "document_date", "net_amount", "vat_amount", "gross_amount")):
            return jsonify({"ok": False, "error": "Поля уже заполнены. Подтвердите повторное распознавание."}), 409
        root = expense_documents_root()
        path = (root / attachments[0]["stored_name"]).resolve()
        if path.parent != root:
            raise ValueError("Некорректный путь оригинала.")
        result = recognize_invoice([path])
        now = datetime.now(timezone.utc).isoformat()
        details = {key: result[key] for key in ("methods", "errors", "missing_fields")}
        details["field_sources"] = result["fields"].get("field_sources", {})
        details["review_reasons"] = result["fields"].get("review_reasons", [])
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT sync_version,is_deleted FROM expense_documents WHERE id=?", (document_id,)).fetchone()
        if current["is_deleted"] or current["sync_version"] != version:
            conn.rollback()
            return jsonify({"ok": False, "error": "Документ изменился во время распознавания."}), 409
        if result["state"] != "failed":
            fields_payload = {**result["fields"], "verification_status": "requires_review", "allocations": [],
                              "recognized_text": result["fields"]["raw_text"]}
            if not invoice_net_schema_ready(conn):
                fields_payload.pop("allocation_basis", None)
            fields = validate_expense_fields(fields_payload)
            conn.execute("""UPDATE expense_documents SET supplier_name=?,invoice_number=?,document_date=?,
                            currency_code=?,net_amount=?,vat_amount=?,gross_amount=?,recognized_text=?,
                            verification_status='requires_review' WHERE id=?""", (
                fields["supplier_name"], fields["invoice_number"], fields["document_date"], fields["currency_code"],
                fields["net_amount"], fields["vat_amount"], fields["gross_amount"], fields_payload["recognized_text"], document_id))
            store_expense_allocation_rows(conn.cursor(), document_id, [], fields)
        conn.execute("UPDATE expense_documents SET sync_version=sync_version+1,updated_at=? WHERE id=?", (now, document_id))
        conn.execute("INSERT OR REPLACE INTO expense_processing VALUES (?,?,?,?)",
                     (document_id, result["state"], json.dumps(details, ensure_ascii=False), now))
        conn.commit()
        saved = conn.execute("SELECT * FROM expense_documents WHERE id=?", (document_id,)).fetchone()
        return jsonify({"ok": True, "document": serialize_expense_document(conn.cursor(), saved, include_text=True)})
    except (ValueError, RuntimeError) as error:
        conn.rollback()
        return jsonify({"ok": False, "error": str(error)}), 400
    finally:
        conn.close()


@app.route("/api/expense-documents/<int:document_id>/split", methods=["POST"])
def expense_document_split_api(document_id):
    if not expense_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    payload = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute("SELECT details_json FROM expense_processing WHERE document_id=? AND state='split'", (document_id,)).fetchone()
        if previous:
            return jsonify({"ok": True, **json.loads(previous[0])})
        parent = conn.execute("SELECT * FROM expense_documents WHERE id=? AND is_deleted=0", (document_id,)).fetchone()
        if parent is None:
            return jsonify({"ok": False, "error": "Документ не найден."}), 404
        if payload.get("expected_version") != parent["sync_version"]:
            return jsonify({"ok": False, "error": "Документ изменился; обновите карточку."}), 409
        if not invoice_net_schema_ready(conn):
            return jsonify({"ok": False, "error": "Для разделения пакета требуется обновление схемы расходов."}), 503
        if parent["verification_status"] == "confirmed" or conn.execute(
                """SELECT 1 FROM expense_allocations WHERE document_id=? AND (
                    object_id IS NOT NULL OR allocated_gross IS NOT NULL OR allocated_net IS NOT NULL
                    OR allocated_vat IS NOT NULL OR destination='company'
                    OR allocation_note<>'' OR expense_category<>'')""", (document_id,)).fetchone():
            return jsonify({"ok": False, "error": "Нельзя автоматически разделить подтверждённый или распределённый документ."}), 409
        attachments = conn.execute("SELECT * FROM expense_document_attachments WHERE document_id=? ORDER BY ordinal,id", (document_id,)).fetchall()
        if len(attachments) < 2:
            return jsonify({"ok": False, "error": "В карточке нет пакета файлов."}), 400
        now = datetime.now(timezone.utc).isoformat()
        children = []
        for attachment in attachments:
            cursor = conn.execute("""INSERT INTO expense_documents
                (client_id,verification_status,original_name,stored_name,currency_code,created_at,updated_at)
                VALUES (?,'requires_review',?,?,'UNKNOWN',?,?)""", (
                    str(uuid.uuid4()), attachment["original_name"], attachment["stored_name"], now, now))
            child_id = cursor.lastrowid
            conn.execute("UPDATE expense_document_attachments SET document_id=?,ordinal=0 WHERE id=?", (child_id, attachment["id"]))
            children.append(child_id)
        conn.execute("UPDATE expense_documents SET original_name=NULL,stored_name=NULL,file_sha256=NULL,sync_version=sync_version+1,updated_at=? WHERE id=?", (now, document_id))
        conn.execute("INSERT OR REPLACE INTO expense_processing VALUES (?,?,?,?)",
                     (document_id, "split", json.dumps({"children": children}), now))
        conn.commit()
        return jsonify({"ok": True, "children": children})
    finally:
        conn.close()


@app.route("/api/expense-documents/<int:document_id>/original", methods=["GET"])
def expense_document_original_api(document_id):
    if not expense_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    conn = get_db_connection()
    try:
        document = conn.execute("""
            SELECT original_name, stored_name FROM expense_documents
            WHERE id = ? AND is_deleted = 0
        """, (document_id,)).fetchone()
        attachment = conn.execute("""
            SELECT original_name, stored_name FROM expense_document_attachments
            WHERE document_id = ? ORDER BY ordinal, id LIMIT 1
        """, (document_id,)).fetchone()
    finally:
        conn.close()
    if document is None:
        return jsonify({"ok": False, "error": "Оригинал не найден."}), 404
    selected_original_name = attachment["original_name"] if attachment else document["original_name"]
    selected_stored_name = attachment["stored_name"] if attachment else document["stored_name"]
    if not selected_stored_name:
        return jsonify({"ok": False, "error": "Оригинал не найден."}), 404
    try:
        root = expense_documents_root()
    except (RuntimeError, ValueError) as error:
        return jsonify({"ok": False, "error": str(error)}), 503
    original = (root / selected_stored_name).resolve()
    if original.parent != root or not original.is_file():
        return jsonify({"ok": False, "error": "Файл оригинала недоступен."}), 404
    return send_file(
        original, as_attachment=True,
        download_name=selected_original_name or original.name,
        conditional=True,
    )


@app.route("/api/expense-documents/<int:document_id>/attachments/<int:attachment_id>/original", methods=["GET"])
def expense_document_attachment_original_api(document_id, attachment_id):
    if not expense_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    conn = get_db_connection()
    try:
        attachment = conn.execute("""
            SELECT a.original_name, a.stored_name
            FROM expense_document_attachments a
            JOIN expense_documents d ON d.id = a.document_id
            WHERE a.id = ? AND a.document_id = ? AND d.is_deleted = 0
        """, (int(attachment_id), int(document_id))).fetchone()
    finally:
        conn.close()
    if attachment is None:
        return jsonify({"ok": False, "error": "Оригинал не найден."}), 404
    try:
        root = expense_documents_root()
    except (RuntimeError, ValueError) as error:
        return jsonify({"ok": False, "error": str(error)}), 503
    original = (root / attachment["stored_name"]).resolve()
    if original.parent != root or not original.is_file():
        return jsonify({"ok": False, "error": "Файл оригинала недоступен."}), 404
    return send_file(
        original, as_attachment=True,
        download_name=attachment["original_name"] or original.name,
        conditional=True,
    )


def admin_api_authorized():
    return session.get("role") == "admin" or bearer_admin_user() is not None


@app.route("/api/admin/storage-status", methods=["GET"])
def admin_storage_status():
    if not admin_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    database = Path(DB_FILE).resolve()
    status = {"ok": True, "database_path": str(database), "database_bytes": database.stat().st_size if database.exists() else 0}
    usage = shutil.disk_usage(database.parent)
    status["disk"] = {"total_bytes": usage.total, "free_bytes": usage.free}
    backup_dir = database.parent / "backups"
    backups = sorted(backup_dir.glob("backup_*.db")) if backup_dir.is_dir() else []
    status["backups"] = {"directory": str(backup_dir), "count": len(backups),
                         "newest": [{"name": item.name, "bytes": item.stat().st_size} for item in backups[-3:]]}
    try:
        root = expense_documents_root()
    except (RuntimeError, ValueError) as error:
        status["documents"] = {"error": str(error)}
        return jsonify(status)
    verify_hashes = request.args.get("verify_hashes") == "1"
    conn = get_db_connection()
    try:
        rows = conn.execute("""
            SELECT a.stored_name, a.file_sha256 FROM expense_document_attachments a
            JOIN expense_documents d ON d.id = a.document_id WHERE d.is_deleted = 0
        """).fetchall()
        documents = conn.execute("SELECT COUNT(*) FROM expense_documents WHERE is_deleted = 0").fetchone()[0]
    finally:
        conn.close()
    missing, corrupted = [], []
    for row in rows:
        path = (root / row["stored_name"]).resolve()
        if path.parent != root or not path.is_file():
            missing.append(row["stored_name"])
        elif verify_hashes and hashlib.sha256(path.read_bytes()).hexdigest() != row["file_sha256"]:
            corrupted.append(row["stored_name"])
    status["documents"] = {
        "directory": str(root), "same_filesystem_as_database": root.exists() and os.stat(root).st_dev == os.stat(database.parent).st_dev,
        "active_documents": documents, "attachments": len(rows), "missing_files": len(missing), "missing_examples": missing[:5],
        "hashes_verified": verify_hashes, "hash_mismatches": len(corrupted) if verify_hashes else None,
    }
    return jsonify(status)


@app.route("/api/admin/backup/database", methods=["GET"])
def admin_backup_database():
    if not admin_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    handle, snapshot_path = tempfile.mkstemp(suffix=".db", dir=str(Path(DB_FILE).resolve().parent))
    os.close(handle)
    source = sqlite3.connect(f"file:{Path(DB_FILE).resolve()}?mode=ro", uri=True, timeout=30.0)
    destination = sqlite3.connect(snapshot_path)
    try:
        source.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("Снимок базы не прошёл проверку целостности.")
    except Exception:
        os.unlink(snapshot_path)
        raise
    finally:
        destination.close()
        source.close()
    response = send_file(snapshot_path, as_attachment=True, download_name="smartreport_server_snapshot.db")

    def remove_snapshot():
        try:
            os.unlink(snapshot_path)
        except OSError:
            pass

    response.call_on_close(remove_snapshot)
    return response


@app.route("/api/expense-objects/<int:object_id>/expenses", methods=["GET"])
def expense_object_summary_api(object_id):
    if not expense_api_authorized():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    for date_value in (date_from, date_to):
        if date_value:
            try:
                datetime.strptime(date_value, "%Y-%m-%d")
            except ValueError:
                return jsonify({"ok": False, "error": "Даты должны быть в формате YYYY-MM-DD."}), 400
    if date_from and date_to and date_from > date_to:
        return jsonify({"ok": False, "error": "Начало периода позже его окончания."}), 400

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        object_row = cursor.execute(
            "SELECT id, name, markning, company FROM meta_objects WHERE id = ?", (object_id,)
        ).fetchone()
        if object_row is None:
            return jsonify({"ok": False, "error": "Объект не найден."}), 404
        conditions = ["a.object_id = ?", "d.is_deleted = 0"]
        params = [object_id]
        if date_from:
            conditions.append("d.document_date >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("d.document_date <= ?")
            params.append(date_to)
        rows = cursor.execute(f"""
            SELECT DISTINCT d.*
            FROM expense_documents d
            JOIN expense_allocations a ON a.document_id = d.id
            WHERE {' AND '.join(conditions)}
            ORDER BY d.document_date DESC, d.id DESC LIMIT 1000
        """, params).fetchall()

        totals = {}
        draft_count = review_count = 0
        documents = []
        for row in rows:
            document = serialize_expense_document(cursor, row)
            object_allocations = [
                item for item in document["allocations"] if item["object_id"] == object_id
            ]
            object_gross_values = [parse_decimal(item["gross_amount"]) for item in object_allocations]
            object_gross = sum(object_gross_values, Decimal(0)) if all(value is not None for value in object_gross_values) else None
            object_net_values = [parse_decimal(item["net_amount"]) for item in object_allocations]
            object_vat_values = [parse_decimal(item["vat_amount"]) for item in object_allocations]
            object_net = (
                sum(object_net_values, Decimal(0))
                if document["net_amount"] is not None and all(value is not None for value in object_net_values)
                else None
            )
            object_vat = (
                sum(object_vat_values, Decimal(0))
                if document["vat_amount"] is not None and all(value is not None for value in object_vat_values)
                else None
            )
            document["object_gross"] = decimal_text(object_gross)
            document["object_net"] = decimal_text(object_net)
            document["object_vat"] = decimal_text(object_vat)
            if document["verification_status"] == "confirmed":
                currency = document["currency_code"]
                bucket = totals.setdefault(currency, {
                    "gross": Decimal(0), "net": Decimal(0), "vat": Decimal(0),
                    "materials": Decimal(0), "other": Decimal(0),
                    "gross_known": True, "vat_known": True,
                })
                if object_gross is not None:
                    bucket["gross"] += object_gross
                else:
                    bucket["gross_known"] = False
                if object_net is not None:
                    bucket["net"] += object_net
                if object_vat is not None:
                    bucket["vat"] += object_vat
                else:
                    bucket["vat_known"] = False
                category = document["expense_category"].casefold()
                bucket["materials" if "материал" in category or "material" in category else "other"] += object_gross or Decimal(0)
            elif document["verification_status"] == "draft":
                draft_count += 1
            else:
                review_count += 1
            documents.append(document)

        summary = {
              currency: {key: (decimal_text(value) if
                           (key not in {"gross", "materials", "other"} or amounts["gross_known"])
                           and (key != "vat" or amounts["vat_known"]) else None)
                       for key, value in amounts.items() if key not in {"gross_known", "vat_known"}}
            for currency, amounts in totals.items()
        }
        return jsonify({
            "ok": True, "object": dict(object_row), "summary": summary,
            "draft_count": draft_count, "review_count": review_count,
            "documents": documents,
        })
    finally:
        conn.close()

@app.route('/get-active-employees', methods=['GET'])
def get_active_employees():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM meta_employees WHERE is_active = 1 ORDER BY name")
    rows = cursor.fetchall()
    conn.close()
    
    employees = [{"id": r['id'], "name": r['name']} for r in rows]
    return jsonify({"ok": True, "employees": employees})


@app.route('/api/sync/employee-catalog', methods=['GET'])
def sync_employee_catalog_api():
    if not verify_sync_key():
        return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
    conn = get_db_connection()
    try:
        rows = conn.execute("""
            SELECT e.id, e.name, e.salary_rate, e.is_active,
                   COALESCE((
                       SELECT MAX(event.revision)
                       FROM employee_sync_events event
                       WHERE event.employee_key = normalize_employee_name(e.name)
                   ), 0) AS sync_version
            FROM meta_employees e
            ORDER BY e.name, e.id
        """).fetchall()
        return jsonify({"ok": True, "employees": [dict(row) for row in rows]})
    finally:
        conn.close()

@app.route('/check-user-pin', methods=['POST'])
def check_user_pin():
    data = request.json or {}
    username = str(data.get('username', '')).strip()
    if not username:
        return jsonify({"ok": False, "error": "Не указано имя"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT password_hash FROM users WHERE username = ? AND is_active = 1", (username,))
    user = cursor.fetchone()
    conn.close()

    if not user:
        return jsonify({"ok": True, "has_pin": False, "is_new": True})

    has_pin = bool(user['password_hash'])
    return jsonify({"ok": True, "has_pin": has_pin, "is_new": not has_pin})

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Неверные данные для входа"}), 400
    username = data.get('username', '')
    password = data.get('password', '')

    if not isinstance(username, str) or not isinstance(password, str):
        return jsonify({"ok": False, "error": "Неверные данные для входа"}), 400
    username = username.strip()
    if not username or not password or len(username) > 100 or len(password) > 200:
        return jsonify({"ok": False, "error": "Неверные данные для входа"}), 400

    try:
        attempt_id = web_login_throttle.reserve(username, request.remote_addr or "unknown")
    except sqlite3.Error:
        return jsonify({"ok": False, "error": "Вход временно недоступен. Повторите позже."}), 503
    if attempt_id is None:
        return jsonify({
            "ok": False, "error": "Слишком много неудачных попыток. Повторите через несколько минут.",
        }), 429, {"Retry-After": str(math.ceil(web_login_throttle.window))}

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (username,))
    user = cursor.fetchone()

    if not user:
        conn.close()
        return jsonify({"ok": False, "error": "Пользователь не найден в системе"}), 401

    elif not user['password_hash']:
        conn.close()
        return jsonify({
            "ok": False,
            "code": "pin_not_provisioned",
            "error": "ПИН ещё не назначен. Обратитесь к администратору: "
                     "первый ПИН задаётся в справочнике сотрудников.",
        }), 403

    elif not check_password_hash(user['password_hash'], password):
        conn.close()
        return jsonify({"ok": False, "error": "Неверный пин-код или пароль"}), 401

    emp_id = user['employee_id']
    if user['role'] != 'admin':
        active_profiles = find_employee_profiles(cursor, user['username'], active_only=True)
        if emp_id:
            profile = next((item for item in active_profiles if item['id'] == emp_id), None)
        else:
            profile = active_profiles[0] if len(active_profiles) == 1 else None
        if profile is None:
            conn.close()
            return jsonify({"ok": False, "error": "Профиль сотрудника не найден или неактивен"}), 401
        emp_id = profile['id']
        if user['employee_id'] != emp_id:
            cursor.execute("UPDATE users SET employee_id = ? WHERE id = ?", (emp_id, user['id']))
            conn.commit()

    conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), user['id']))
    conn.commit()
    conn.close()

    try:
        web_login_throttle.complete_success(attempt_id)
    except sqlite3.Error:
        return jsonify({"ok": False, "error": "Вход временно недоступен. Повторите позже."}), 503
    session.clear()
    session.permanent = True
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['role'] = user['role']
    session['employee_id'] = emp_id
    from sync_dataset import file_dataset
    session["dataset_id"] = file_dataset(DB_FILE)

    return jsonify({
        "ok": True,
        "message": "Успешный вход",
        "user": {
            "username": user['username'],
            "role": user['role'],
            "employee_id": emp_id
        }
    })

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"ok": True, "message": "Выход выполнен"})

@app.route('/me', methods=['GET'])
def get_current_user():
    if 'user_id' not in session:
        return jsonify({"ok": False, "error": "Не авторизован"}), 401
    if session.get('role') != 'admin':
        conn = get_db_connection()
        try:
            resolve_session_employee(conn.cursor())
        except (PermissionError, ValueError) as error:
            return jsonify({"ok": False, "error": str(error)}), 403
        finally:
            conn.close()
    return jsonify({
        "ok": True,
        "user": {
            "username": session.get('username'),
            "role": session.get('role'),
            "employee_id": session.get('employee_id')
        }
    })

@app.route('/admin/set-user-pin', methods=['POST'])
def admin_set_user_pin():
    is_admin_session = session.get('role') == 'admin'
    is_valid_sync = verify_sync_key()
    
    if not is_admin_session and not is_valid_sync:
        return jsonify({"ok": False, "error": "Доступ запрещен"}), 403
        
    data = request.json or {}
    username = str(data.get('username', '')).strip()
    pin = str(data.get('pin', '')).strip()
    
    if not username or not pin:
        return jsonify({"ok": False, "error": "Не указано имя пользователя или ПИН"}), 400
        
    hashed_pw = generate_password_hash(pin)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT id FROM meta_employees WHERE name = ?", (username,))
    emp_row = cursor.fetchone()
    emp_id = emp_row['id'] if emp_row else None

    cursor.execute("UPDATE users SET password_hash = ?, employee_id = COALESCE(employee_id, ?) WHERE username = ?", (hashed_pw, emp_id, username))
    
    if cursor.rowcount == 0:
        now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('''
            INSERT INTO users (username, password_hash, role, employee_id, is_active, created_at)
            VALUES (?, ?, 'user', ?, 1, ?)
        ''', (username, hashed_pw, emp_id, now_str))
        
    conn.commit()
    conn.close()
    
    return jsonify({"ok": True, "message": f"Пин-код для пользователя {username} успешно установлен"})

@app.route('/admin/get-users-status', methods=['GET'])
def admin_get_users_status():
    is_admin_session = session.get('role') == 'admin'
    is_valid_sync = verify_sync_key()

    if not is_admin_session and not is_valid_sync:
        return jsonify({"ok": False, "error": "Доступ запрещен"}), 403

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT username, role, is_active, created_at, last_login, (password_hash IS NOT NULL AND password_hash != '') as has_pin FROM users")
    rows = cursor.fetchall()
    conn.close()

    users = [{
        "username": r['username'],
        "role": r['role'],
        "is_active": r['is_active'],
        "created_at": r['created_at'],
        "last_login": r['last_login'],
        "has_pin": bool(r['has_pin'])
    } for r in rows]

    return jsonify({"ok": True, "users": users})

@app.route('/')
def index():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM meta_employees WHERE is_active = 1 ORDER BY name")
    employees = [row['name'] for row in cursor.fetchall()]
    if not employees:
        employees = ["Aliaksei Patonich"]

    cursor.execute("SELECT name FROM meta_companies ORDER BY name")
    companies = [row['name'] for row in cursor.fetchall()]
    if not companies:
        companies = ["Privat"]

    cursor.execute("SELECT name, markning, company FROM meta_objects WHERE is_active = 1 ORDER BY company, name")
    obj_rows = cursor.fetchall()
    
    objects = []
    seen = set()
    for r in obj_rows:
        name, mark, comp = safe_parse_obj(r['name'])
        if not name or "{" in name:
            continue

        if r['markning'] and not mark:
            mark = str(r['markning']).strip()
        if r['company'] and comp == "Privat":
            comp = str(r['company']).strip()

        key = (name, comp)
        if key in seen:
            continue
        seen.add(key)

        display_str = f"{mark} | {name}" if mark else name
        if comp and comp != 'Privat':
            display_str += f" [{comp}]"
            
        objects.append({
            "name": name,
            "markning": mark,
            "company": comp,
            "display": display_str
        })

    conn.close()
    return render_template('index.html', employees=employees, companies=companies, objects=objects)

@app.route('/submit-shift', methods=['POST'])
def submit_shift():
    if 'user_id' not in session:
        return jsonify({"ok": False, "error": "Требуется авторизация"}), 401

    data = request.json
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Неверный формат данных"}), 400

    try:
        submission_type = resolve_submission_type(data)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    date_str = str(data.get('date', '')).strip()
    company = str(data.get('company', '')).strip()
    object_name = str(data.get('object_name', '')).strip()
    request_id = str(data.get('request_id') or uuid.uuid4().hex).strip()
    if not request_id or len(request_id) > 120:
        return jsonify({"ok": False, "error": "Некорректный идентификатор запроса"}), 400
    comment = str(data.get('comment', ''))[:2000]

    conn = get_db_connection()
    cursor = conn.cursor()
    ob_result = None

    if session.get('role') == 'admin':
        employee = str(data.get('employee', '')).strip()
        if not employee:
            conn.close()
            return jsonify({"ok": False, "error": "Администратор должен указать сотрудника"}), 400
    else:
        emp_id = session.get('employee_id')
        if not emp_id:
            conn.close()
            return jsonify({"ok": False, "error": "Ошибка сессии: не найден ID сотрудника"}), 403
        
        cursor.execute("SELECT name FROM meta_employees WHERE id = ? AND is_active = 1", (emp_id,))
        emp_row = cursor.fetchone()
        if not emp_row:
            conn.close()
            return jsonify({"ok": False, "error": "Сотрудник не найден в базе или деактивирован"}), 403
        employee = emp_row['name']

    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        conn.close()
        return jsonify({"ok": False, "error": "Некорректная дата (ожидается формат YYYY-MM-DD)"}), 400

    if datetime.strptime(date_str, '%Y-%m-%d').date() > datetime.now().date() + timedelta(days=7):
        conn.close()
        return jsonify({"ok": False, "error": "Нельзя отправлять смены более чем на 7 дней вперед"}), 400

    if submission_type == "ob":
        if not ob_schema_ready(cursor):
            conn.close()
            return jsonify({"ok": False, "error": "OB-схема не подготовлена. Выполните migrate_ob.py с backup."}), 503
        try:
            ob_result = calculate_ob_payload(
                cursor, date_str, data.get("start_time"), data.get("end_time"), employee,
                company=company, object_name=object_name,
            )
            if not ob_result["base_rate_configured"]:
                conn.close()
                return jsonify({"ok": False, "error": "Не удалось рассчитать OB-смену. Обратитесь к администратору."}), 400
            date_str = ob_result["date"]
            hours = ob_result["hours"]
            rate = 0.0
            transport = submission_transport(data.get("transport", 0), submission_type)
        except (TypeError, ValueError) as error:
            conn.close()
            return jsonify({"ok": False, "error": str(error)}), 400
    else:
        try:
            hours = float(data.get('hours'))
            if not (0.5 <= hours <= 24) or (hours % 0.5 != 0) or str(hours).lower() in ['nan', 'inf', '-inf']:
                raise ValueError()
        except (TypeError, ValueError):
            conn.close()
            return jsonify({"ok": False, "error": "Некорректное значение часов (от 0.5 до 24, шаг 0.5)"}), 400

        try:
            rate = round(float(data.get('rate', 0.0)), 2)
            transport = round(float(data.get('transport', 0.0)), 2)
            if rate < 0 or transport < 0 or rate > 100000 or transport > 10000:
                raise ValueError()
            if str(rate).lower() in ['nan', 'inf', '-inf'] or str(transport).lower() in ['nan', 'inf', '-inf']:
                raise ValueError()
        except (TypeError, ValueError):
            conn.close()
            return jsonify({"ok": False, "error": "Некорректные числовые значения ставки или транспорта"}), 400

        if submission_type != "work":
            rate = 0.0
            transport = 0.0

    try:
        cursor.execute("SELECT id FROM meta_employees WHERE name = ? AND is_active = 1", (employee,))
        if not cursor.fetchone():
            conn.close()
            return jsonify({"ok": False, "error": "Указанный сотрудник не найден или неактивен"}), 400

        cursor.execute("SELECT id FROM meta_companies WHERE name = ?", (company,))
        if not cursor.fetchone():
            conn.close()
            return jsonify({"ok": False, "error": "Указанная фирма не найдена"}), 400

        parsed_obj_name, _, _ = safe_parse_obj(object_name)
        final_obj_name = parsed_obj_name or object_name

        try:
            validate_submission_object(cursor, submission_type, company, final_obj_name)
        except ValueError as e:
            conn.close()
            return jsonify({"ok": False, "error": str(e)}), 400

        cursor.execute("BEGIN IMMEDIATE")
        payload = {
            "date": date_str, "employee": employee, "company": company, "object_name": final_obj_name,
            "hours": hours, "rate": rate, "transport": transport, "comment": comment,
            "submission_type": submission_type
        }
        if ob_result:
            payload.update({
                "start_time": ob_result["start_time"], "end_time": ob_result["end_time"],
                "ob_base_rate": ob_result["ob_base_rate"],
            })
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()
        cursor.execute("SELECT id, payload_hash, submission_type FROM incoming_submissions WHERE request_id = ?", (request_id,))
        existing = cursor.fetchone()
        if existing:
            conn.rollback()
            legacy_payload = dict(payload)
            legacy_payload.pop('submission_type', None)
            legacy_payload_hash = hashlib.sha256(json.dumps(legacy_payload, sort_keys=True).encode('utf-8')).hexdigest()
            if existing['payload_hash'] == payload_hash or (
                'submission_type' not in data and (existing['submission_type'] or 'work') == submission_type
                and existing['payload_hash'] == legacy_payload_hash
            ):
                return jsonify({"ok": True, "idempotent": True, "request_id": request_id}), 200
            return jsonify({"ok": False, "error": "request_id уже использован для другой смены"}), 409

        sixty_secs_ago = (datetime.utcnow() - timedelta(seconds=60)).strftime('%Y-%m-%d %H:%M:%S')
        if ob_result:
            cursor.execute('''
                SELECT id FROM incoming_submissions
                WHERE employee = ? AND date = ? AND company = ? AND object_name = ? AND hours = ?
                  AND submission_type = ? AND start_time = ? AND end_time = ? AND created_at >= ? AND is_deleted = 0
            ''', (employee, date_str, company, final_obj_name, hours, submission_type,
                  ob_result["start_time"], ob_result["end_time"], sixty_secs_ago))
        else:
            cursor.execute('''
                SELECT id FROM incoming_submissions
                WHERE employee = ? AND date = ? AND company = ? AND object_name = ? AND hours = ?
                  AND submission_type = ? AND created_at >= ? AND is_deleted = 0
            ''', (employee, date_str, company, final_obj_name, hours, submission_type, sixty_secs_ago))
        if cursor.fetchone():
            conn.rollback()
            return jsonify({"ok": False, "error": "Похожая смена уже была зарегистрирована только что. Подождите немного."}), 409

        change_seq = next_change_sequence(cursor)
        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        if ob_result:
            cursor.execute('''
                INSERT INTO incoming_submissions (
                    request_id, sync_id, submission_type, date, employee, company, object_name,
                    hours, rate, transport, comment, version, change_seq, is_deleted,
                    desktop_acked, source, last_operation_id, payload_hash, created_at, updated_at,
                    start_time, end_time, ob_base_rate
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, 0, 'web', NULL, ?, ?, ?, ?, ?, ?)
            ''', (
                request_id, request_id, submission_type, date_str, employee, company, final_obj_name,
                hours, rate, transport, comment, change_seq, payload_hash, now, now,
                ob_result["start_time"], ob_result["end_time"], ob_result["ob_base_rate"],
            ))
            replace_ob_segments(cursor, request_id, ob_result["segments"])
        else:
            cursor.execute('''
                INSERT INTO incoming_submissions (
                    request_id, sync_id, submission_type, date, employee, company, object_name,
                    hours, rate, transport, comment, version, change_seq, is_deleted,
                    desktop_acked, source, last_operation_id, payload_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, 0, 'web', NULL, ?, ?, ?)
            ''', (
                request_id, request_id, submission_type, date_str, employee, company, final_obj_name,
                hours, rate, transport, comment, change_seq, payload_hash, now, now
            ))
        if submission_type in ("work", "ob"):
            quote = agreed_prices_for_date(cursor, "incoming_submissions", {
                "date": date_str, "company": company, "object_name": final_obj_name,
            })
            if quote:
                store_pricing(cursor, "incoming_submissions", request_id, quote)
        conn.commit()
    except ValueError as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка при сохранении смены: {str(e)}")
        return jsonify({"ok": False, "error": "Внутренняя ошибка сервера при сохранении смены."}), 500
    finally:
        conn.close()

    response = {"ok": True, "message": "Смена успешно сохранена!", "request_id": request_id}
    if ob_result:
        response.update(public_ob_result(ob_result))
    return jsonify(response), 201


@app.route('/calculate-ob', methods=['POST'])
def calculate_ob_preview():
    if 'user_id' not in session:
        return jsonify({"ok": False, "error": "Требуется авторизация"}), 401
    data = request.json
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Неверный формат данных"}), 400
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        requested_employee = data.get("employee")
        logger.info(
            "OB preview request employee_name=%r role=%r session_username=%r session_employee_id=%r",
            requested_employee, session.get("role"), session.get("username"), session.get("employee_id"),
        )
        employee = resolve_session_employee(cursor, requested_employee)
        logger.info(
            "OB preview resolved employee_name=%r session_employee_id=%r",
            employee, session.get("employee_id"),
        )
        result = calculate_ob_payload(
            cursor, data.get("date"), data.get("start_time"), data.get("end_time"), employee,
            company=data.get("company"), object_name=data.get("object_name"),
        )
    except PermissionError as error:
        return jsonify({"ok": False, "error": str(error)}), 403
    except (TypeError, ValueError) as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    finally:
        conn.close()
    return jsonify({"ok": True, **public_ob_result(result)})

@app.route('/edit-shift', methods=['POST'])
def edit_shift():
    if 'user_id' not in session:
        return jsonify({"ok": False, "error": "Требуется авторизация"}), 401
    data = request.json
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Неверный формат данных"}), 400
    sync_id = str(data.get('sync_id') or '').strip()
    operation_id = str(data.get('operation_id') or uuid.uuid4().hex).strip()
    if not sync_id or len(sync_id) > 120 or not operation_id or len(operation_id) > 120:
        return jsonify({"ok": False, "error": "Отсутствует корректный sync_id или operation_id"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    ob_result = None
    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT * FROM incoming_submissions WHERE sync_id = ?", (sync_id,))
        existing = cursor.fetchone()
        if not existing:
            conn.rollback()
            return jsonify({"ok": False, "error": "Смена не найдена"}), 404
        if existing['is_deleted']:
            conn.rollback()
            return jsonify({"ok": False, "error": "Удалённую смену нельзя редактировать", "reason": "deleted"}), 409

        employee = resolve_session_employee(cursor, data.get('employee', existing['employee']))
        if employee != existing['employee'] and session.get('role') != 'admin':
            conn.rollback()
            return jsonify({"ok": False, "error": "Нельзя изменять записи другого сотрудника"}), 403
        try:
            expected_version = int(data.get('version', existing['version']))
        except (TypeError, ValueError):
            raise ValueError("Некорректная версия смены.")
        submission_type = resolve_submission_type(data, existing['submission_type'] or 'work')
        date_val = str(data.get('date', existing['date'])).strip()
        datetime.strptime(date_val, '%Y-%m-%d')
        company = str(data.get('company', existing['company'])).strip()
        object_name = str(data.get('object_name', existing['object_name'])).strip()
        validate_submission_object(cursor, submission_type, company, object_name)
        if submission_type == "ob":
            if not ob_schema_ready(cursor):
                conn.rollback()
                return jsonify({"ok": False, "error": "OB-схема не подготовлена. Выполните migrate_ob.py с backup."}), 503
            ob_result = calculate_ob_payload(
                cursor,
                date_val,
                data.get("start_time", existing["start_time"]),
                data.get("end_time", existing["end_time"]),
                employee,
                existing=existing,
                company=company, object_name=object_name,
            )
            if not ob_result["base_rate_configured"]:
                raise ValueError("Не удалось пересчитать OB-смену. Обратитесь к администратору.")
            date_val = ob_result["date"]
            hours = ob_result["hours"]
            transport = submission_transport(data.get("transport", existing["transport"]), submission_type)
        else:
            hours = float(data.get('hours', existing['hours']))
            transport = round(float(data.get('transport', existing['transport'])), 2)
            if not (0.5 <= hours <= 24) or hours % 0.5 != 0 or not (0 <= transport <= 10000):
                raise ValueError("Часы или транспорт вне допустимого диапазона.")
            if any(str(value).lower() in ('nan', 'inf', '-inf') for value in (hours, transport)):
                raise ValueError("Числовое поле должно быть конечным.")
        comment = str(data.get('comment', existing['comment'] or ''))[:2000]
        invoiced = int(existing['invoiced']) if submission_type in ('work', 'ob') else 0
        if existing["invoiced"]:
            raise ValueError("Выставленная запись защищена. Сначала верните её в работу в приложении.")
        normalized = {
            "date": date_val, "employee": employee, "company": company,
            "object_name": object_name, "hours": hours, "rate": existing['rate'],
            "transport": transport, "comment": comment, "invoiced": invoiced,
            "submission_type": submission_type
        }
        if ob_result:
            normalized.update({
                "start_time": ob_result["start_time"], "end_time": ob_result["end_time"],
                "ob_base_rate": ob_result["ob_base_rate"],
            })
        operation_hash = hashlib.sha256(json.dumps(
            {"operation": "upsert", "shift": normalized}, sort_keys=True
        ).encode('utf-8')).hexdigest()
        if existing['last_operation_id'] == operation_id:
            if existing['payload_hash'] == operation_hash:
                conn.rollback()
                return jsonify({"ok": True, "idempotent": True, "sync_id": sync_id, "version": existing['version']}), 200
            conn.rollback()
            return jsonify({"ok": False, "error": "operation_id уже использован для другого изменения"}), 409
        if expected_version != existing['version']:
            conn.rollback()
            return jsonify({"ok": False, "error": "Смена уже изменена; обновите историю", "conflict": True, "current": serialize_submission(existing)}), 409

        change_seq = next_change_sequence(cursor)
        new_version = existing['version'] + 1
        if ob_schema_ready(cursor):
            cursor.execute('''
                UPDATE incoming_submissions SET
                    date = ?, employee = ?, company = ?, object_name = ?, hours = ?,
                    transport = ?, comment = ?, invoiced = ?, submission_type = ?,
                    start_time = ?, end_time = ?, ob_base_rate = ?,
                    version = ?, change_seq = ?, desktop_acked = 0,
                    last_operation_id = ?, payload_hash = ?, updated_at = ?
                WHERE id = ? AND is_deleted = 0
            ''', (
                date_val, employee, company, object_name, hours, transport, comment,
                invoiced, submission_type,
                ob_result["start_time"] if ob_result else None,
                ob_result["end_time"] if ob_result else None,
                ob_result["ob_base_rate"] if ob_result else None,
                new_version, change_seq, operation_id,
                operation_hash, datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), existing['id']
            ))
        else:
            cursor.execute('''
                UPDATE incoming_submissions SET
                    date = ?, employee = ?, company = ?, object_name = ?, hours = ?,
                    transport = ?, comment = ?, invoiced = ?, submission_type = ?,
                    version = ?, change_seq = ?, desktop_acked = 0,
                    last_operation_id = ?, payload_hash = ?, updated_at = ?
                WHERE id = ? AND is_deleted = 0
            ''', (
                date_val, employee, company, object_name, hours, transport, comment,
                invoiced, submission_type, new_version, change_seq, operation_id,
                operation_hash, datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), existing['id']
            ))
        if existing["customer_pricing"] and any(
            normalized[key] != existing[key] for key in ("date", "company", "object_name")
        ):
            store_pricing(cursor, "incoming_submissions", sync_id, None)
        if ob_result and not ob_result.get("preserved"):
            replace_ob_segments(cursor, sync_id, ob_result["segments"])
        elif not ob_result and ob_schema_ready(cursor) and existing['submission_type'] == "ob":
            cursor.execute("DELETE FROM ob_segments WHERE shift_id = ?", (sync_id,))
        conn.commit()
        response = {"ok": True, "sync_id": sync_id, "version": new_version, "change_seq": change_seq}
        if ob_result:
            response.update(public_ob_result(ob_result))
        return jsonify(response), 200
    except PermissionError as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)}), 403
    except (ValueError, TypeError, sqlite3.IntegrityError) as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception:
        conn.rollback()
        logger.exception("Ошибка редактирования смены в браузере")
        return jsonify({"ok": False, "error": "Не удалось изменить смену"}), 500
    finally:
        conn.close()

@app.route('/delete-shift', methods=['POST'])
def delete_shift():
    if 'user_id' not in session:
        return jsonify({"ok": False, "error": "Требуется авторизация"}), 401
    data = request.json
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Неверный формат данных"}), 400
    sync_id = str(data.get('sync_id') or '').strip()
    operation_id = str(data.get('operation_id') or uuid.uuid4().hex).strip()
    if not sync_id or len(sync_id) > 120 or not operation_id or len(operation_id) > 120:
        return jsonify({"ok": False, "error": "Отсутствует корректный sync_id или operation_id"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT * FROM incoming_submissions WHERE sync_id = ?", (sync_id,))
        existing = cursor.fetchone()
        if not existing:
            conn.rollback()
            return jsonify({"ok": False, "error": "Смена не найдена"}), 404
        employee = resolve_session_employee(cursor, data.get('employee', existing['employee']))
        if employee != existing['employee'] and session.get('role') != 'admin':
            conn.rollback()
            return jsonify({"ok": False, "error": "Нельзя удалять записи другого сотрудника"}), 403
        try:
            expected_version = int(data.get('version', existing['version']))
        except (TypeError, ValueError):
            raise ValueError("Некорректная версия смены.")
        submission_type = resolve_submission_type(data, existing['submission_type'] or 'work')
        if submission_type != (existing['submission_type'] or 'work'):
            raise ValueError("Тип удаления не совпадает с текущим типом записи.")
        operation_hash = hashlib.sha256(json.dumps(
            {"operation": "delete", "submission_type": submission_type}, sort_keys=True
        ).encode('utf-8')).hexdigest()
        if existing['last_operation_id'] == operation_id:
            if existing['payload_hash'] == operation_hash and existing['is_deleted']:
                conn.rollback()
                return jsonify({"ok": True, "idempotent": True, "sync_id": sync_id, "version": existing['version']}), 200
            conn.rollback()
            return jsonify({"ok": False, "error": "operation_id уже использован для другого изменения"}), 409
        if existing['is_deleted']:
            conn.rollback()
            return jsonify({"ok": True, "idempotent": True, "sync_id": sync_id, "version": existing['version']}), 200
        if expected_version != existing['version']:
            conn.rollback()
            return jsonify({"ok": False, "error": "Смена уже изменена; обновите историю", "conflict": True, "current": serialize_submission(existing)}), 409

        change_seq = next_change_sequence(cursor)
        new_version = existing['version'] + 1
        cursor.execute('''
            UPDATE incoming_submissions SET
                submission_type = ?, is_deleted = 1, version = ?, change_seq = ?,
                desktop_acked = 0, last_operation_id = ?, payload_hash = ?, updated_at = ?
            WHERE id = ? AND is_deleted = 0
        ''', (
            submission_type, new_version, change_seq, operation_id, operation_hash,
            datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), existing['id']
        ))
        if existing['submission_type'] == "ob" and ob_schema_ready(cursor):
            cursor.execute("DELETE FROM ob_segments WHERE shift_id = ?", (sync_id,))
        if existing['legacy_cloud_id'] is not None:
            cursor.execute("UPDATE cloud_shifts SET synced = 1 WHERE id = ?", (existing['legacy_cloud_id'],))
        conn.commit()
        return jsonify({"ok": True, "sync_id": sync_id, "version": new_version, "change_seq": change_seq, "submission_type": submission_type}), 200
    except PermissionError as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)}), 403
    except (ValueError, TypeError, sqlite3.IntegrityError) as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception:
        conn.rollback()
        logger.exception("Ошибка удаления смены в браузере")
        return jsonify({"ok": False, "error": "Не удалось удалить смену"}), 500
    finally:
        conn.close()

@app.route('/delete-cloud-shift', methods=['POST'])
def delete_cloud_shift():
    if not verify_sync_key():
        return jsonify({"ok": False, "error": "Доступ запрещен"}), 403

    data = request.json or {}
    shift_id = data.get('id')
    sync_id = str(data.get('sync_id') or '').strip()
    date = data.get('date')
    employee = data.get('employee')
    object_name = data.get('object_name')
    hours = data.get('hours')

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        if isinstance(shift_id, str) and shift_id.startswith('submission:'):
            try:
                submission_id = int(shift_id.split(':', 1)[1])
            except ValueError:
                return jsonify({"ok": False, "error": "Некорректный ID заявки"}), 400
            cursor.execute("SELECT * FROM incoming_submissions WHERE id = ?", (submission_id,))
            submission = cursor.fetchone()
        elif sync_id:
            cursor.execute("SELECT * FROM incoming_submissions WHERE sync_id = ?", (sync_id,))
            submission = cursor.fetchone()
        elif shift_id is not None:
            try:
                legacy_id = int(shift_id)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "Некорректный ID смены"}), 400
            cursor.execute("SELECT * FROM incoming_submissions WHERE legacy_cloud_id = ?", (legacy_id,))
            submission = cursor.fetchone()
        else:
            submission = None

        if submission:
            if not submission['is_deleted']:
                change_seq = next_change_sequence(cursor)
                cursor.execute('''
                    UPDATE incoming_submissions
                    SET is_deleted = 1, version = version + 1, change_seq = ?, updated_at = ?
                    WHERE id = ?
                ''', (change_seq, datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), submission['id']))
            if submission['legacy_cloud_id'] is not None:
                cursor.execute("UPDATE cloud_shifts SET synced = 1 WHERE id = ?", (submission['legacy_cloud_id'],))
        elif date and employee and object_name and hours is not None:
            cursor.execute('''
                SELECT * FROM incoming_submissions
                WHERE date = ? AND employee = ? AND object_name = ? AND hours = ? AND is_deleted = 0
            ''', (date, employee, object_name, hours))
            matches = cursor.fetchall()
            if len(matches) != 1:
                conn.close()
                return jsonify({"ok": False, "error": "Удаление по полям неоднозначно; требуется стабильный ID"}), 409
            submission = matches[0]
            change_seq = next_change_sequence(cursor)
            cursor.execute('''
                UPDATE incoming_submissions
                SET is_deleted = 1, version = version + 1, change_seq = ?, updated_at = ?
                WHERE id = ?
            ''', (change_seq, datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), submission['id']))
            if submission['legacy_cloud_id'] is not None:
                cursor.execute("UPDATE cloud_shifts SET synced = 1 WHERE id = ?", (submission['legacy_cloud_id'],))
        
        elif not submission:
            conn.close()
            return jsonify({"ok": False, "error": "Смена не найдена"}), 404

        conn.commit()
        conn.close()
        return jsonify({"ok": True, "message": "Смена успешно удалена на сервере"})
    except Exception as e:
        conn.close()
        logger.error(f"Ошибка удаления смены на сервере: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/check-employee-shifts', methods=['POST'])
def check_employee_shifts():
    if 'user_id' not in session:
        return jsonify({"ok": False, "error": "Требуется авторизация"}), 401

    data = request.json or {}
    selected_month = data.get('month')

    conn = get_db_connection()
    cursor = conn.cursor()

    if session.get('role') == 'admin':
        emp = data.get('employee')
        if not emp:
            conn.close()
            return jsonify({"ok": False, "error": "Не указан сотрудник"}), 400
    else:
        emp_id = session.get('employee_id')
        if not emp_id:
            conn.close()
            return jsonify({"ok": False, "error": "Доступ запрещен"}), 403
        
        cursor.execute("SELECT name FROM meta_employees WHERE id = ?", (emp_id,))
        emp_row = cursor.fetchone()
        if not emp_row:
            conn.close()
            return jsonify({"ok": False, "error": "Сотрудник не найден"}), 403
        emp = emp_row['name']

    if selected_month:
        try:
            year, month = map(int, selected_month.split('-'))
            start_date = datetime(year, month, 1).strftime('%Y-%m-%d')
            if month == 12:
                end_date = datetime(year + 1, 1, 1).strftime('%Y-%m-%d')
            else:
                end_date = datetime(year, month + 1, 1).strftime('%Y-%m-%d')
            
            cursor.execute("""
                SELECT date, employee, company, object_name, hours, rate, transport, comment, invoiced, sync_id, version, change_seq, submission_type FROM incoming_submissions
                WHERE employee = ? AND date >= ? AND date < ? AND is_deleted = 0
                ORDER BY date ASC, id ASC
            """, (emp, start_date, end_date))
        except Exception:
            cursor.execute("SELECT date, employee, company, object_name, hours, rate, transport, comment, invoiced, sync_id, version, change_seq, submission_type FROM incoming_submissions WHERE employee = ? AND is_deleted = 0 ORDER BY date DESC LIMIT 31", (emp,))
    else:
        cursor.execute("SELECT date, employee, company, object_name, hours, rate, transport, comment, invoiced, sync_id, version, change_seq, submission_type FROM incoming_submissions WHERE employee = ? AND is_deleted = 0 ORDER BY date DESC LIMIT 31", (emp,))

    rows = cursor.fetchall()
    
    shifts = []
    for r in rows:
        obj_name = r['object_name']
        marking = ""
        cursor.execute("SELECT markning FROM meta_objects WHERE name = ? AND company = ?", (obj_name, r['company']))
        obj_meta = cursor.fetchone()
        if obj_meta and obj_meta['markning']:
            marking = obj_meta['markning']
            
        shifts.append({
            "date": r['date'], 
            "employee": r['employee'],
            "object_name": obj_name, 
            "marking": marking,
            "hours": r['hours'],
            "transport": r['transport'],
            "company": r['company'],
            "comment": r['comment'] or "",
            "invoiced": r['invoiced'],
            "sync_id": r['sync_id'],
            "version": r['version'],
            "change_seq": r['change_seq'],
            "submission_type": r['submission_type'] or "work"
        })
        if r['submission_type'] == "ob" and ob_schema_ready(cursor):
            cursor.execute(
                "SELECT start_time, end_time, ob_base_rate FROM incoming_submissions WHERE sync_id = ?",
                (r['sync_id'],),
            )
            ob_record = cursor.fetchone()
            if ob_record:
                segments = get_ob_segments(cursor, r['sync_id'])
                public_segments = [
                    {key: segment[key] for key in ("start_time", "end_time", "hours")}
                    for segment in segments
                ]
                shifts[-1].update({
                    "start_time": ob_record["start_time"],
                    "end_time": ob_record["end_time"],
                    "ob_segments": public_segments,
                })
        
    conn.close()
    return jsonify({"ok": True, "recent_shifts": shifts})

@app.route('/check-missing-shifts', methods=['POST'])
def check_missing_shifts():
    if 'user_id' not in session:
        return jsonify({"ok": False, "error": "Требуется авторизация"}), 401

    conn = get_db_connection()
    cursor = conn.cursor()
    
    if session.get('role') == 'admin':
        data = request.json or {}
        emp = data.get('employee')
        if not emp:
            conn.close()
            return jsonify({"ok": False, "error": "Не указан сотрудник"}), 400
    else:
        emp_id = session.get('employee_id')
        if not emp_id:
            conn.close()
            return jsonify({"ok": False, "error": "Доступ запрещен"}), 403
        cursor.execute("SELECT name FROM meta_employees WHERE id = ?", (emp_id,))
        emp_row = cursor.fetchone()
        if not emp_row:
            conn.close()
            return jsonify({"ok": False, "error": "Сотрудник не найден"}), 403
        emp = emp_row['name']
    
    today = datetime.utcnow().date()
    start_check = today - timedelta(days=10)
    
    cursor.execute("""
        SELECT date FROM incoming_submissions
        WHERE employee = ? AND date >= ? AND date <= ? AND is_deleted = 0 AND submission_type IN ('work', 'ob')
    """, (emp, start_check.strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')))
    
    existing_dates = {row['date'] for row in cursor.fetchall()}
    conn.close()

    missing_days = []
    current = start_check
    while current < today:
        if current.weekday() < 5:
            date_str = current.strftime('%Y-%m-%d')
            if date_str not in existing_dates:
                missing_days.append(date_str)
        current += timedelta(days=1)

    has_gaps = len(missing_days) > 0
    return jsonify({
        "ok": True, 
        "has_gaps": has_gaps, 
        "missing_days": missing_days,
        "message": f"Внимание! За прошлые рабочие дни не заполнено смен: {len(missing_days)}." if has_gaps else ""
    })


def fetch_arbetsdagbok_records(cursor, employee, selected_month):
    try:
        month_start = datetime.strptime(selected_month, '%Y-%m')
    except ValueError:
        return None, "Укажите месяц в формате YYYY-MM"
    if month_start.strftime('%Y-%m') != selected_month:
        return None, "Укажите месяц в формате YYYY-MM"
    if month_start.month == 12:
        month_end = datetime(month_start.year + 1, 1, 1)
    else:
        month_end = datetime(month_start.year, month_start.month + 1, 1)

    cursor.execute('''
        SELECT date, company, object_name, hours, transport, comment, submission_type, sync_id
        FROM incoming_submissions
        WHERE normalize_employee_name(employee) = ?
          AND date >= ? AND date < ? AND is_deleted = 0
          AND submission_type IN ('work', 'ob', 'vacation', 'holiday')
        ORDER BY date ASC, id ASC
    ''', (
        normalize_employee_name(employee),
        month_start.strftime('%Y-%m-%d'),
        month_end.strftime('%Y-%m-%d'),
    ))
    raw_shifts = cursor.fetchall()

    type_labels = {
        'work': 'Arbete',
        'ob': 'Arbete (OB-post)',
        'vacation': 'Semester / Semestertimmar',
        'holiday': 'Röda dagar',
    }
    totals_by_type = {'work': 0.0, 'ob': 0.0, 'vacation': 0.0, 'holiday': 0.0}
    totals_by_object = {}
    formatted_shifts = []

    for shift in raw_shifts:
        sub_type = shift['submission_type'] or 'work'
        hours = float(shift['hours'] or 0.0)
        totals_by_type[sub_type if sub_type in totals_by_type else 'work'] += hours

        cursor.execute(
            "SELECT markning FROM meta_objects WHERE name = ? AND company = ?",
            (shift['object_name'], shift['company']),
        )
        object_meta = cursor.fetchone()
        raw_obj_name = shift['object_name'] or ''
        if object_meta and object_meta['markning']:
            obj_display = f"[{object_meta['markning']}] {raw_obj_name}"
        else:
            obj_display = raw_obj_name

        totals_by_object[obj_display] = round(totals_by_object.get(obj_display, 0.0) + hours, 2)

        ob_segments = []
        if sub_type == 'ob' and ob_schema_ready(cursor):
            segments = get_ob_segments(cursor, shift['sync_id'])
            for seg in segments:
                ob_segments.append({
                    'start_time': seg['start_time'],
                    'end_time': seg['end_time'],
                    'hours': float(seg['hours']),
                    'premium_percent': float(seg['premium_percent']),
                })

        formatted_shifts.append({
            'date': shift['date'] or '',
            'company': shift['company'] or '',
            'object_name': obj_display,
            'comment': shift['comment'] or '',
            'hours': hours,
            'transport': float(shift['transport'] or 0),
            'type_key': sub_type,
            'type_label': type_labels.get(sub_type, 'Arbete'),
            'ob_segments': ob_segments,
            'ob_premium_hours': (
                sum(segment['hours'] for segment in ob_segments if segment['premium_percent'] > 0)
                if ob_segments else None
            ),
        })

    total_hours = sum(totals_by_type.values())
    return {
        'employee': employee,
        'selected_month': selected_month,
        'shifts': formatted_shifts,
        'totals_by_type': totals_by_type,
        'totals_by_object': totals_by_object,
        'total_hours': total_hours,
        'total_transport': sum(shift['transport'] for shift in formatted_shifts),
        'type_labels': type_labels,
    }, None


@app.route('/report-preview', methods=['GET'])
def report_preview():
    if 'user_id' not in session:
        return redirect('/?login_required=1')

    selected_month = str(request.args.get('month') or '').strip()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if session.get('role') == 'admin':
            requested_employee = str(request.args.get('employee') or '').strip()
            profiles = find_employee_profiles(cursor, requested_employee, active_only=True)
            profile = select_employee_profile(profiles, preferred_name=requested_employee)
            if not profile:
                return "Сотрудник не найден или не активен", 400
            employee = profile['name']
        else:
            employee = resolve_session_employee(cursor)

        report_data, err = fetch_arbetsdagbok_records(cursor, employee, selected_month)
        if err:
            return err, 400

        return render_template('report_preview.html', **report_data)
    finally:
        conn.close()


@app.route('/download-arbetsdagbok.pdf', methods=['GET'])
def download_arbetsdagbok_pdf():
    if 'user_id' not in session:
        return jsonify({"ok": False, "error": "Требуется авторизация"}), 401

    selected_month = str(request.args.get('month') or '').strip()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if session.get('role') == 'admin':
            requested_employee = str(request.args.get('employee') or '').strip()
            profiles = find_employee_profiles(cursor, requested_employee, active_only=True)
            profile = select_employee_profile(profiles, preferred_name=requested_employee)
            if not profile:
                return jsonify({"ok": False, "error": "Укажите активного сотрудника"}), 400
            employee = profile['name']
        else:
            employee = resolve_session_employee(cursor)

        report_data, err = fetch_arbetsdagbok_records(cursor, employee, selected_month)
        if err:
            return jsonify({"ok": False, "error": err}), 400

        styles = getSampleStyleSheet()
        font_path_candidates = [
            os.environ.get('ARBETSDAGBOK_FONT_PATH'),
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf',
            os.path.join(os.path.dirname(reportlab.__file__), 'fonts', 'Vera.ttf'),
        ]
        bold_font_candidates = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf',
            os.path.join(os.path.dirname(reportlab.__file__), 'fonts', 'VeraBd.ttf'),
        ]
        font_path = next((path for path in font_path_candidates if path and os.path.isfile(path)), None)
        bold_font_path = next((path for path in bold_font_candidates if os.path.isfile(path)), None)
        if font_path:
            if 'Arbetsdagbok' not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont('Arbetsdagbok', font_path))
            if bold_font_path and 'Arbetsdagbok-Bold' not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont('Arbetsdagbok-Bold', bold_font_path))
            body_font = 'Arbetsdagbok'
            bold_font = 'Arbetsdagbok-Bold' if bold_font_path else body_font
        else:
            body_font = 'Helvetica'
            bold_font = 'Helvetica-Bold'

        title_style = ParagraphStyle(
            'ArbetsdagbokTitle', parent=styles['Title'], fontName=bold_font,
            fontSize=15, leading=19, alignment=TA_CENTER, spaceAfter=8,
        )
        meta_style = ParagraphStyle(
            'ArbetsdagbokMeta', parent=styles['BodyText'], fontName=body_font,
            fontSize=9, leading=12, spaceAfter=4,
        )
        cell_style = ParagraphStyle(
            'ArbetsdagbokCell', parent=styles['BodyText'], fontName=body_font,
            fontSize=8.5, leading=11,
        )
        interval_style = ParagraphStyle(
            'ArbetsdagbokInterval', parent=cell_style, fontSize=8, leading=10,
            leftIndent=4, spaceBefore=3,
        )
        hours_style = ParagraphStyle(
            'ArbetsdagbokHours', parent=cell_style, fontName=bold_font,
            alignment=TA_RIGHT,
        )

        type_labels = report_data['type_labels']
        totals_by_type = report_data['totals_by_type']
        table_data = [[
            Paragraph('Datum', cell_style),
            Paragraph('Objekt / Arbetsbeskrivning', cell_style),
            Paragraph('Typ', cell_style),
            Paragraph('Timmar totalt', hours_style),
        ]]

        for shift in report_data['shifts']:
            description_parts = [escape(shift['object_name'])]
            if shift['company']:
                description_parts.append(f"Firma: {escape(shift['company'])}")
            if shift['comment']:
                comment = escape(str(shift['comment'])).replace('\n', '<br/>')
                description_parts.append(f"Arbetsbeskrivning: {comment}")
            if shift['transport']:
                description_parts.append(f"Resa: {shift['transport']:g}")

            if shift['ob_segments']:
                interval_lines = [
                    f"{escape(segment['start_time'])}–{escape(segment['end_time'])} "
                    f"({float(segment['hours']):.2f} h; "
                    + (f"OB-tillägg {segment['premium_percent']:g}%"
                       if segment['premium_percent'] > 0 else "Utan OB-tillägg") + ")"
                    for segment in shift['ob_segments']
                ]
                interval_lines.append(f"<b>Hela passet: {float(shift['hours']):.2f} h</b>")
                interval_lines.append(f"Timmar med OB-tillägg: {shift['ob_premium_hours']:.2f} h")
                description_parts.append(
                    '<font size="8">Delintervaller (med/utan OB-tillägg):<br/>'
                    + '<br/>'.join(interval_lines) + '</font>'
                )
            elif shift['type_key'] == 'ob':
                description_parts.append('Timmar med OB-tillägg: uppgift saknas')

            table_data.append([
                Paragraph(escape(shift['date'] or ''), cell_style),
                Paragraph('<br/>'.join(description_parts), cell_style),
                Paragraph(shift['type_label'], cell_style),
                Paragraph(f"{float(shift['hours']):.2f} h", hours_style),
            ])

        output = io.BytesIO()
        document = SimpleDocTemplate(
            output, pagesize=A4, rightMargin=15 * mm, leftMargin=15 * mm,
            topMargin=15 * mm, bottomMargin=15 * mm,
            title=f"Arbetsdagbok {selected_month}", author="SmartReport",
        )
        story = [
            Paragraph('Arbetsdagbok', title_style),
            Paragraph(f"Medarbetare: {escape(employee)}", meta_style),
            Paragraph(f"Period: {escape(selected_month)}", meta_style),
            Spacer(1, 6 * mm),
        ]
        table = LongTable(table_data, colWidths=[20 * mm, 93 * mm, 35 * mm, 22 * mm], repeatRows=1)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e8edf2')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
            ('FONTNAME', (0, 0), (-1, 0), bold_font),
            ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#b8c2cc')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ALIGN', (-1, 0), (-1, -1), 'RIGHT'),
            ('LEFTPADDING', (0, 0), (-1, -1), 5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        story.append(table)
        story.append(Spacer(1, 8 * mm))
        total_work = totals_by_type.get('work', 0.0)
        total_ob = totals_by_type.get('ob', 0.0)
        total_vacation = totals_by_type.get('vacation', 0.0)
        total_holiday = totals_by_type.get('holiday', 0.0)
        story.append(Paragraph(f"<b>Arbetad tid i vanliga arbetsposter: {total_work:.2f} tim</b>", meta_style))
        story.append(Paragraph(f"<b>Arbetad tid i OB-poster (hela passet): {total_ob:.2f} tim</b>", meta_style))
        story.append(Paragraph(f"<b>Totalt semester: {total_vacation:.2f} tim</b>", meta_style))
        story.append(Paragraph(f"<b>Totalt röda dagar: {total_holiday:.2f} tim</b>", meta_style))
        document.build(story)
        output.seek(0)
    except Exception:
        logger.exception("Не удалось сформировать Arbetsdagbok PDF")
        return jsonify({"ok": False, "error": "Не удалось сформировать PDF-отчёт"}), 500
    finally:
        conn.close()

    return send_file(
        output,
        mimetype='application/pdf',
        as_attachment=False,
        download_name=f"Arbetsdagbok_{selected_month}.pdf",
        max_age=0,
    )


@app.route('/export-shifts-csv', methods=['POST'])
def export_shifts_csv():
    if 'user_id' not in session:
        return jsonify({"ok": False, "error": "Требуется авторизация"}), 401

    data = request.json or {}
    selected_month = data.get('month')
    empName = data.get('employee')

    if not selected_month or not empName:
        return jsonify({"ok": False, "error": "Не указан месяц или сотрудник"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    if session.get('role') != 'admin':
        emp_id = session.get('employee_id')
        cursor.execute("SELECT name FROM meta_employees WHERE id = ? AND is_active = 1", (emp_id,))
        employee_row = cursor.fetchone()
        if not employee_row:
            conn.close()
            return jsonify({"ok": False, "error": "Доступ запрещен"}), 403
        empName = employee_row['name']

    try:
        year, month = map(int, selected_month.split('-'))
        start_date = datetime(year, month, 1).strftime('%Y-%m-%d')
        if month == 12:
            end_date = datetime(year + 1, 1, 1).strftime('%Y-%m-%d')
        else:
            end_date = datetime(year, month + 1, 1).strftime('%Y-%m-%d')
        
        ob_export_enabled = ob_schema_ready(cursor)
        if ob_export_enabled:
            cursor.execute("""
                SELECT date, company, object_name, hours, rate, transport, comment, submission_type,
                       sync_id, start_time, end_time, ob_base_rate
                FROM incoming_submissions
                WHERE employee = ? AND date >= ? AND date < ? AND is_deleted = 0
                ORDER BY date ASC
            """, (empName, start_date, end_date))
        else:
            cursor.execute("""
                SELECT date, company, object_name, hours, rate, transport, comment, submission_type
                FROM incoming_submissions
                WHERE employee = ? AND date >= ? AND date < ? AND is_deleted = 0
                ORDER BY date ASC
            """, (empName, start_date, end_date))
        rows = cursor.fetchall()
    except Exception as e:
        conn.close()
        return jsonify({"ok": False, "error": str(e)}), 400
    
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow([
        'Дата', 'Сотрудник', 'Фирма', 'Объект', 'Тип записи', 'Часы (вся запись; OB — полное отработанное время)', 'Ставка',
        'Транспорт', 'Комментарий', 'Время начала', 'Время окончания',
        'OB сегменты (hours — часы интервала; premium_percent — надбавка %)', 'OB надбавка (сумма)'
    ])
    
    for r in rows:
        ob_values = ["", "", "", ""]
        if ob_export_enabled and (r['submission_type'] or 'work') == 'ob':
            ob_segments = get_ob_segments(cursor, r['sync_id'])
            public_segments = [
                {key: segment[key] for key in (
                    "segment_date", "start_time", "end_time", "hours", "category",
                    "premium_percent", "multiplier", "amount",
                )}
                for segment in ob_segments
            ]
            ob_values = [
                r['start_time'] or '', r['end_time'] or '',
                json.dumps(public_segments, ensure_ascii=False),
                round(sum(segment['amount'] for segment in ob_segments), 2),
            ]
        writer.writerow([
            r['date'], 
            empName, 
            r['company'], 
            r['object_name'], 
            r['submission_type'] or 'work',
            r['hours'], 
            r['rate'], 
            r['transport'], 
            r['comment'] or ''
        ] + ob_values)

    conn.close()
    csv_data = output.getvalue().encode('utf-8-sig')
    response = make_response(csv_data)
    response.headers["Content-Disposition"] = f"attachment; filename=report_{empName}_{selected_month}.csv"
    response.headers["Content-type"] = "text/csv; charset=utf-8-sig"
    return response

def apply_object_sync_event(cursor, event):
    if not isinstance(event, dict):
        raise ValueError("Некорректное событие синхронизации объекта.")
    operation_id = str(event.get("operation_id") or "").strip()
    sync_id = str(event.get("sync_id") or "").strip()
    operation = str(event.get("operation") or "").strip()
    object_data = event.get("object")
    if not operation_id or len(operation_id) > 100 or not sync_id or len(sync_id) > 100:
        raise ValueError("В событии объекта отсутствует устойчивый ID.")
    if operation not in {"upsert", "deactivate", "activate", "link_legacy"}:
        raise ValueError("Неизвестная операция объекта.")
    if not isinstance(object_data, dict):
        raise ValueError("В событии объекта отсутствуют данные.")

    payload_hash = hashlib.sha256(json.dumps(
        event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    cursor.execute(
        "SELECT payload_hash, sync_id, revision, operation, server_object_id FROM object_sync_events WHERE operation_id = ?",
        (operation_id,),
    )
    previous = cursor.fetchone()
    if previous:
        if previous["payload_hash"] != payload_hash or previous["sync_id"] != sync_id or previous["operation"] != operation:
            raise ValueError("operation_id объекта повторно использован с другими данными.")
        cursor.execute(
            "SELECT name, markning, company, rate, transport_rate, is_active, sync_version FROM meta_objects WHERE id = ?",
            (previous["server_object_id"],),
        )
        object_state = cursor.fetchone()
        cursor.execute("SELECT MAX(revision) FROM object_sync_events WHERE sync_id = ?", (sync_id,))
        latest_revision = int(cursor.fetchone()[0] or 0)
        if object_state is not None:
            latest_revision = max(latest_revision, int(object_state["sync_version"] or 0))
        stale = latest_revision > int(previous["revision"])
        return {
            "operation_id": operation_id,
            "sync_id": sync_id,
            "operation": operation,
            "server_object_id": previous["server_object_id"],
            "object": dict(object_state) if object_state else None,
            "stale": stale,
            "equivalent": not stale,
            "revision": latest_revision,
        }

    object_name = str(object_data.get("name") or "").strip()
    company = " ".join(str(object_data.get("company") or "").split())
    markning = str(object_data.get("markning") or "").strip()
    if not object_name or len(object_name) > 200 or not company or len(company) > 150 or len(markning) > 50:
        raise ValueError("Некорректное имя объекта, фирмы или markning.")
    try:
        revision = int(event.get("revision"))
    except (TypeError, ValueError):
        raise ValueError("В событии объекта отсутствует revision.")
    if revision < 1:
        raise ValueError("Некорректная revision события объекта.")
    try:
        rate = float(object_data.get("rate", 0.0))
        transport_rate = float(object_data.get("transport_rate", 0.0))
        if not (0 <= rate <= 100000 and 0 <= transport_rate <= 100000) or not math.isfinite(rate) or not math.isfinite(transport_rate):
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("Некорректная ставка или транспортная ставка объекта.")

    state_payload = {
        "sync_id": sync_id,
        "revision": revision,
        "operation": operation,
        "object": {
            "name": object_name, "company": company, "markning": markning,
            "rate": rate, "transport_rate": transport_rate,
        },
    }
    if operation == "link_legacy":
        state_payload["object"].update({
            key: object_data.get(key) for key in (
                "server_object_id", "expected_server_name",
                "expected_server_company", "expected_server_markning",
                "expected_server_rate", "expected_server_transport_rate",
                "expected_server_is_active", "expected_server_revision",
            )
        })
    state_hash = hashlib.sha256(json.dumps(
        state_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()

    cursor.execute("SELECT id FROM meta_companies WHERE name = ? COLLATE NOCASE", (company,))
    if cursor.fetchone() is None:
        raise ValueError("Фирма объекта отсутствует в серверном справочнике.")

    cursor.execute("SELECT MAX(revision) FROM object_sync_events WHERE sync_id = ?", (sync_id,))
    latest_event_revision = int(cursor.fetchone()[0] or 0)
    cursor.execute("SELECT id, company, sync_version FROM meta_objects WHERE sync_id = ?", (sync_id,))
    current_object = cursor.fetchone()
    if current_object is not None and current_object["company"] != company:
        raise ValueError("Устойчивый ID принадлежит другой фирме.")
    current_revision = max(
        latest_event_revision,
        int(current_object["sync_version"] or 0) if current_object is not None else 0,
    )
    if revision < current_revision:
        if operation == "link_legacy" and current_object is None:
            raise ValueError("Запоздавшую legacy-связь нельзя подтвердить: серверный ID не был связан.")
        server_object_id = int(current_object["id"]) if current_object is not None else None
        cursor.execute("""
            INSERT INTO object_sync_events (
                operation_id, payload_hash, sync_id, revision, state_hash, operation, server_object_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            operation_id, payload_hash, sync_id, revision, state_hash, operation, server_object_id,
            datetime.now(timezone.utc).isoformat(),
        ))
        cursor.execute(
            "SELECT name, markning, company, rate, transport_rate, is_active, sync_version FROM meta_objects WHERE id = ?",
            (server_object_id,),
        )
        object_state = cursor.fetchone()
        return {
            "operation_id": operation_id,
            "sync_id": sync_id,
            "operation": operation,
            "server_object_id": server_object_id,
            "stale": True,
            "revision": current_revision,
            "object": dict(object_state) if object_state else None,
        }
    if revision == current_revision:
        cursor.execute(
            "SELECT state_hash FROM object_sync_events WHERE sync_id = ? AND revision = ? ORDER BY rowid DESC LIMIT 1",
            (sync_id, revision),
        )
        previous_state = cursor.fetchone()
        if previous_state is None or previous_state["state_hash"] != state_hash:
            raise ValueError("Конфликт объекта: revision уже занята другим содержимым.")
        server_object_id = int(current_object["id"]) if current_object is not None else None
        cursor.execute("""
            INSERT INTO object_sync_events (
                operation_id, payload_hash, sync_id, revision, state_hash, operation, server_object_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            operation_id, payload_hash, sync_id, revision, state_hash, operation, server_object_id,
            datetime.now(timezone.utc).isoformat(),
        ))
        cursor.execute(
            "SELECT name, markning, company, rate, transport_rate, is_active, sync_version FROM meta_objects WHERE id = ?",
            (server_object_id,),
        )
        object_state = cursor.fetchone()
        return {
            "operation_id": operation_id,
            "sync_id": sync_id,
            "operation": operation,
            "server_object_id": server_object_id,
            "stale": True,
            "equivalent": True,
            "revision": current_revision,
            "object": dict(object_state) if object_state else None,
        }

    server_object_id = None
    if operation == "link_legacy":
        try:
            server_object_id = int(object_data.get("server_object_id"))
            expected_server_rate = float(object_data.get("expected_server_rate"))
            expected_server_transport_rate = float(object_data.get("expected_server_transport_rate"))
            expected_server_is_active = int(object_data.get("expected_server_is_active"))
            expected_server_revision = int(object_data.get("expected_server_revision"))
        except (TypeError, ValueError) as error:
            raise ValueError("Для подтверждения связи нужны ID и состояние серверного объекта.") from error
        if (expected_server_is_active not in (0, 1) or expected_server_revision < 0
                or not math.isfinite(expected_server_rate) or not math.isfinite(expected_server_transport_rate)):
            raise ValueError("Некорректное ожидаемое состояние серверного объекта.")
        cursor.execute(
            "SELECT id, name, company, markning, sync_id, rate, transport_rate, is_active, sync_version "
            "FROM meta_objects WHERE id = ?",
            (server_object_id,),
        )
        target = cursor.fetchone()
        if target is None:
            raise ValueError("Подтверждаемый серверный объект не найден.")
        cursor.execute(
            "SELECT COUNT(*) FROM meta_objects WHERE sync_id IS NULL AND name = ? AND company = ? AND COALESCE(markning, '') = ?",
            (object_name, company, markning),
        )
        if cursor.fetchone()[0] != 1:
            raise ValueError("Legacy-связь неоднозначна или кандидат уже связан; изменений не внесено.")
        expected_server = (
            str(object_data.get("expected_server_name") or "").strip(),
            " ".join(str(object_data.get("expected_server_company") or "").split()),
            str(object_data.get("expected_server_markning") or "").strip(),
            expected_server_rate, expected_server_transport_rate,
            expected_server_is_active, expected_server_revision,
        )
        actual_server = (
            target["name"], target["company"], target["markning"] or "",
            float(target["rate"] or 0.0), float(target["transport_rate"] or 0.0),
            int(bool(target["is_active"])), int(target["sync_version"] or 0),
        )
        if actual_server != expected_server or target["company"] != company:
            raise ValueError("Данные серверного объекта изменились; связь не применена.")
        if revision != expected_server_revision + 1:
            raise ValueError("Revision legacy-связи не следует за серверной revision.")
        if target["sync_id"] not in (None, sync_id):
            raise ValueError("Серверный объект уже связан с другим устойчивым ID.")
        cursor.execute("SELECT id FROM meta_objects WHERE sync_id = ? AND id <> ?", (sync_id, server_object_id))
        if cursor.fetchone():
            raise ValueError("Устойчивый ID уже связан с другим серверным объектом.")
        cursor.execute(
            "UPDATE meta_objects SET sync_id = ?, rate = ?, transport_rate = ?, sync_version = ? "
            "WHERE id = ? AND sync_id IS NULL AND sync_version = ?",
            (sync_id, rate, transport_rate, revision, server_object_id, expected_server_revision),
        )
        if cursor.rowcount != 1:
            raise ValueError("Серверный объект изменился во время подтверждения связи.")
    else:
        cursor.execute("SELECT id, company, is_active FROM meta_objects WHERE sync_id = ?", (sync_id,))
        target = cursor.fetchone()
        if target is not None:
            server_object_id = int(target["id"])
            if target["company"] != company:
                raise ValueError("Устойчивый ID принадлежит другой фирме.")
            if operation == "upsert":
                if not target["is_active"]:
                    raise ValueError("Неактивный объект нельзя восстановить обычным upsert; нужна явная операция activate.")
                cursor.execute(
                    "UPDATE meta_objects SET name = ?, markning = ?, rate = ?, transport_rate = ?, sync_version = ? WHERE id = ?",
                    (object_name, markning, rate, transport_rate, revision, server_object_id),
                )
            elif operation == "deactivate":
                cursor.execute("UPDATE meta_objects SET is_active = 0, sync_version = ? WHERE id = ?", (revision, server_object_id))
            else:
                cursor.execute("""
                    UPDATE meta_objects
                    SET name = ?, markning = ?, rate = ?, transport_rate = ?, is_active = 1, sync_version = ?
                    WHERE id = ?
                """, (object_name, markning, rate, transport_rate, revision, server_object_id))
        elif operation == "deactivate":
            server_object_id = None
        elif operation == "activate":
            raise ValueError("Не найден объект для явного восстановления.")
        else:
            cursor.execute(
                "SELECT id FROM meta_objects WHERE name = ? AND company = ?",
                (object_name, company),
            )
            if cursor.fetchone():
                raise ValueError(
                    f"Объект «{object_name}» в фирме «{company}» не сопоставлен с устойчивым ID; "
                    "сначала подтвердите связь с серверной записью."
                )
            cursor.execute("""
                INSERT INTO meta_objects (sync_id, name, markning, company, rate, transport_rate, is_active, sync_version)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """, (sync_id, object_name, markning, company, rate, transport_rate, revision))
            server_object_id = int(cursor.lastrowid)

    cursor.execute("""
        INSERT INTO object_sync_events (
            operation_id, payload_hash, sync_id, revision, state_hash, operation, server_object_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        operation_id, payload_hash, sync_id, revision, state_hash, operation, server_object_id,
        datetime.now(timezone.utc).isoformat(),
    ))
    cursor.execute(
        "SELECT name, markning, company, rate, transport_rate, is_active, sync_version FROM meta_objects WHERE id = ?",
        (server_object_id,),
    )
    object_state = cursor.fetchone()
    return {
        "operation_id": operation_id,
        "sync_id": sync_id,
        "operation": operation,
        "server_object_id": server_object_id,
        "object": dict(object_state) if object_state else None,
        "stale": False,
        "revision": revision,
    }


def apply_employee_sync_snapshot(cursor, employee_name, salary_rate, is_active):
    profiles = find_employee_profiles(cursor, employee_name)
    if profiles:
        profile_ids = [profile["id"] for profile in profiles]
        placeholders = ", ".join("?" for _ in profile_ids)
        if salary_rate is None:
            if not is_active:
                cursor.execute(
                    f"UPDATE meta_employees SET is_active = 0 WHERE id IN ({placeholders})",
                    profile_ids,
                )
        elif is_active:
            cursor.execute(
                f"UPDATE meta_employees SET salary_rate = ? WHERE id IN ({placeholders}) AND is_active = 1",
                (salary_rate, *profile_ids),
            )
        else:
            cursor.execute(
                f"UPDATE meta_employees SET salary_rate = ?, is_active = 0 WHERE id IN ({placeholders})",
                (salary_rate, *profile_ids),
            )
    else:
        cursor.execute(
            "INSERT INTO meta_employees (name, salary_rate, is_active) VALUES (?, ?, ?)",
            (employee_name, salary_rate if salary_rate is not None else 0.0, is_active),
        )
    return find_employee_profiles(cursor, employee_name)


def get_employee_sync_state(cursor, profile_id):
    if profile_id is None:
        return None
    cursor.execute(
        "SELECT id, name, salary_rate, is_active FROM meta_employees WHERE id = ?",
        (profile_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "salary_rate": float(row["salary_rate"] or 0.0),
        "is_active": bool(row["is_active"]),
    }


def apply_employee_sync_event(cursor, event, operation):
    if not isinstance(event, dict):
        raise ValueError("Некорректное событие сотрудника.")
    operation_id = str(event.get("operation_id") or "").strip()
    employee_name = " ".join(str(event.get("name") or "").split())
    employee_key = str(event.get("employee_key") or normalize_employee_name(employee_name)).strip()
    try:
        revision = int(event.get("revision", 1))
    except (TypeError, ValueError):
        raise ValueError("Некорректная revision события сотрудника.")
    if not operation_id or len(operation_id) > 100 or not employee_name or len(employee_name) > 150:
        raise ValueError("Некорректные данные события сотрудника.")
    if not employee_key or employee_key != normalize_employee_name(employee_name) or revision < 1:
        raise ValueError("Некорректный ключ или revision события сотрудника.")
    salary_rate = event.get("salary_rate")
    if operation == "activate" and salary_rate is not None:
        try:
            salary_rate = round(float(salary_rate), 2)
            if not 0 <= salary_rate <= 100000 or not math.isfinite(salary_rate):
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError("Некорректная ставка восстанавливаемого сотрудника.")

    payload_hash = hashlib.sha256(json.dumps(
        {"operation_id": operation_id, "employee_key": employee_key,
         "revision": revision, "operation": operation, "name": employee_name,
         "salary_rate": salary_rate},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    state_hash = hashlib.sha256(json.dumps(
        {"employee_key": employee_key, "revision": revision, "operation": operation,
         "salary_rate": salary_rate if operation == "activate" else None},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    cursor.execute(
        "SELECT payload_hash, employee_key, revision, operation, profile_id FROM employee_sync_events WHERE operation_id = ?",
        (operation_id,),
    )
    previous = cursor.fetchone()
    if previous:
        if (previous["payload_hash"] != payload_hash or previous["employee_key"] != employee_key
                or previous["revision"] != revision or previous["operation"] != operation):
            raise ValueError("operation_id сотрудника повторно использован с другими данными.")
        cursor.execute("SELECT MAX(revision) FROM employee_sync_events WHERE employee_key = ?", (employee_key,))
        latest_revision = int(cursor.fetchone()[0] or 0)
        return {
            "operation_id": operation_id,
            "profile_id": previous["profile_id"],
            "stale": latest_revision > revision,
            "equivalent": latest_revision == revision,
            "revision": latest_revision,
            "employee": get_employee_sync_state(cursor, previous["profile_id"]),
        }

    profiles = find_employee_profiles(cursor, employee_name)
    if len(profiles) > 1:
        raise ValueError(f"Удаление/восстановление «{employee_name}» неоднозначно: найдено несколько профилей.")
    profile_id = int(profiles[0]["id"]) if profiles else None
    cursor.execute("SELECT MAX(revision) FROM employee_sync_events WHERE employee_key = ?", (employee_key,))
    latest_revision = int(cursor.fetchone()[0] or 0)
    if revision < latest_revision:
        cursor.execute("""
            INSERT INTO employee_sync_events (
                operation_id, payload_hash, employee_key, revision, state_hash, operation, profile_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            operation_id, payload_hash, employee_key, revision, state_hash, operation, profile_id,
            datetime.now(timezone.utc).isoformat(),
        ))
        return {
            "operation_id": operation_id,
            "profile_id": profile_id,
            "stale": True,
            "revision": latest_revision,
            "employee": get_employee_sync_state(cursor, profile_id),
        }
    if revision == latest_revision:
        cursor.execute(
            "SELECT operation, state_hash FROM employee_sync_events WHERE employee_key = ? AND revision = ? ORDER BY rowid DESC LIMIT 1",
            (employee_key, revision),
        )
        previous_revision = cursor.fetchone()
        if (previous_revision is None or previous_revision["operation"] != operation
                or previous_revision["state_hash"] != state_hash):
            raise ValueError("Конфликт сотрудника: revision уже занята другой операцией.")
        cursor.execute("""
            INSERT INTO employee_sync_events (
                operation_id, payload_hash, employee_key, revision, state_hash, operation, profile_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            operation_id, payload_hash, employee_key, revision, state_hash, operation, profile_id,
            datetime.now(timezone.utc).isoformat(),
        ))
        return {
            "operation_id": operation_id,
            "profile_id": profile_id,
            "stale": True,
            "equivalent": True,
            "revision": latest_revision,
            "employee": get_employee_sync_state(cursor, profile_id),
        }

    if operation == "deactivate":
        if profile_id is not None:
            cursor.execute("UPDATE meta_employees SET is_active = 0 WHERE id = ?", (profile_id,))
            cursor.execute(
                "UPDATE users SET is_active = 0 WHERE role <> 'admin' AND "
                "(username = ? COLLATE NOCASE OR employee_id = ?)",
                (employee_name, profile_id),
            )
        else:
            cursor.execute(
                "UPDATE users SET is_active = 0 WHERE role <> 'admin' AND username = ? COLLATE NOCASE",
                (employee_name,),
            )
    elif operation == "activate":
        if profile_id is None:
            raise ValueError("Не найден профиль сотрудника для явного восстановления.")
        if salary_rate is None:
            cursor.execute("UPDATE meta_employees SET is_active = 1 WHERE id = ?", (profile_id,))
        else:
            cursor.execute(
                "UPDATE meta_employees SET salary_rate = ?, is_active = 1 WHERE id = ?",
                (salary_rate, profile_id),
            )
        cursor.execute(
            "UPDATE users SET is_active = 1 WHERE role <> 'admin' AND "
            "(username = ? COLLATE NOCASE OR employee_id = ?)",
            (employee_name, profile_id),
        )
    else:
        raise ValueError("Неизвестная операция сотрудника.")

    cursor.execute("""
        INSERT INTO employee_sync_events (
            operation_id, payload_hash, employee_key, revision, state_hash, operation, profile_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        operation_id, payload_hash, employee_key, revision, state_hash, operation, profile_id,
        datetime.now(timezone.utc).isoformat(),
    ))
    return {
        "operation_id": operation_id,
        "profile_id": profile_id,
        "stale": False,
        "revision": revision,
        "employee": get_employee_sync_state(cursor, profile_id),
    }


@app.route('/sync-desktop-data', methods=['POST'])
def sync_desktop_data():
    if not verify_sync_key():
        return jsonify({"ok": False, "error": "Доступ запрещен. Требуется валидный ключ синхронизации."}), 403

    data = request.json or {}
    employees = data.get("employees", [])
    deleted_employees = data.get("deleted_employees", [])
    restored_employees = data.get("restored_employees", [])
    companies = data.get("companies", [])
    objects = data.get("objects", [])
    object_events = data.get("object_events", [])
    desktop_shifts = data.get("shifts", [])

    if not isinstance(employees, list) or not isinstance(deleted_employees, list) or not isinstance(restored_employees, list) or not isinstance(companies, list) or \
       not isinstance(objects, list) or not isinstance(object_events, list) or not isinstance(desktop_shifts, list):
        return jsonify({"ok": False, "error": "Неверный формат данных: ожидаются списки"}), 400

    if len(employees) > 500 or len(deleted_employees) > 500 or len(restored_employees) > 500 or len(companies) > 500 or len(objects) > 2000 or len(object_events) > 2000 or len(desktop_shifts) > 5000:
        return jsonify({"ok": False, "error": "Превышен лимит записей в пакете синхронизации"}), 400

    normalized_deletions = []
    deletion_operation_ids = set()
    for deletion in deleted_employees:
        if not isinstance(deletion, dict):
            return jsonify({"ok": False, "error": "Некорректная запись удаления сотрудника"}), 400
        operation_id = str(deletion.get("operation_id") or "").strip()
        employee_name = " ".join(str(deletion.get("name") or "").split())
        if not operation_id or len(operation_id) > 100 or not employee_name or len(employee_name) > 150:
            return jsonify({"ok": False, "error": "Некорректные данные удаления сотрудника"}), 400
        if operation_id in deletion_operation_ids:
            return jsonify({"ok": False, "error": "Повторяется operation_id удаления сотрудника"}), 400
        deletion_operation_ids.add(operation_id)
        try:
            revision = max(1, int(deletion.get("revision", 1)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Некорректная revision удаления сотрудника"}), 400
        employee_key = str(deletion.get("employee_key") or normalize_employee_name(employee_name)).strip()
        if employee_key != normalize_employee_name(employee_name):
            return jsonify({"ok": False, "error": "Ключ удаления сотрудника не совпадает с именем"}), 400
        normalized_deletions.append({
            "operation_id": operation_id, "name": employee_name,
            "employee_key": employee_key, "revision": revision,
        })

    normalized_restorations = []
    restoration_operation_ids = set()
    for restoration in restored_employees:
        if not isinstance(restoration, dict):
            return jsonify({"ok": False, "error": "Некорректная запись восстановления сотрудника"}), 400
        operation_id = str(restoration.get("operation_id") or "").strip()
        employee_name = " ".join(str(restoration.get("name") or "").split())
        employee_key = str(restoration.get("employee_key") or normalize_employee_name(employee_name)).strip()
        try:
            revision = int(restoration.get("revision"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Некорректная revision восстановления сотрудника"}), 400
        salary_rate = restoration.get("salary_rate")
        if salary_rate is not None:
            try:
                salary_rate = round(float(salary_rate), 2)
                if not 0 <= salary_rate <= 100000 or not math.isfinite(salary_rate):
                    raise ValueError()
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "Некорректная ставка восстановления сотрудника"}), 400
        if (not operation_id or len(operation_id) > 100 or not employee_name or len(employee_name) > 150
                or employee_key != normalize_employee_name(employee_name) or revision < 1):
            return jsonify({"ok": False, "error": "Некорректные данные восстановления сотрудника"}), 400
        if operation_id in restoration_operation_ids:
            return jsonify({"ok": False, "error": "Повторяется operation_id восстановления сотрудника"}), 400
        restoration_operation_ids.add(operation_id)
        normalized_restorations.append({
            "operation_id": operation_id, "name": employee_name,
            "employee_key": employee_key, "revision": revision,
            "salary_rate": salary_rate,
        })

    conn = get_db_connection()
    cursor = conn.cursor()
    employee_sync_results = []
    deleted_employee_operations = []
    restored_employee_operations = []
    employee_event_results = []
    object_sync_results = []

    try:
        if employees:
            for emp in employees:
                if isinstance(emp, dict):
                    emp_name = " ".join(str(emp.get('name', '')).split())
                    if not emp_name or len(emp_name) > 150:
                        raise ValueError("Недопустимое имя сотрудника")
                    try:
                        emp_rate = round(float(emp.get('salary_rate', 0.0)), 2)
                        if not (0 <= emp_rate <= 100000) or str(emp_rate).lower() in ['nan', 'inf', '-inf']:
                            raise ValueError()
                    except (TypeError, ValueError):
                        return jsonify({"ok": False, "error": "Некорректная ставка сотрудника"}), 400
                    emp_active = int(emp.get('is_active', 1))
                    if emp_active not in (0, 1):
                        raise ValueError("Недопустимый статус сотрудника")
                else:
                    emp_name = " ".join(str(emp).split()) if emp else ""
                    if not emp_name or len(emp_name) > 150:
                        raise ValueError("Недопустимое имя сотрудника")
                    emp_rate = None
                    emp_active = 1

                stored_profiles = apply_employee_sync_snapshot(cursor, emp_name, emp_rate, emp_active)
                employee_sync_results.append({
                    "employee_name": emp_name,
                    "profiles": [{
                        "id": profile["id"],
                        "name": profile["name"],
                        "salary_rate": float(profile["salary_rate"] or 0.0),
                        "is_active": bool(profile["is_active"]),
                    } for profile in stored_profiles],
                })

        if companies:
            for comp in companies:
                comp_name = str(comp.get('name', '') if isinstance(comp, dict) else comp).strip()
                if comp_name:
                    if len(comp_name) > 150:
                        return jsonify({"ok": False, "error": "Слишком длинное имя компании"}), 400
                    cursor.execute("INSERT OR IGNORE INTO meta_companies (name) VALUES (?)", (comp_name,))

        for event in object_events:
            object_sync_results.append(apply_object_sync_event(cursor, event))

        for restoration in normalized_restorations:
            result = apply_employee_sync_event(cursor, restoration, "activate")
            employee_event_results.append(result)
            restored_employee_operations.append(restoration["operation_id"])

        for deletion in normalized_deletions:
            result = apply_employee_sync_event(cursor, deletion, "deactivate")
            employee_event_results.append(result)
            deleted_employee_operations.append(deletion["operation_id"])

        conn.commit()
    except Exception as e:
        logger.error(f"Safe sync validation error: {e}")
        conn.rollback()
        return jsonify({"ok": False, "error": f"Ошибка валидации и синхронизации: {str(e)}"}), 400
    finally:
        conn.close()

    return jsonify({
        "ok": True,
        "status": "metadata_synced",
        "shift_sync": "versioned_api_required",
        "employee_sync": employee_sync_results,
        "deleted_employee_operations": deleted_employee_operations,
        "restored_employee_operations": restored_employee_operations,
        "employee_event_results": employee_event_results,
        "object_sync_status": "legacy_snapshot_ignored" if objects and not object_events else "event_sync",
        "object_sync_operations": object_sync_results,
    })

def serialize_submission(row, cursor=None):
    sub_type = row['submission_type'] or "work"
    payload = {
        "id": f"submission:{row['id']}",
        "request_id": row['request_id'],
        "sync_id": row['sync_id'],
        "version": row['version'],
        "change_seq": row['change_seq'],
        "last_operation_id": row["last_operation_id"],
        "deleted": bool(row['is_deleted']),
        "source": row['source'],
        "date": row['date'],
        "employee": row['employee'],
        "company": row['company'],
        "object_name": row['object_name'],
        "hours": row['hours'],
        "rate": row['rate'],
        "transport": row['transport'],
        "comment": row['comment'] or "",
        "submission_type": sub_type,
        "record_type": sub_type,
        "invoiced": row['invoiced'],
        "customer_pricing": row["customer_pricing"] if "customer_pricing" in row.keys() else None,
    }
    if sub_type == "ob":
        start_time = row["start_time"] if "start_time" in row.keys() else None
        end_time = row["end_time"] if "end_time" in row.keys() else None
        ob_base_rate = row["ob_base_rate"] if "ob_base_rate" in row.keys() else None
        segments = []
        if cursor and ob_schema_ready(cursor):
            segments = get_ob_segments(cursor, row["sync_id"])
        payload.update({
            "start_time": start_time,
            "end_time": end_time,
            "ob_base_rate": ob_base_rate,
            "ob_segments": segments,
        })
    return payload

@app.route('/sync/pull', methods=['GET'])
def sync_pull():
    if not verify_sync_key():
        return jsonify({"ok": False, "error": "Доступ запрещен"}), 403

    try:
        since = max(0, int(request.args.get('since', '0')))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Некорректный курсор синхронизации"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("BEGIN")
    cursor.execute("SELECT * FROM incoming_submissions WHERE change_seq > ? ORDER BY change_seq, id", (since,))
    rows = cursor.fetchall()
    cursor.execute("SELECT value FROM sync_metadata WHERE name = 'change_sequence'")
    current_sequence = cursor.fetchone()[0]
    changes = [serialize_submission(row, cursor=cursor) for row in rows]
    conn.commit()
    conn.close()
    return jsonify({
        "ok": True,
        "changes": changes,
        "cursor": current_sequence
    })

@app.route('/sync/capabilities', methods=['GET'])
def sync_capabilities():
    if not verify_sync_key():
        return jsonify({"ok": False, "error": "Доступ запрещен"}), 403
    conn = get_db_connection()
    cursor = conn.cursor()
    supports_ob = ob_schema_ready(cursor)
    conn.close()
    submission_types = set(SUPPORTED_SUBMISSION_TYPES)
    if not supports_ob:
        submission_types.discard("ob")
    return jsonify({
        "ok": True,
        "submission_types": sorted(submission_types),
        "typed_tombstones": True,
        "customer_pricing": 1,
        "fixed_pricing": 1,
        "payroll_history": 1,
    })

@app.route('/sync/push', methods=['POST'])
def sync_push():
    if not verify_sync_key():
        return jsonify({"ok": False, "error": "Доступ запрещен"}), 403

    data = request.json or {}
    changes = data.get('changes', [])
    if not isinstance(changes, list) or len(changes) > 500:
        return jsonify({"ok": False, "error": "Ожидается список изменений (не более 500)"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    results = []
    touched_fixed = set()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        for change in changes:
            if not isinstance(change, dict):
                raise ValueError("Некорректный элемент синхронизации")
            sync_id = str(change.get('sync_id', '')).strip()
            operation_id = str(change.get('operation_id', '')).strip()
            operation = change.get('operation')
            if not sync_id or len(sync_id) > 120 or not operation_id or len(operation_id) > 120:
                raise ValueError("Отсутствует стабильный ID или operation_id")
            if operation not in ('upsert', 'delete'):
                raise ValueError("Неизвестный тип изменения")
            try:
                base_version = int(change.get('base_version', 0))
            except (TypeError, ValueError):
                raise ValueError("Некорректная версия изменения")
            if base_version < 0:
                raise ValueError("Некорректная версия изменения")

            payload = change.get('shift') or {}
            if not isinstance(payload, dict):
                raise ValueError("Некорректные данные смены.")
            cursor.execute("SELECT * FROM incoming_submissions WHERE sync_id = ? OR request_id = ?", (sync_id, sync_id))
            existing = cursor.fetchone()
            if existing and existing["customer_pricing"]:
                old_fixed = json.loads(existing["customer_pricing"]).get("fixed")
                if old_fixed:
                    touched_fixed.add(old_fixed["id"])
            ob_result = None

            if operation == 'upsert':
                date_val = str(payload.get('date', '')).strip()
                datetime.strptime(date_val, '%Y-%m-%d')
                employee = str(payload.get('employee', '')).strip()
                company = str(payload.get('company', '')).strip()
                object_name = str(payload.get('object_name', '')).strip()
                if not employee or len(employee) > 150 or not company or not object_name or len(object_name) > 200:
                    raise ValueError("Некорректные поля смены")
                hours = float(payload.get('hours'))
                rate = round(float(payload.get('rate', 0)), 2)
                transport = round(float(payload.get('transport', 0)), 2)
                if not (0.5 <= hours <= 24) or hours % 0.5 != 0 or not (0 <= rate <= 100000) or not (0 <= transport <= 10000):
                    raise ValueError("Часы, ставка или транспорт вне допустимого диапазона")
                if any(str(value).lower() in ('nan', 'inf', '-inf') for value in (hours, rate, transport)):
                    raise ValueError("Числовое поле должно быть конечным")
                comment = str(payload.get('comment', ''))[:2000]
                has_type = 'submission_type' in payload or 'record_type' in payload
                if has_type:
                    submission_type = resolve_submission_type(payload)
                elif existing and not existing['is_deleted']:
                    submission_type = existing['submission_type'] or infer_submission_type_from_object(cursor, company, object_name)
                else:
                    submission_type = infer_submission_type_from_object(cursor, company, object_name)
                preserve_history_object = bool(
                    existing and not existing['is_deleted']
                    and existing['company'] == company
                    and existing['object_name'] == object_name
                    and existing['submission_type'] == submission_type
                )
                validate_submission_object(
                    cursor, submission_type, company, object_name,
                    allow_inactive=preserve_history_object,
                )
                invoiced = int(bool(payload.get('invoiced', 0))) if submission_type in ('work', 'ob') else 0
                if submission_type == "ob":
                    if not ob_schema_ready(cursor):
                        raise ValueError("OB-схема не подготовлена; выполните миграцию с backup")
                    ob_result = calculate_ob_payload(
                        cursor, date_val, payload.get("start_time"), payload.get("end_time"), employee,
                        existing=existing,
                        company=company, object_name=object_name,
                    )
                    if not ob_result["base_rate_configured"]:
                        raise ValueError("В профиле сотрудника не настроена базовая ставка.")
                    date_val = ob_result["date"]
                    hours = ob_result["hours"]
                    rate = 0.0
                    transport = submission_transport(transport, submission_type)
                elif submission_type == "holiday":
                    rate = 0.0
                    transport = 0.0
                elif submission_type != 'work':
                    rate = float(existing['rate']) if existing else 0.0
                    transport = float(existing['transport']) if existing else 0.0
                normalized = {
                    "date": date_val, "employee": employee, "company": company,
                    "object_name": object_name, "hours": hours, "rate": rate,
                    "transport": transport, "comment": comment, "invoiced": invoiced,
                    "submission_type": submission_type
                }
                if ob_result:
                    normalized.update({
                        "start_time": ob_result["start_time"],
                        "end_time": ob_result["end_time"],
                        "ob_base_rate": ob_result["ob_base_rate"],
                    })
                pricing_value = payload.get("customer_pricing", existing["customer_pricing"] if existing else None)
                if "customer_pricing" in payload or (existing and pricing_value):
                    normalized["customer_pricing"] = pricing_json(pricing_value, dict(normalized, sync_id=sync_id))
                    parsed_quote = json.loads(normalized["customer_pricing"]) if normalized["customer_pricing"] else {}
                    if parsed_quote.get("fixed"):
                        touched_fixed.add(parsed_quote["fixed"]["id"])
                    if existing and existing["invoiced"] and normalized["customer_pricing"] != existing["customer_pricing"]:
                        old_quote = json.loads(existing["customer_pricing"]) if existing["customer_pricing"] else {}
                        old_fixed = old_quote.pop("fixed", None)
                        removing_fixed = old_fixed and not invoiced and parsed_quote == old_quote
                        if not removing_fixed:
                            raise ValueError("Закрытая клиентская цена защищена; сначала верните запись в работу.")
            else:
                has_type = 'submission_type' in payload or 'record_type' in payload
                if has_type:
                    submission_type = resolve_submission_type(payload)
                elif existing:
                    submission_type = existing['submission_type'] or "work"
                elif all(payload.get(key) for key in ('company', 'object_name')):
                    submission_type = infer_submission_type_from_object(
                        cursor, str(payload['company']).strip(), str(payload['object_name']).strip()
                    )
                else:
                    submission_type = "work"
                if has_type:
                    tombstone_company = str(payload.get('company') or (existing['company'] if existing else '')).strip()
                    tombstone_object = str(payload.get('object_name') or (existing['object_name'] if existing else '')).strip()
                    if operation == 'upsert' and tombstone_company and tombstone_object:
                        validate_submission_object(cursor, submission_type, tombstone_company, tombstone_object)
                    elif existing and existing['submission_type'] and existing['submission_type'] != submission_type:
                        raise ValueError("Тип удаляемой записи не совпадает с серверной записью.")
                normalized = {"submission_type": submission_type}

            operation_hash = hashlib.sha256(json.dumps(
                {"operation": operation, "shift": normalized}, sort_keys=True
            ).encode('utf-8')).hexdigest()

            if existing and existing['last_operation_id'] == operation_id:
                legacy_normalized = dict(normalized)
                legacy_normalized.pop('submission_type', None)
                legacy_operation_hash = hashlib.sha256(json.dumps(
                    {"operation": operation, "shift": legacy_normalized}, sort_keys=True
                ).encode('utf-8')).hexdigest()
                if existing['payload_hash'] == operation_hash or (
                    not has_type and (existing['submission_type'] or 'work') == submission_type
                    and existing['payload_hash'] == legacy_operation_hash
                ):
                    results.append({"operation_id": operation_id, "ok": True, "version": existing['version'], "idempotent": True})
                else:
                    results.append({"operation_id": operation_id, "ok": False, "conflict": True, "reason": "operation_id_reused"})
                continue

            if existing and existing['is_deleted'] and operation != 'delete':
                results.append({"operation_id": operation_id, "ok": False, "conflict": True, "reason": "deleted"})
                continue
            if existing and (existing['version'] != base_version or existing['sync_id'] != sync_id):
                results.append({
                    "operation_id": operation_id, "ok": False, "conflict": True,
                    "reason": "stale_version", "current": serialize_submission(existing)
                })
                continue
            if not existing and base_version != 0:
                results.append({"operation_id": operation_id, "ok": False, "conflict": True, "reason": "missing_record"})
                continue

            if operation == "upsert" and existing and existing["invoiced"]:
                if any(normalized.get(key) != (existing[key] or "" if key == "comment" else existing[key]) for key in (
                    "date", "employee", "company", "object_name", "hours", "rate", "transport", "comment", "submission_type"
                )):
                    raise ValueError("Выставленная запись защищена; сначала верните её в работу.")
            if operation == "upsert" and normalized["invoiced"] and (not existing or not existing["invoiced"]):
                pricing_row = dict(normalized, entry_type=submission_type,
                                   sync_id=sync_id, customer_pricing=normalized.get("customer_pricing"))
                segments = (ob_result["segments"] if ob_result else [])
                require_final_prices(pricing_row, segments)

            now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            change_seq = next_change_sequence(cursor)
            version = existing['version'] + 1 if existing else 1
            if existing:
                if ob_schema_ready(cursor):
                    cursor.execute('''
                        UPDATE incoming_submissions SET
                            date = COALESCE(?, date), employee = COALESCE(?, employee),
                            company = COALESCE(?, company), object_name = COALESCE(?, object_name),
                            hours = COALESCE(?, hours), rate = COALESCE(?, rate),
                            transport = COALESCE(?, transport), comment = COALESCE(?, comment),
                            invoiced = COALESCE(?, invoiced),
                            submission_type = COALESCE(?, submission_type),
                            start_time = ?, end_time = ?, ob_base_rate = ?,
                            version = ?, change_seq = ?, is_deleted = ?, desktop_acked = 1,
                            last_operation_id = ?, payload_hash = ?, updated_at = ?
                        WHERE id = ?
                    ''', (
                        normalized.get('date'), normalized.get('employee'), normalized.get('company'),
                        normalized.get('object_name'), normalized.get('hours'), normalized.get('rate'),
                        normalized.get('transport'), normalized.get('comment'), normalized.get('invoiced'),
                        normalized.get('submission_type'), normalized.get('start_time'), normalized.get('end_time'),
                        normalized.get('ob_base_rate'), version, change_seq,
                        int(operation == 'delete'), operation_id, operation_hash, now, existing['id']
                    ))
                else:
                    cursor.execute('''
                        UPDATE incoming_submissions SET
                            date = COALESCE(?, date), employee = COALESCE(?, employee),
                            company = COALESCE(?, company), object_name = COALESCE(?, object_name),
                            hours = COALESCE(?, hours), rate = COALESCE(?, rate),
                            transport = COALESCE(?, transport), comment = COALESCE(?, comment),
                            invoiced = COALESCE(?, invoiced),
                            submission_type = COALESCE(?, submission_type),
                            version = ?, change_seq = ?, is_deleted = ?, desktop_acked = 1,
                            last_operation_id = ?, payload_hash = ?, updated_at = ?
                        WHERE id = ?
                    ''', (
                        normalized.get('date'), normalized.get('employee'), normalized.get('company'),
                        normalized.get('object_name'), normalized.get('hours'), normalized.get('rate'),
                        normalized.get('transport'), normalized.get('comment'), normalized.get('invoiced'),
                        normalized.get('submission_type'), version, change_seq,
                        int(operation == 'delete'), operation_id, operation_hash, now, existing['id']
                    ))
            else:
                if operation == 'delete':
                    results.append({"operation_id": operation_id, "ok": False, "conflict": True, "reason": "missing_record"})
                    continue
                if ob_schema_ready(cursor):
                    cursor.execute('''
                        INSERT INTO incoming_submissions (
                            request_id, sync_id, submission_type, date, employee, company, object_name,
                            hours, rate, transport, comment, invoiced, version, change_seq, is_deleted,
                            desktop_acked, source, last_operation_id, payload_hash, created_at, updated_at,
                            start_time, end_time, ob_base_rate
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 'desktop', ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        sync_id, sync_id, normalized['submission_type'], normalized['date'], normalized['employee'], normalized['company'],
                        normalized['object_name'], normalized['hours'], normalized['rate'], normalized['transport'],
                        normalized['comment'], normalized['invoiced'], version, change_seq, operation_id, operation_hash, now, now,
                        normalized.get('start_time'), normalized.get('end_time'), normalized.get('ob_base_rate'),
                    ))
                else:
                    cursor.execute('''
                        INSERT INTO incoming_submissions (
                            request_id, sync_id, submission_type, date, employee, company, object_name,
                            hours, rate, transport, comment, invoiced, version, change_seq, is_deleted,
                            desktop_acked, source, last_operation_id, payload_hash, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 'desktop', ?, ?, ?, ?)
                    ''', (
                        sync_id, sync_id, normalized['submission_type'], normalized['date'], normalized['employee'], normalized['company'],
                        normalized['object_name'], normalized['hours'], normalized['rate'], normalized['transport'],
                        normalized['comment'], normalized['invoiced'], version, change_seq, operation_id, operation_hash, now, now
                    ))
            if operation == "upsert" and "customer_pricing" in normalized:
                ensure_pricing_schema(cursor, "incoming_submissions")
                store_pricing(cursor, "incoming_submissions", sync_id, normalized["customer_pricing"], from_sync=True)
            if ob_schema_ready(cursor):
                if operation == "delete" and existing and existing["submission_type"] == "ob":
                    cursor.execute("DELETE FROM ob_segments WHERE shift_id = ?", (sync_id,))
                elif ob_result and not ob_result.get("preserved"):
                    replace_ob_segments(cursor, sync_id, ob_result["segments"])
            results.append({"operation_id": operation_id, "ok": True, "version": version, "change_seq": change_seq})

        validate_fixed_storage(cursor, "incoming_submissions", touched_fixed)
        conn.commit()
    except (ValueError, TypeError, sqlite3.IntegrityError) as e:
        conn.rollback()
        conn.close()
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception:
        conn.rollback()
        conn.close()
        logger.exception("Ошибка применения синхронизации смен")
        return jsonify({"ok": False, "error": "Не удалось применить изменения синхронизации"}), 500
    conn.close()
    return jsonify({"ok": True, "results": results})

@app.route('/get-unsynced', methods=['GET'])
def get_unsynced():
    if not verify_sync_key():
        return jsonify({"ok": False, "error": "Доступ запрещен"}), 403

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, request_id, date, employee, company, object_name, hours, rate, transport, comment FROM cloud_shifts WHERE synced = 0")
    legacy_rows = cursor.fetchall()
    cursor.execute("SELECT * FROM incoming_submissions WHERE source = 'web' AND desktop_acked = 0 AND is_deleted = 0 ORDER BY id")
    submission_rows = cursor.fetchall()

    shifts = []
    for row in legacy_rows:
        shifts.append({
            "id": row['id'], "request_id": row['request_id'] or f"legacy-cloud:{row['id']}",
            "sync_id": row['request_id'] or f"legacy-cloud:{row['id']}", "version": 1,
            "source": "legacy",
            "date": row['date'], "employee": row['employee'], "company": row['company'],
            "object_name": row['object_name'], "hours": row['hours'], "rate": row['rate'],
            "transport": row['transport'], "comment": row['comment'] or "",
            "submission_type": "work",
            "record_type": "work",
        })
    shifts.extend(serialize_submission(row, cursor=cursor) for row in submission_rows)
    conn.close()
    return jsonify({"ok": True, "shifts": shifts})

@app.route('/mark-synced', methods=['POST'])
def mark_synced():
    if not verify_sync_key():
        return jsonify({"ok": False, "error": "Доступ запрещен"}), 403

    data = request.json or {}
    ids = data.get("ids", [])
    if not isinstance(ids, list):
        return jsonify({"ok": False, "error": "Ожидается список идентификаторов"}), 400
    if ids:
        conn = get_db_connection()
        cursor = conn.cursor()
        for shift_id in ids:
            if isinstance(shift_id, str) and shift_id.startswith('submission:'):
                try:
                    submission_id = int(shift_id.split(':', 1)[1])
                except ValueError:
                    conn.close()
                    return jsonify({"ok": False, "error": "Некорректный ID заявки"}), 400
                cursor.execute("UPDATE incoming_submissions SET desktop_acked = 1 WHERE id = ? AND source = 'web'", (submission_id,))
            else:
                try:
                    legacy_id = int(shift_id)
                except (TypeError, ValueError):
                    conn.close()
                    return jsonify({"ok": False, "error": "Некорректный ID смены"}), 400
                cursor.execute("UPDATE cloud_shifts SET synced = 1 WHERE id = ?", (legacy_id,))
                cursor.execute("UPDATE incoming_submissions SET desktop_acked = 1 WHERE legacy_cloud_id = ?", (legacy_id,))
        conn.commit()
        conn.close()
    return jsonify({"ok": True, "status": "marked"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)