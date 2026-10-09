import sqlite3
import time
import ast
from flask import Flask, render_template, request, jsonify
from deep_translator import MyMemoryTranslator

app = Flask(__name__)
DB_FILE = "cloud_database.db"

# Инициализируем стабильный переводчик на шведский язык ('sv')
translator = MyMemoryTranslator(source='auto', target='sv')

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.execute('PRAGMA journal_mode=WAL;')
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
    conn.commit()
    conn.close()

init_cloud_db()

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
    data = request.json
    conn = get_db_connection()
    cursor = conn.cursor()
    name, _, _ = safe_parse_obj(data.get('object_name'))
    
    # Автоматический перехват и перевод комментария на шведский
    raw_comment = data.get('comment', '')
    translated_comment = raw_comment
    
    if raw_comment and raw_comment.strip():
        try:
            translated_comment = translator.translate(raw_comment)
        except Exception as e:
            print(f"Translation error: {e}")
            translated_comment = raw_comment  # В случае сбоя сохраняем оригинал

    cursor.execute('''
        INSERT INTO cloud_shifts (date, employee, company, object_name, hours, rate, transport, comment, synced)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
    ''', (
        data.get('date'),
        data.get('employee'),
        data.get('company'),
        name or data.get('object_name'),
        data.get('hours', 0.0),
        data.get('rate', 0.0),
        data.get('transport', 0.0),
        translated_comment,  # Сохраняем уже переведенный шведский вариант
    ))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": "Смена успешно отправлена и переведена!"})

@app.route('/check-employee-shifts', methods=['POST'])
def check_employee_shifts():
    data = request.json
    emp = data.get('employee')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT date, object_name, hours FROM cloud_shifts WHERE employee = ? ORDER BY date DESC LIMIT 5", (emp,))
    rows = cursor.fetchall()
    conn.close()
    
    shifts = [{"date": r['date'], "object_name": r['object_name'], "hours": r['hours']} for r in rows]
    return jsonify({"recent_shifts": shifts})

@app.route('/sync-desktop-data', methods=['POST'])
def sync_desktop_data():
    data = request.json
    employees = data.get("employees", [])
    companies = data.get("companies", [])
    objects = data.get("objects", [])
    desktop_shifts = data.get("shifts", [])

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Полная синхронизация (перезапись) справочника сотрудников
        cursor.execute("DELETE FROM meta_employees")
        if employees:
            for emp in employees:
                if emp:
                    cursor.execute("INSERT OR IGNORE INTO meta_employees (name) VALUES (?)", (str(emp),))

        # Полная синхронизация (перезапись) справочника компаний
        cursor.execute("DELETE FROM meta_companies")
        if companies:
            for comp in companies:
                if comp:
                    cursor.execute("INSERT OR IGNORE INTO meta_companies (name) VALUES (?)", (str(comp),))

        # Полная синхронизация (перезапись) справочника объектов
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

        # Полная перезапись облачных смен актуальным списком с ПК
        cursor.execute("DELETE FROM cloud_shifts")
        for s in desktop_shifts:
            name, _, _ = safe_parse_obj(s.get('object_name'))
            obj_name = name or s.get('object_name')
            
            cursor.execute('''
                INSERT INTO cloud_shifts (date, employee, company, object_name, hours, rate, transport, comment, synced)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                s.get('date'), s.get('employee'), s.get('company'), obj_name,
                s.get('hours', 0.0), s.get('rate', 0.0), s.get('transport', 0.0), s.get('comment', ''), 1
            ))

        conn.commit()
    except Exception as e:
        print(f"Sync error: {e}")
        conn.rollback()
    finally:
        conn.close()

    return jsonify({"status": "synced"})

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
    return jsonify({"shifts": shifts})

@app.route('/mark-synced', methods=['POST'])
def mark_synced():
    data = request.json
    ids = data.get("ids", [])
    if ids:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.executemany("UPDATE cloud_shifts SET synced = 1 WHERE id = ?", [(i,) for i in ids])
        conn.commit()
        conn.close()
    return jsonify({"status": "marked"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)