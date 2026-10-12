import sqlite3
import time
import ast
import os
import json
import hashlib
import logging
import csv
import io
import uuid
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, jsonify, session, make_response
from werkzeug.security import generate_password_hash, check_password_hash
from ob_calculator import calculate_work_interval

# Настройка системного логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

SUPPORTED_SUBMISSION_TYPES = {"work", "atf", "sick", "ob"}
SPECIAL_SUBMISSION_OBJECTS = {
    "atf": ("Arbetstidsförkortning", "p1000"),
    "sick": ("Sjuk", "p1001"),
}
OB_BASE_RATE_FALLBACKS = {
    "aliaksei patonich": 245.80,
    "andrii mudrii": 220.00,
    "andrii piskun": 245.80,
    "ivan kovpak": 220.00,
    "mykhailo berdnyk": 220.00,
    "oleg ievdokimov": 220.00,
    "oleh strun": 220.00,
    "roman bashchuk": 245.80,
    "roman palamar": 220.00,
    "taras oleksii": 220.00,
    "taras roman": 220.00,
    "vitalii stankovskyi": 220.00,
}


def resolve_submission_type(data, default="work"):
    provided = [data[key] for key in ("submission_type", "record_type") if key in data]
    if len(provided) == 2 and provided[0] != provided[1]:
        raise ValueError("Поля submission_type и record_type не совпадают.")
    submission_type = str(provided[0] if provided else default).strip()
    if submission_type not in SUPPORTED_SUBMISSION_TYPES:
        raise ValueError("Неизвестный тип записи. Допустимы work, atf, sick.")
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
    if salary_rate <= 0:
        fallback_rate = OB_BASE_RATE_FALLBACKS.get(normalize_employee_name(employee))
        if fallback_rate is not None:
            logger.warning(
                "OB salary fallback used employee_name=%r profile_id=%s database_rate=%s fallback_rate=%s",
                employee, profile["id"], salary_rate, fallback_rate,
            )
            salary_rate = fallback_rate
    logger.info(
        "OB salary lookup result employee_name=%r profile_id=%s matched_name=%r salary_rate=%s",
        employee, profile["id"], profile["name"], salary_rate,
    )
    return salary_rate


def calculate_ob_payload(cursor, work_date, start_time, end_time, employee):
    base_rate = get_employee_ob_base_rate(cursor, employee)
    result = calculate_work_interval(work_date, start_time, end_time, base_rate)
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
        "base_rate_configured": base_rate > 0,
    }

def public_ob_result(result):
    public_segments = [
        {key: segment[key] for key in (
            "segment_date", "start_time", "end_time", "hours", "category",
            "premium_percent", "multiplier", "amount",
        )}
        for segment in result["segments"]
    ]
    return {
        "date": result["date"],
        "start_time": result["start_time"],
        "end_time": result["end_time"],
        "hours": result["hours"],
        "total_hours": result["hours"],
        "total_amount": result["total_amount"],
        "segments": public_segments,
        "rules_version": result["rules_version"],
        "base_rate_configured": result["base_rate_configured"],
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


def validate_submission_object(cursor, submission_type, company, object_name):
    reference = SPECIAL_SUBMISSION_OBJECTS.get(submission_type)
    if not reference:
        cursor.execute("SELECT id, name, markning FROM meta_objects WHERE name = ? AND company = ?", (object_name, company))
    else:
        cursor.execute(
            "SELECT id, name, markning FROM meta_objects WHERE name = ? AND company = ? AND markning = ?",
            (reference[0], company, reference[1])
        )
    matches = cursor.fetchall()
    if len(matches) != 1:
        if reference and not matches:
            raise ValueError(f"Для типа {submission_type} не найдена точная запись справочника {reference[0]} / {reference[1]}.")
        if len(matches) > 1:
            raise ValueError("Запись объекта неоднозначна в справочнике.")
        raise ValueError("Объект не принадлежит выбранной фирме или не существует.")
    if reference and object_name != reference[0]:
        raise ValueError(f"Для типа {submission_type} нужно выбрать объект {reference[0]}.")
    if not reference and (matches[0]['name'], matches[0]['markning']) in SPECIAL_SUBMISSION_OBJECTS.values():
        raise ValueError("Справочная запись АТФ/больничного требует соответствующий тип.")
    return matches[0]['id']


def infer_submission_type_from_object(cursor, company, object_name):
    cursor.execute(
        "SELECT name, markning FROM meta_objects WHERE company = ? AND name = ?",
        (company, object_name)
    )
    matches = cursor.fetchall()
    if len(matches) > 1:
        raise ValueError("Объект неоднозначен; требуется проверить название и отметку справочника.")
    if matches:
        object_key = (matches[0]['name'], matches[0]['markning'])
        for submission_type, reference in SPECIAL_SUBMISSION_OBJECTS.items():
            if object_key == reference:
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

SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    logger.critical("КРИТИЧЕСКАЯ ОШИБКА: Переменная окружения SECRET_KEY не задана!")
    raise RuntimeError("SECRET_KEY environment variable is required.")
app.secret_key = SECRET_KEY

SYNC_API_KEY = os.environ.get('SYNC_API_KEY')
if not SYNC_API_KEY:
    logger.critical("КРИТИЧЕСКАЯ ОШИБКА: Переменная окружения SYNC_API_KEY не задана!")
    raise RuntimeError("SYNC_API_KEY environment variable is required.")

use_secure = os.environ.get('USE_SECURE_COOKIES', 'false').lower() == 'true'
app.config.update(
    SESSION_COOKIE_SECURE=use_secure,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax'
)

DB_FILE = os.environ.get('DATABASE_PATH', "cloud_database.db")

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
        CREATE TABLE IF NOT EXISTS meta_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meta_objects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            markning TEXT,
            company TEXT,
            UNIQUE(name, company)
        )
    ''')

    cursor.execute("SELECT COUNT(*) FROM meta_employees")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT OR IGNORE INTO meta_employees (id, name, salary_rate, is_active) VALUES (?, ?, ?, ?)", (1, "Aliaksei Patonich", 0.0, 1))

    cursor.execute("SELECT id, name, salary_rate FROM meta_employees")
    seeded_ob_rates = []
    for employee_row in cursor.fetchall():
        fallback_rate = OB_BASE_RATE_FALLBACKS.get(normalize_employee_name(employee_row["name"]))
        if fallback_rate is not None and float(employee_row["salary_rate"] or 0.0) <= 0:
            cursor.execute(
                "UPDATE meta_employees SET salary_rate = ? WHERE id = ?",
                (fallback_rate, employee_row["id"]),
            )
            seeded_ob_rates.append((employee_row["name"], fallback_rate))
    if seeded_ob_rates:
        logger.warning("Seeded missing OB base rates from configured fallback list: %s", seeded_ob_rates)

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
        
    hashed_pw = generate_password_hash(admin_pass)
    now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")
    if cursor.fetchone()[0] == 0:
        cursor.execute('''
            INSERT INTO users (username, password_hash, role, employee_id, is_active, created_at)
            VALUES (?, ?, 'admin', NULL, 1, ?)
        ''', (admin_user, hashed_pw, now_str))
    else:
        cursor.execute('''
            UPDATE users SET password_hash = ? WHERE role = 'admin'
        ''', (hashed_pw,))

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

    conn.commit()
    conn.close()

init_cloud_db()

def verify_sync_key():
    sync_key = request.headers.get('X-Sync-Key') or request.headers.get('X-API-Key')
    if not sync_key or sync_key != SYNC_API_KEY:
        logger.warning(f"Несанкционированная попытка доступа с IP: {request.remote_addr}")
        return False
    return True

@app.route('/get-active-employees', methods=['GET'])
def get_active_employees():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM meta_employees WHERE is_active = 1 ORDER BY name")
    rows = cursor.fetchall()
    conn.close()
    
    employees = [{"id": r['id'], "name": r['name']} for r in rows]
    return jsonify({"ok": True, "employees": employees})

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
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password or len(username) > 100:
        return jsonify({"ok": False, "error": "Неверные данные для входа"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (username,))
    user = cursor.fetchone()

    if not user:
        conn.close()
        return jsonify({"ok": False, "error": "Пользователь не найден в системе"}), 401

    elif not user['password_hash']:
        hashed_pw = generate_password_hash(password)
        cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hashed_pw, user['id']))
        conn.commit()
        
        cursor.execute("SELECT * FROM users WHERE id = ?", (user['id'],))
        user = cursor.fetchone()

    elif not check_password_hash(user['password_hash'], password):
        conn.close()
        return jsonify({"ok": False, "error": "Неверный пин-код или пароль"}), 401

    emp_id = user['employee_id']
    if not emp_id and user['role'] != 'admin':
        cursor.execute("SELECT id FROM meta_employees WHERE name = ?", (user['username'],))
        emp_row = cursor.fetchone()
        if emp_row:
            emp_id = emp_row['id']
            cursor.execute("UPDATE users SET employee_id = ? WHERE id = ?", (emp_id, user['id']))
            conn.commit()

    conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), user['id']))
    conn.commit()
    conn.close()

    session.permanent = True
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['role'] = user['role']
    session['employee_id'] = emp_id

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

    cursor.execute("SELECT name, markning, company FROM meta_objects ORDER BY company, name")
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

    if submission_type == "ob":
        if not ob_schema_ready(cursor):
            conn.close()
            return jsonify({"ok": False, "error": "OB-схема не подготовлена. Выполните migrate_ob.py с backup."}), 503
        try:
            ob_result = calculate_ob_payload(
                cursor, date_str, data.get("start_time"), data.get("end_time"), employee
            )
            if not ob_result["base_rate_configured"]:
                conn.close()
                return jsonify({"ok": False, "error": "В профиле сотрудника не настроена базовая ставка. Обратитесь к администратору."}), 400
            date_str = ob_result["date"]
            hours = ob_result["hours"]
            rate = 0.0
            transport = 0.0
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
            cursor, data.get("date"), data.get("start_time"), data.get("end_time"), employee
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
            )
            if not ob_result["base_rate_configured"]:
                raise ValueError("В профиле сотрудника не настроена базовая ставка. Обратитесь к администратору.")
            date_val = ob_result["date"]
            hours = ob_result["hours"]
            transport = 0.0
        else:
            hours = float(data.get('hours', existing['hours']))
            transport = round(float(data.get('transport', existing['transport'])), 2)
            if not (0.5 <= hours <= 24) or hours % 0.5 != 0 or not (0 <= transport <= 10000):
                raise ValueError("Часы или транспорт вне допустимого диапазона.")
            if any(str(value).lower() in ('nan', 'inf', '-inf') for value in (hours, transport)):
                raise ValueError("Числовое поле должно быть конечным.")
        comment = str(data.get('comment', existing['comment'] or ''))[:2000]
        invoiced = int(existing['invoiced']) if submission_type == 'work' else 0
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
        if ob_result:
            replace_ob_segments(cursor, sync_id, ob_result["segments"])
        elif ob_schema_ready(cursor) and existing['submission_type'] == "ob":
            cursor.execute("DELETE FROM ob_segments WHERE shift_id = ?", (sync_id,))
        conn.commit()
        response = {"ok": True, "sync_id": sync_id, "version": new_version, "change_seq": change_seq}
        if ob_result:
            response.update({"hours": ob_result["hours"], "segments": ob_result["segments"], "total_amount": ob_result["total_amount"]})
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
            "company": r['company'],
            "rate": r['rate'],
            "transport": r['transport'],
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
                    {key: segment[key] for key in (
                        "segment_date", "start_time", "end_time", "hours", "category",
                        "premium_percent", "multiplier", "amount",
                    )}
                    for segment in segments
                ]
                shifts[-1].update({
                    "start_time": ob_record["start_time"],
                    "end_time": ob_record["end_time"],
                    "ob_segments": public_segments,
                    "ob_total_amount": round(sum(segment["amount"] for segment in segments), 2),
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
        'Дата', 'Сотрудник', 'Фирма', 'Объект', 'Тип записи', 'Часы', 'Ставка',
        'Транспорт', 'Комментарий', 'Время начала', 'Время окончания', 'OB сегменты', 'OB сумма'
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

@app.route('/sync-desktop-data', methods=['POST'])
def sync_desktop_data():
    if not verify_sync_key():
        return jsonify({"ok": False, "error": "Доступ запрещен. Требуется валидный ключ синхронизации."}), 403

    data = request.json or {}
    employees = data.get("employees", [])
    companies = data.get("companies", [])
    objects = data.get("objects", [])
    desktop_shifts = data.get("shifts", [])

    if not isinstance(employees, list) or not isinstance(companies, list) or \
       not isinstance(objects, list) or not isinstance(desktop_shifts, list):
        return jsonify({"ok": False, "error": "Неверный формат данных: ожидаются списки"}), 400

    if len(employees) > 500 or len(companies) > 500 or len(objects) > 2000 or len(desktop_shifts) > 5000:
        return jsonify({"ok": False, "error": "Превышен лимит записей в пакете синхронизации"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    employee_sync_results = []

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
                else:
                    emp_name = " ".join(str(emp).split()) if emp else ""
                    if not emp_name or len(emp_name) > 150:
                        raise ValueError("Недопустимое имя сотрудника")
                    emp_rate = 0.0
                    emp_active = 1

                matching_profiles = find_employee_profiles(cursor, emp_name)
                if matching_profiles:
                    profile_ids = [profile['id'] for profile in matching_profiles]
                    placeholders = ', '.join('?' for _ in profile_ids)
                    cursor.execute(
                        f"UPDATE meta_employees SET salary_rate = ?, is_active = ? WHERE id IN ({placeholders})",
                        (emp_rate, emp_active, *profile_ids),
                    )
                else:
                    cursor.execute('''
                        INSERT INTO meta_employees (name, salary_rate, is_active)
                        VALUES (?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            salary_rate = excluded.salary_rate,
                            is_active = excluded.is_active
                    ''', (emp_name, emp_rate, emp_active))
                stored_profiles = find_employee_profiles(cursor, emp_name)
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

        if objects:
            for obj in objects:
                name, mark, comp = safe_parse_obj(obj)
                if name and "{" not in name:
                    if len(name) > 200 or len(mark) > 50 or len(comp) > 150:
                        return jsonify({"ok": False, "error": "Превышена длина полей объекта"}), 400
                    cursor.execute('''
                        INSERT OR IGNORE INTO meta_objects (name, markning, company) VALUES (?, ?, ?)
                        ON CONFLICT(name, company) DO UPDATE SET markning = excluded.markning
                    ''', (name, mark, comp if comp else "Privat"))

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
    })

def serialize_submission(row):
    payload = {
        "id": f"submission:{row['id']}",
        "request_id": row['request_id'],
        "sync_id": row['sync_id'],
        "version": row['version'],
        "change_seq": row['change_seq'],
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
        "submission_type": row['submission_type'] or "work",
        "invoiced": row['invoiced']
    }
    if "start_time" in row.keys() and (row["submission_type"] or "work") == "ob":
        payload.update({
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "ob_base_rate": row["ob_base_rate"],
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
    conn.commit()
    conn.close()
    return jsonify({
        "ok": True,
        "changes": [serialize_submission(row) for row in rows],
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
        "typed_tombstones": True
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
                validate_submission_object(cursor, submission_type, company, object_name)
                invoiced = int(bool(payload.get('invoiced', 0))) if submission_type == 'work' else 0
                if submission_type == "ob":
                    if not ob_schema_ready(cursor):
                        raise ValueError("OB-схема не подготовлена; выполните миграцию с backup")
                    ob_result = calculate_ob_payload(
                        cursor, date_val, payload.get("start_time"), payload.get("end_time"), employee
                    )
                    if not ob_result["base_rate_configured"]:
                        raise ValueError("В профиле сотрудника не настроена базовая ставка.")
                    date_val = ob_result["date"]
                    hours = ob_result["hours"]
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
                    if tombstone_company and tombstone_object:
                        validate_submission_object(cursor, submission_type, tombstone_company, tombstone_object)
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
            if ob_schema_ready(cursor):
                if operation == "delete" and existing and existing["submission_type"] == "ob":
                    cursor.execute("DELETE FROM ob_segments WHERE shift_id = ?", (sync_id,))
                elif ob_result:
                    replace_ob_segments(cursor, sync_id, ob_result["segments"])
            results.append({"operation_id": operation_id, "ok": True, "version": version, "change_seq": change_seq})

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
    conn.close()

    shifts = []
    for row in legacy_rows:
        shifts.append({
            "id": row['id'], "request_id": row['request_id'] or f"legacy-cloud:{row['id']}",
            "sync_id": row['request_id'] or f"legacy-cloud:{row['id']}", "version": 1,
            "source": "legacy",
            "date": row['date'], "employee": row['employee'], "company": row['company'],
            "object_name": row['object_name'], "hours": row['hours'], "rate": row['rate'],
            "transport": row['transport'], "comment": row['comment'] or ""
        })
    shifts.extend(serialize_submission(row) for row in submission_rows)
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