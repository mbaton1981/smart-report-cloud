import sqlite3
import time
import ast
import os
import shutil
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash

# Настройка системного логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

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
    # 🛡 Если по пути базы данных случайно образовалась директория — удаляем её
    if os.path.exists(DB_FILE) and os.path.isdir(DB_FILE):
        try:
            os.rmdir(DB_FILE)
        except Exception:
            pass

    # Автоматическое создание родительской папки (например, /data/) перед подключением
    db_dir = os.path.dirname(os.path.abspath(DB_FILE))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA foreign_keys = ON;')
    conn.row_factory = sqlite3.Row
    return conn

def create_database_backup():
    """Создание резервной копии базы данных при запуске сервера"""
    try:
        if not os.path.exists(DB_FILE):
            return
        
        backup_dir = "backups"
        os.makedirs(backup_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = os.path.join(backup_dir, f"backup_{timestamp}.db")
        
        # Безопасное копирование файла базы данных
        shutil.copy2(DB_FILE, backup_path)
        logger.info(f"Резервная копия базы данных успешно создана: {backup_path}")
        
        # Очистка старых бэкапов (оставляем последние 10 штук, чтобы не забивать диск)
        backups = sorted([os.path.join(backup_dir, f) for f in os.listdir(backup_dir) if f.endswith('.db')])
        if len(backups) > 10:
            for old_backup in backups[:-10]:
                try:
                    os.remove(old_backup)
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Не удалось создать резервную копию базы данных: {e}")

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
    # Создаем бэкап существующей базы перед инициализацией
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
    
    cursor.execute("PRAGMA table_info(cloud_shifts)")
    shift_cols = [c[1] for c in cursor.fetchall()]
    if 'request_id' not in shift_cols:
        try:
            cursor.execute("ALTER TABLE cloud_shifts ADD COLUMN request_id TEXT UNIQUE")
        except Exception:
            pass

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

    date_str = str(data.get('date', '')).strip()
    company = str(data.get('company', '')).strip()
    object_name = str(data.get('object_name', '')).strip()
    request_id = data.get('request_id')
    comment = str(data.get('comment', ''))[:2000]

    conn = get_db_connection()
    cursor = conn.cursor()

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

        cursor.execute("SELECT id FROM meta_objects WHERE name = ? AND company = ?", (final_obj_name, company))
        if not cursor.fetchone():
            conn.close()
            return jsonify({"ok": False, "error": "Объект не принадлежит выбранной фирме или не существует"}), 400

        if request_id:
            cursor.execute("SELECT id FROM cloud_shifts WHERE request_id = ?", (request_id,))
            if cursor.fetchone():
                conn.close()
                return jsonify({"ok": False, "error": "Такая смена уже была отправлена ранее"}), 409

        sixty_secs_ago = (datetime.utcnow() - timedelta(seconds=60)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('''
            SELECT id FROM cloud_shifts 
            WHERE employee = ? AND date = ? AND company = ? AND object_name = ? AND hours = ? AND created_at >= ?
        ''', (employee, date_str, company, final_obj_name, hours, sixty_secs_ago))
        if cursor.fetchone():
            conn.close()
            return jsonify({"ok": False, "error": "Похожая смена уже была зарегистрирована только что. Подождите немного."}), 409

        cursor.execute('''
            INSERT INTO cloud_shifts (request_id, date, employee, company, object_name, hours, rate, transport, comment, synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ''', (request_id, date_str, employee, company, final_obj_name, hours, rate, transport, comment))
        
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка при сохранении смены: {str(e)}")
        return jsonify({"ok": False, "error": "Внутренняя ошибка сервера при сохранении смены."}), 500
    finally:
        conn.close()

    return jsonify({"ok": True, "message": "Смена успешно сохранена!"}), 201

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
                SELECT date, object_name, hours FROM cloud_shifts 
                WHERE employee = ? AND date >= ? AND date < ? 
                ORDER BY date ASC
            """, (emp, start_date, end_date))
        except Exception:
            cursor.execute("SELECT date, object_name, hours FROM cloud_shifts WHERE employee = ? ORDER BY date DESC LIMIT 31", (emp,))
    else:
        cursor.execute("SELECT date, object_name, hours FROM cloud_shifts WHERE employee = ? ORDER BY date DESC LIMIT 31", (emp,))

    rows = cursor.fetchall()
    
    shifts = []
    for r in rows:
        obj_name = r['object_name']
        marking = ""
        cursor.execute("SELECT markning FROM meta_objects WHERE name = ?", (obj_name,))
        obj_meta = cursor.fetchone()
        if obj_meta and obj_meta['markning']:
            marking = obj_meta['markning']
            
        shifts.append({
            "date": r['date'], 
            "object_name": obj_name, 
            "marking": marking,
            "hours": r['hours']
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
        SELECT date FROM cloud_shifts 
        WHERE employee = ? AND date >= ? AND date <= ?
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

    try:
        if employees:
            for emp in employees:
                if isinstance(emp, dict):
                    emp_name = str(emp.get('name', '')).strip()
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
                    emp_name = str(emp).strip() if emp else ""
                    if not emp_name or len(emp_name) > 150:
                        raise ValueError("Недопустимое имя сотрудника")
                    emp_rate = 0.0
                    emp_active = 1

                cursor.execute('''
                    INSERT INTO meta_employees (name, salary_rate, is_active) 
                    VALUES (?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET 
                        salary_rate = excluded.salary_rate,
                        is_active = excluded.is_active
                ''', (emp_name, emp_rate, emp_active))

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

        if desktop_shifts:
            for s in desktop_shifts:
                if not isinstance(s, dict):
                    return jsonify({"ok": False, "error": "Неверный формат смены"}), 400

                date_val = str(s.get('date', '')).strip()
                try:
                    datetime.strptime(date_val, '%Y-%m-%d')
                except ValueError:
                    return jsonify({"ok": False, "error": f"Некорректный формат даты: {date_val}"}), 400

                emp_val = str(s.get('employee', '')).strip()
                if not emp_val or len(emp_val) > 150:
                    return jsonify({"ok": False, "error": "Некорректное имя сотрудника в смене"}), 400

                try:
                    hrs_val = float(s.get('hours', 0.0))
                    rate_val = round(float(s.get('rate', 0.0)), 2)
                    transport_val = round(float(s.get('transport', 0.0)), 2)
                    
                    if not (0.5 <= hrs_val <= 24) or str(hrs_val).lower() in ['nan', 'inf', '-inf']:
                        raise ValueError("Часы вне диапазона")
                    if not (0 <= rate_val <= 100000) or str(rate_val).lower() in ['nan', 'inf', '-inf']:
                        raise ValueError("Некорректная ставка")
                    if not (0 <= transport_val <= 10000) or str(transport_val).lower() in ['nan', 'inf', '-inf']:
                        raise ValueError("Некорректный транспорт")
                except (TypeError, ValueError) as err:
                    return jsonify({"ok": False, "error": f"Ошибка параметров смены: {err}"}), 400

                name, _, _ = safe_parse_obj(s.get('object_name'))
                obj_name = name or str(s.get('object_name', '')).strip()
                if not obj_name or len(obj_name) > 200:
                    return jsonify({"ok": False, "error": "Некорректное имя объекта"}), 400

                comment_val = str(s.get('comment', ''))[:2000]
                req_id = str(s.get('request_id', ''))[:100]

                # 🛡 Мягкая обработка дубликатов по request_id без падения пакета
                if req_id:
                    cursor.execute("SELECT id FROM cloud_shifts WHERE request_id = ?", (req_id,))
                    if cursor.fetchone():
                        continue  # Уже существует — тихо пропускаем

                cursor.execute('''
                    INSERT OR IGNORE INTO cloud_shifts (request_id, date, employee, company, object_name, hours, rate, transport, comment, synced)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ''', (
                    req_id if req_id else None, date_val, emp_val, str(s.get('company', 'Privat')), obj_name,
                    hrs_val, rate_val, transport_val, comment_val
                ))

        conn.commit()
    except Exception as e:
        logger.error(f"Safe sync validation error: {e}")
        conn.rollback()
        return jsonify({"ok": False, "error": f"Ошибка валидации и синхронизации: {str(e)}"}), 400
    finally:
        conn.close()

    return jsonify({"ok": True, "status": "safe_merged_and_validated"})

@app.route('/get-unsynced', methods=['GET'])
def get_unsynced():
    if not verify_sync_key():
        return jsonify({"ok": False, "error": "Доступ запрещен"}), 403

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, date, employee, company, object_name, hours, rate, transport, comment FROM cloud_shifts WHERE synced = 0")
    rows = cursor.fetchall()
    conn.close()
    
    shifts = [{
        "id": r['id'], "date": r['date'], "employee": r['employee'], "company": r['company'],
        "object_name": r['object_name'], "hours": r['hours'], "rate": r['rate'], "transport": r['transport'], "comment": r['comment']
    } for r in rows]
    return jsonify({"ok": True, "shifts": shifts})

@app.route('/mark-synced', methods=['POST'])
def mark_synced():
    if not verify_sync_key():
        return jsonify({"ok": False, "error": "Доступ запрещен"}), 403

    data = request.json or {}
    ids = data.get("ids", [])
    if ids:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.executemany("UPDATE cloud_shifts SET synced = 1 WHERE id = ?", [(i,) for i in ids])
        conn.commit()
        conn.close()
    return jsonify({"ok": True, "status": "marked"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)