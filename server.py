import sqlite3
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
DB_FILE = "cloud_database.db"

def init_cloud_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Таблица для смен из веб-формы
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
    
    # Таблицы для хранения справочников (чтобы они не сбрасывались)
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
    
    # Дефолтный сотрудник на случай пустой базы
    cursor.execute("SELECT COUNT(*) FROM meta_employees")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT OR IGNORE INTO meta_employees (name, salary_rate) VALUES (?, ?)", ("Aliaksei Patonich", 0.0))
        cursor.execute("INSERT OR IGNORE INTO meta_companies (name) VALUES (?)", ("Privat",))
        cursor.execute("INSERT OR IGNORE INTO meta_objects (name, markning, company) VALUES (?, ?, ?)", ("Badbacken 2", "p1010", "Privat"))

    conn.commit()
    conn.close()

init_cloud_db()

@app.route('/')
def index():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Загружаем актуальные справочники из базы сервера
    cursor.execute("SELECT name FROM meta_employees ORDER BY name")
    emp_rows = cursor.fetchall()
    employees = [row[0] for row in emp_rows] if emp_rows else ["Aliaksei Patonich"]

    cursor.execute("SELECT name FROM meta_companies ORDER BY name")
    comp_rows = cursor.fetchall()
    companies = [row[0] for row in comp_rows] if comp_rows else ["Privat"]

    cursor.execute("SELECT name, markning, company FROM meta_objects ORDER BY company, name")
    obj_rows = cursor.fetchall()
    objects = []
    for r in obj_rows:
        objects.append({"name": r[0], "markning": r[1] or "", "company": r[2]})

    conn.close()
    return render_template('index.html', employees=employees, companies=companies, objects=objects)

@app.route('/submit-shift', methods=['POST'])
def submit_shift():
    data = request.json
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO cloud_shifts (date, employee, company, object_name, hours, rate, transport, comment, synced)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
    ''', (
        data.get('date'),
        data.get('employee'),
        data.get('company'),
        data.get('object_name'),
        data.get('hours', 0.0),
        data.get('rate', 0.0),
        data.get('transport', 0.0),
        data.get('comment', ''),
    ))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/sync-desktop-data', methods=['POST'])
def sync_desktop_data():
    """Принимает полные справочники и смены с десктопного приложения"""
    data = request.json
    employees = data.get("employees", [])
    companies = data.get("companies", [])
    objects = data.get("objects", []) # Список строк объектов или словарей
    desktop_shifts = data.get("shifts", [])

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # 1. Обновляем справочник сотрудников
    for emp in employees:
        if emp:
            cursor.execute("INSERT OR IGNORE INTO meta_employees (name) VALUES (?)", (emp,))

    # 2. Обновляем справочник фирм
    for comp in companies:
        if comp:
            cursor.execute("INSERT OR IGNORE INTO meta_companies (name) VALUES (?)", (comp,))

    # 3. Обновляем справочник объектов (если передаются строками или структурой)
    for obj in objects:
        if isinstance(obj, str) and obj:
            cursor.execute("INSERT OR IGNORE INTO meta_objects (name, company) VALUES (?, ?)", (obj, "Privat"))
        elif isinstance(obj, dict) and obj.get("name"):
            cursor.execute('''
                INSERT OR IGNORE INTO meta_objects (name, markning, company) VALUES (?, ?, ?)
            ''', (obj.get("name"), obj.get("markning", ""), obj.get("company", "Privat")))

    # 4. Сохраняем смены с десктопа в историю, если их там еще нет
    for s in desktop_shifts:
        cursor.execute('''
            SELECT id FROM cloud_shifts 
            WHERE date = ? AND employee = ? AND object_name = ? AND hours = ?
        ''', (s['date'], s['employee'], s['object_name'], s['hours']))
        if not cursor.fetchone():
            cursor.execute('''
                INSERT INTO cloud_shifts (date, employee, company, object_name, hours, rate, transport, comment, synced)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ''', (
                s['date'], s['employee'], s['company'], s['object_name'],
                s['hours'], s.get('rate', 0.0), s.get('transport', 0.0), s.get('comment', ''), 1
            ))

    conn.commit()
    conn.close()
    return jsonify({"status": "synced"})

@app.route('/get-unsynced', methods=['GET'])
def get_unsynced():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, date, employee, company, object_name, hours, rate, transport, comment FROM cloud_shifts WHERE synced = 0")
    rows = cursor.fetchall()
    conn.close()
    
    shifts = []
    for r in rows:
        shifts.append({
            "id": r[0], "date": r[1], "employee": r[2], "company": r[3],
            "object_name": r[4], "hours": r[5], "rate": r[6], "transport": r[7], "comment": r[8]
        })
    return jsonify({"shifts": shifts})

@app.route('/mark-synced', methods=['POST'])
def mark_synced():
    data = request.json
    ids = data.get("ids", [])
    if ids:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.executemany("UPDATE cloud_shifts SET synced = 1 WHERE id = ?", [(i,) for i in ids])
        conn.commit()
        conn.close()
    return jsonify({"status": "marked"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)