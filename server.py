import sqlite3
import time
import ast
import os
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-it-12345')
app.permanent_session_lifetime = timedelta(hours=8)

DB_FILE = os.environ.get('DATABASE_PATH', "cloud_database.db")

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA foreign_keys = ON;')
    conn.row_factory = sqlite3.Row
    return conn

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
            id INTEGER PRIMARY KEY,
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
    admin_pass = os.environ.get('ADMIN_PASSWORD', '1981')
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

    conn.commit()
    conn.close()

init_cloud_db()

@app.route('/get-active-employees', methods=['GET'])
def get_active_employees():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM meta_employees WHERE is_active = 1 ORDER BY name")
    rows = cursor.fetchall()
    conn.close()
    
    employees = [r['name'] for r in rows]
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
        now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        hashed_pw = generate_password_hash(password)
        cursor.execute('''
            INSERT INTO users (username, password_hash, role, is_active, created_at)
            VALUES (?, ?, 'user', 1, ?)
        ''', (username, hashed_pw, now_str))
        conn.commit()
        
        cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
        user = cursor.fetchone()

    elif not user['password_hash']:
        hashed_pw = generate_password_hash(password)
        cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hashed_pw, user['id']))
        conn.commit()
        
        cursor.execute("SELECT * FROM users WHERE id = ?", (user['id'],))
        user = cursor.fetchone()

    elif not check_password_hash(user['password_hash'], password):
        conn.close()
        return jsonify({"ok": False, "error": "Неверный пин-код или пароль"}), 401

    conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), user['id']))
    conn.commit()
    conn.close()

    session.permanent = True
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['role'] = user['role']
    session['employee_id'] = user['employee_id']

    return jsonify({
        "ok": True,
        "message": "Успешный вход",
        "user": {
            "username": user['username'],
            "role": user['role'],
            "employee_id": user['employee_id']
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

@app.route('/admin/reset-user-pin', methods=['POST'])
def admin_reset_user_pin():
    sync_key = request.headers.get('X-Sync-Key')
    expected_key = os.environ.get('SYNC_API_KEY')
    
    is_admin_session = session.get('role') == 'admin'
    is_valid_sync = expected_key and sync_key and sync_key == expected_key
    
    if not is_admin_session and not is_valid_sync and expected_key:
        return jsonify({"ok": False, "error": "Доступ запрещен"}), 403
        
    data = request.json or {}
    username = str(data.get('username', '')).strip()
    if not username:
        return jsonify({"ok": False, "error": "Не указано имя пользователя"}), 400
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET password_hash = NULL WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    
    return jsonify({"ok": True, "message": f"Пин-код для пользователя {username} успешно сброшен"})

@app.route('/admin/get-users-status', methods=['GET'])
def admin_get_users_status():
    sync_key = request.headers.get('X-Sync-Key')
    expected_key = os.environ.get('SYNC_API_KEY')
    if expected_key and sync_key and sync_key != expected_key:
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
    employee = str(data.get('employee', '')).strip()
    company = str(data.get('company', '')).strip()
    object_name = str(data.get('object_name', '')).strip()
    request_id = data.get('request_id')
    comment = str(data.get('comment', ''))[:2000]

    if session.get('role') != 'admin':
        session_username = session.get('username', '').lower()
        if session_username not in employee.lower():
            return jsonify({"ok": False, "error": "Вы можете отправлять смены только от своего имени"}), 403

    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({"ok": False, "error": "Некорректная дата (ожидается формат YYYY-MM-DD)"}), 400

    try:
        hours = float(data.get('hours'))
        if not (0.5 <= hours <= 24) or (hours % 0.5 != 0):
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Некорректное значение часов (от 0.5 до 24, шаг 0.5)"}), 400

    try:
        rate = float(data.get('rate', 0.0))
        transport = float(data.get('transport', 0.0))
        if rate < 0 or transport < 0:
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Некорректные числовые значения ставки или транспорта"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT id FROM meta_employees WHERE name = ? AND is_active = 1", (employee,))
        if not cursor.fetchone():
            return jsonify({"ok": False, "error": "Указанный сотрудник не найден или неактивен"}), 400

        cursor.execute("SELECT id FROM meta_companies WHERE name = ?", (company,))
        if not cursor.fetchone():
            return jsonify({"ok": False, "error": "Указанная фирма не найдена"}), 400

        parsed_obj_name, _, _ = safe_parse_obj(object_name)
        final_obj_name = parsed_obj_name or object_name

        cursor.execute("SELECT id FROM meta_objects WHERE name = ? AND company = ?", (final_obj_name, company))
        if not cursor.fetchone():
            return jsonify({"ok": False, "error": "Объект не принадлежит выбранной фирме или не существует"}), 400

        if request_id:
            cursor.execute("SELECT id FROM cloud_shifts WHERE request_id = ?", (request_id,))
            if cursor.fetchone():
                return jsonify({"ok": False, "error": "Такая смена уже была отправлена ранее"}), 409

        sixty_secs_ago = (datetime.utcnow() - timedelta(seconds=60)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('''
            SELECT id FROM cloud_shifts 
            WHERE employee = ? AND date = ? AND company = ? AND object_name = ? AND hours = ? AND created_at >= ?
        ''', (employee, date_str, company, final_obj_name, hours, sixty_secs_ago))
        if cursor.fetchone():
            return jsonify({"ok": False, "error": "Похожая смена уже была зарегистрирована только что. Подождите немного."}), 409

        cursor.execute('''
            INSERT INTO cloud_shifts (request_id, date, employee, company, object_name, hours, rate, transport, comment, synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ''', (request_id, date_str, employee, company, final_obj_name, hours, rate, transport, comment))
        
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": f"Ошибка сервера при сохранении: {str(e)}"}), 500
    finally:
        conn.close()

    return jsonify({"ok": True, "message": "Смена успешно сохранена!"}), 201

@app.route('/check-employee-shifts', methods=['POST'])
def check_employee_shifts():
    if 'user_id' not in session:
        return jsonify({"ok": False, "error": "Требуется авторизация"}), 401

    data = request.json or {}
    emp = data.get('employee')

    if session.get('role') != 'admin':
        session_username = session.get('username', '').lower()
        if session_username not in str(emp).lower():
            return jsonify({"ok": False, "error": "Доступ запрещен"}), 403

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT date, object_name, hours FROM cloud_shifts WHERE employee = ? ORDER BY date DESC LIMIT 5", (emp,))
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

@app.route('/sync-desktop-data', methods=['POST'])
def sync_desktop_data():
    sync_key = request.headers.get('X-Sync-Key')
    expected_key = os.environ.get('SYNC_API_KEY')
    if expected_key and sync_key and sync_key != expected_key:
        return jsonify({"ok": False, "error": "Доступ запрещен"}), 403

    data = request.json or {}
    employees = data.get("employees", [])
    companies = data.get("companies", [])
    objects = data.get("objects", [])
    desktop_shifts = data.get("shifts", [])

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("DELETE FROM meta_employees")
        if employees:
            for emp in employees:
                if isinstance(emp, dict):
                    emp_name = str(emp.get('name', '')).strip()
                    emp_rate = float(emp.get('salary_rate', 0.0))
                    emp_active = int(emp.get('is_active', 1))
                else:
                    emp_name = str(emp).strip()
                    emp_rate = 0.0
                    emp_active = 1

                if emp_name:
                    cursor.execute('''
                        INSERT OR REPLACE INTO meta_employees (name, salary_rate, is_active) 
                        VALUES (?, ?, ?)
                    ''', (emp_name, emp_rate, emp_active))

        cursor.execute("DELETE FROM meta_companies")
        if companies:
            for comp in companies:
                if comp:
                    comp_name = str(comp.get('name', '') if isinstance(comp, dict) else comp).strip()
                    if comp_name:
                        cursor.execute("INSERT OR IGNORE INTO meta_companies (name) VALUES (?)", (comp_name,))

        cursor.execute("DELETE FROM meta_objects")
        if objects:
            for obj in objects:
                name, mark, comp = safe_parse_obj(obj)
                if name and "{" not in name:
                    cursor.execute('''
                        INSERT OR IGNORE INTO meta_objects (name, markning, company) VALUES (?, ?, ?)
                    ''', (name, mark, comp if comp else "Privat"))

        # Всегда полностью очищаем старые смены в облаке перед заливкой актуальных
        cursor.execute("DELETE FROM cloud_shifts")
        
        if desktop_shifts:
            for s in desktop_shifts:
                name, _, _ = safe_parse_obj(s.get('object_name'))
                obj_name = name or s.get('object_name')
                
                cursor.execute('''
                    INSERT INTO cloud_shifts (date, employee, company, object_name, hours, rate, transport, comment, synced)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    s.get('date'), s.get('employee'), s.get('company'), obj_name,
                    s.get('hours', 0.0), s.get('rate', 0.0), s.get('transport', 0.0), str(s.get('comment', ''))[:2000], 1
                ))

        conn.commit()
    except Exception as e:
        print(f"Sync error: {e}")
        conn.rollback()
    finally:
        conn.close()

    return jsonify({"ok": True, "status": "synced"})

@app.route('/get-unsynced', methods=['GET'])
def get_unsynced():
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
    app.run(host='0.0.0.0', port=5000)import sqlite3
import time
import ast
import os
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-it-12345')
app.permanent_session_lifetime = timedelta(hours=8)

DB_FILE = os.environ.get('DATABASE_PATH', "cloud_database.db")

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA foreign_keys = ON;')
    conn.row_factory = sqlite3.Row
    return conn

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
            id INTEGER PRIMARY KEY,
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
    admin_pass = os.environ.get('ADMIN_PASSWORD', '1981')
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

    conn.commit()
    conn.close()

init_cloud_db()

@app.route('/get-active-employees', methods=['GET'])
def get_active_employees():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM meta_employees WHERE is_active = 1 ORDER BY name")
    rows = cursor.fetchall()
    conn.close()
    
    employees = [r['name'] for r in rows]
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
        now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        hashed_pw = generate_password_hash(password)
        cursor.execute('''
            INSERT INTO users (username, password_hash, role, is_active, created_at)
            VALUES (?, ?, 'user', 1, ?)
        ''', (username, hashed_pw, now_str))
        conn.commit()
        
        cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
        user = cursor.fetchone()

    elif not user['password_hash']:
        hashed_pw = generate_password_hash(password)
        cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hashed_pw, user['id']))
        conn.commit()
        
        cursor.execute("SELECT * FROM users WHERE id = ?", (user['id'],))
        user = cursor.fetchone()

    elif not check_password_hash(user['password_hash'], password):
        conn.close()
        return jsonify({"ok": False, "error": "Неверный пин-код или пароль"}), 401

    conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), user['id']))
    conn.commit()
    conn.close()

    session.permanent = True
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['role'] = user['role']
    session['employee_id'] = user['employee_id']

    return jsonify({
        "ok": True,
        "message": "Успешный вход",
        "user": {
            "username": user['username'],
            "role": user['role'],
            "employee_id": user['employee_id']
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

@app.route('/admin/reset-user-pin', methods=['POST'])
def admin_reset_user_pin():
    sync_key = request.headers.get('X-Sync-Key')
    expected_key = os.environ.get('SYNC_API_KEY')
    
    is_admin_session = session.get('role') == 'admin'
    is_valid_sync = expected_key and sync_key and sync_key == expected_key
    
    if not is_admin_session and not is_valid_sync and expected_key:
        return jsonify({"ok": False, "error": "Доступ запрещен"}), 403
        
    data = request.json or {}
    username = str(data.get('username', '')).strip()
    if not username:
        return jsonify({"ok": False, "error": "Не указано имя пользователя"}), 400
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET password_hash = NULL WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    
    return jsonify({"ok": True, "message": f"Пин-код для пользователя {username} успешно сброшен"})

@app.route('/admin/get-users-status', methods=['GET'])
def admin_get_users_status():
    sync_key = request.headers.get('X-Sync-Key')
    expected_key = os.environ.get('SYNC_API_KEY')
    if expected_key and sync_key and sync_key != expected_key:
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
    employee = str(data.get('employee', '')).strip()
    company = str(data.get('company', '')).strip()
    object_name = str(data.get('object_name', '')).strip()
    request_id = data.get('request_id')
    comment = str(data.get('comment', ''))[:2000]

    if session.get('role') != 'admin':
        session_username = session.get('username', '').lower()
        if session_username not in employee.lower():
            return jsonify({"ok": False, "error": "Вы можете отправлять смены только от своего имени"}), 403

    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({"ok": False, "error": "Некорректная дата (ожидается формат YYYY-MM-DD)"}), 400

    try:
        hours = float(data.get('hours'))
        if not (0.5 <= hours <= 24) or (hours % 0.5 != 0):
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Некорректное значение часов (от 0.5 до 24, шаг 0.5)"}), 400

    try:
        rate = float(data.get('rate', 0.0))
        transport = float(data.get('transport', 0.0))
        if rate < 0 or transport < 0:
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Некорректные числовые значения ставки или транспорта"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT id FROM meta_employees WHERE name = ? AND is_active = 1", (employee,))
        if not cursor.fetchone():
            return jsonify({"ok": False, "error": "Указанный сотрудник не найден или неактивен"}), 400

        cursor.execute("SELECT id FROM meta_companies WHERE name = ?", (company,))
        if not cursor.fetchone():
            return jsonify({"ok": False, "error": "Указанная фирма не найдена"}), 400

        parsed_obj_name, _, _ = safe_parse_obj(object_name)
        final_obj_name = parsed_obj_name or object_name

        cursor.execute("SELECT id FROM meta_objects WHERE name = ? AND company = ?", (final_obj_name, company))
        if not cursor.fetchone():
            return jsonify({"ok": False, "error": "Объект не принадлежит выбранной фирме или не существует"}), 400

        if request_id:
            cursor.execute("SELECT id FROM cloud_shifts WHERE request_id = ?", (request_id,))
            if cursor.fetchone():
                return jsonify({"ok": False, "error": "Такая смена уже была отправлена ранее"}), 409

        sixty_secs_ago = (datetime.utcnow() - timedelta(seconds=60)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('''
            SELECT id FROM cloud_shifts 
            WHERE employee = ? AND date = ? AND company = ? AND object_name = ? AND hours = ? AND created_at >= ?
        ''', (employee, date_str, company, final_obj_name, hours, sixty_secs_ago))
        if cursor.fetchone():
            return jsonify({"ok": False, "error": "Похожая смена уже была зарегистрирована только что. Подождите немного."}), 409

        cursor.execute('''
            INSERT INTO cloud_shifts (request_id, date, employee, company, object_name, hours, rate, transport, comment, synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ''', (request_id, date_str, employee, company, final_obj_name, hours, rate, transport, comment))
        
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": f"Ошибка сервера при сохранении: {str(e)}"}), 500
    finally:
        conn.close()

    return jsonify({"ok": True, "message": "Смена успешно сохранена!"}), 201

@app.route('/check-employee-shifts', methods=['POST'])
def check_employee_shifts():
    if 'user_id' not in session:
        return jsonify({"ok": False, "error": "Требуется авторизация"}), 401

    data = request.json or {}
    emp = data.get('employee')

    if session.get('role') != 'admin':
        session_username = session.get('username', '').lower()
        if session_username not in str(emp).lower():
            return jsonify({"ok": False, "error": "Доступ запрещен"}), 403

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT date, object_name, hours FROM cloud_shifts WHERE employee = ? ORDER BY date DESC LIMIT 5", (emp,))
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

@app.route('/sync-desktop-data', methods=['POST'])
def sync_desktop_data():
    sync_key = request.headers.get('X-Sync-Key')
    expected_key = os.environ.get('SYNC_API_KEY')
    if expected_key and sync_key and sync_key != expected_key:
        return jsonify({"ok": False, "error": "Доступ запрещен"}), 403

    data = request.json or {}
    employees = data.get("employees", [])
    companies = data.get("companies", [])
    objects = data.get("objects", [])
    desktop_shifts = data.get("shifts", [])

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("DELETE FROM meta_employees")
        if employees:
            for emp in employees:
                if isinstance(emp, dict):
                    emp_name = str(emp.get('name', '')).strip()
                    emp_rate = float(emp.get('salary_rate', 0.0))
                    emp_active = int(emp.get('is_active', 1))
                else:
                    emp_name = str(emp).strip()
                    emp_rate = 0.0
                    emp_active = 1

                if emp_name:
                    cursor.execute('''
                        INSERT OR REPLACE INTO meta_employees (name, salary_rate, is_active) 
                        VALUES (?, ?, ?)
                    ''', (emp_name, emp_rate, emp_active))

        cursor.execute("DELETE FROM meta_companies")
        if companies:
            for comp in companies:
                if comp:
                    comp_name = str(comp.get('name', '') if isinstance(comp, dict) else comp).strip()
                    if comp_name:
                        cursor.execute("INSERT OR IGNORE INTO meta_companies (name) VALUES (?)", (comp_name,))

        cursor.execute("DELETE FROM meta_objects")
        if objects:
            for obj in objects:
                name, mark, comp = safe_parse_obj(obj)
                if name and "{" not in name:
                    cursor.execute('''
                        INSERT OR IGNORE INTO meta_objects (name, markning, company) VALUES (?, ?, ?)
                    ''', (name, mark, comp if comp else "Privat"))

        # Всегда полностью очищаем старые смены в облаке перед заливкой актуальных
        cursor.execute("DELETE FROM cloud_shifts")
        
        if desktop_shifts:
            for s in desktop_shifts:
                name, _, _ = safe_parse_obj(s.get('object_name'))
                obj_name = name or s.get('object_name')
                
                cursor.execute('''
                    INSERT INTO cloud_shifts (date, employee, company, object_name, hours, rate, transport, comment, synced)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    s.get('date'), s.get('employee'), s.get('company'), obj_name,
                    s.get('hours', 0.0), s.get('rate', 0.0), s.get('transport', 0.0), str(s.get('comment', ''))[:2000], 1
                ))

        conn.commit()
    except Exception as e:
        print(f"Sync error: {e}")
        conn.rollback()
    finally:
        conn.close()

    return jsonify({"ok": True, "status": "synced"})

@app.route('/get-unsynced', methods=['GET'])
def get_unsynced():
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