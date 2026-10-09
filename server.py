import sqlite3
import time
import ast
import os
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
# Секретный ключ для сессий (на Render берется из окружения)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-it-12345')
app.permanent_session_lifetime = timedelta(hours=8)

DB_FILE = "cloud_database.db"

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
        return str(val.get('name', '')).strip(), str(val.get('markning', '')).strip(), str(val.get('company', 'Privat')).strip()
    
    val_str = str(val).strip()
    if "{" in val_str and "'name'" in val_str:
        try:
            d = ast.literal_eval(val_str)
            if isinstance(d, dict):
                return str(d.get('name', '')).strip(), str(d.get('markning', '')).strip(), str(d.get('company', 'Privat')).strip()
        except Exception:
            pass
    return val_str, "", "Privat"

def init_cloud_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Таблица пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
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
            date TEXT NOT NULL,
            employee TEXT NOT NULL,
            company TEXT NOT NULL,
            object_name TEXT NOT NULL,
            hours REAL NOT NULL,
            rate REAL NOT NULL,
            transport REAL DEFAULT 0.0,
            comment TEXT,
            synced INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meta_employees (
            name TEXT UNIQUE NOT NULL,
            salary_rate REAL DEFAULT 0.0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meta_companies (
            name TEXT UNIQUE NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meta_objects (
            name TEXT NOT NULL,
            markning TEXT,
            company TEXT,
            UNIQUE(name, company)
        )
    ''')

    # Дефолтные данные, если таблицы пустые
    cursor.execute("SELECT COUNT(*) FROM meta_employees")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT OR IGNORE INTO meta_employees (name) VALUES (?)", ("Aliaksei Patonich",))

    cursor.execute("SELECT COUNT(*) FROM meta_companies")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT OR IGNORE INTO meta_companies (name) VALUES (?)", ("Privat",))

    cursor.execute("SELECT COUNT(*) FROM meta_objects")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT OR IGNORE INTO meta_objects (name, markning, company) VALUES (?, ?, ?)", ("Bygg och renovering", "Sthlm", "Privat"))

    # Создаем администратора по умолчанию, если его нет
    cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")
    if cursor.fetchone()[0] == 0:
        admin_user = os.environ.get('ADMIN_USERNAME', 'admin')
        admin_pass = os.environ.get('ADMIN_PASSWORD', 'AdminSecure2026!')
        hashed_pw = generate_password_hash(admin_pass)
        now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('''
            INSERT INTO users (username, password_hash, role, employee_id, is_active, created_at)
            VALUES (?, ?, 'admin', NULL, 1, ?)
        ''', (admin_user, hashed_pw, now_str))

    conn.commit()
    conn.close()

init_cloud_db()

# Эндпоинты авторизации
@app.route('/login', methods=['POST'])
def login():
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return jsonify({"ok": False, "error": "Укажите логин и пароль"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (username,))
    user = cursor.fetchone()
    conn.close()

    if user and check_password_hash(user['password_hash'], password):
        session.permanent = True
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['role'] = user['role']
        session['employee_id'] = user['employee_id']

        # Обновляем last_login
        conn = get_db_connection()
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), user['id']))
        conn.commit()
        conn.close()

        return jsonify({
            "ok": True,
            "message": "Успешный вход",
            "user": {
                "username": user['username'],
                "role": user['role'],
                "employee_id": user['employee_id']
            }
        })

    return jsonify({"ok": False, "error": "Неверный логин или пароль"}), 401

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

@app.route('/')
def index():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM meta_employees ORDER BY name")
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
    data = request.json or {}
    
    # Валидация базовых полей
    date_str = data.get('date')
    hours = data.get('hours')
    try:
        hours = float(hours)
        if not (0 <= hours <= 24):
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Некорректное значение часов (от 0 до 24)"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    name, _, _ = safe_parse_obj(data.get('object_name'))
    
    cursor.execute('''
        INSERT INTO cloud_shifts (date, employee, company, object_name, hours, rate, transport, comment, synced)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
    ''', (
        date_str,
        data.get('employee'),
        data.get('company'),
        name or data.get('object_name'),
        hours,
        data.get('rate', 0.0),
        data.get('transport', 0.0),
        str(data.get('comment', ''))[:2000],
    ))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": "Смена успешно отправлена!"})

@app.route('/check-employee-shifts', methods=['POST'])
def check_employee_shifts():
    data = request.json or {}
    emp = data.get('employee')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT date, object_name, hours FROM cloud_shifts WHERE employee = ? ORDER BY date DESC LIMIT 5", (emp,))
    rows = cursor.fetchall()
    conn.close()
    
    shifts = [{"date": r['date'], "object_name": r['object_name'], "hours": r['hours']} for r in rows]
    return jsonify({"ok": True, "recent_shifts": shifts})

@app.route('/sync-desktop-data', methods=['POST'])
def sync_desktop_data():
    # Разрешаем синхронизацию без жесткой блокировки 403, если ключ не задан или совпадает
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
                if emp:
                    cursor.execute("INSERT OR IGNORE INTO meta_employees (name) VALUES (?)", (str(emp),))

        cursor.execute("DELETE FROM meta_companies")
        if companies:
            for comp in companies:
                if comp:
                    cursor.execute("INSERT OR IGNORE INTO meta_companies (name) VALUES (?)", (str(comp),))

        cursor.execute("DELETE FROM meta_objects")
        if objects:
            for obj in objects:
                if isinstance(obj, dict):
                    name = str(obj.get('name', '')).strip()
                    mark = str(obj.get('markning', '')).strip()
                    comp = str(obj.get('company', '')).strip()
                else:
                    name, mark, comp = safe_parse_obj(obj)

                if name and "{" not in name:
                    cursor.execute('''
                        INSERT OR IGNORE INTO meta_objects (name, markning, company) VALUES (?, ?, ?)
                    ''', (name, mark, comp if comp else "Privat"))

        cursor.execute("DELETE FROM cloud_shifts")
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