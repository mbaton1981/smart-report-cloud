import sqlite3
import time
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
DB_FILE = "cloud_database.db"

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.row_factory = sqlite3.Row
    return conn

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
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM meta_employees ORDER BY name")
    emp_rows = cursor.fetchall()
    employees = [row['name'] for row in emp_rows] if emp_rows else ["Aliaksei Patonich"]

    cursor.execute("SELECT name FROM meta_companies ORDER BY name")
    comp_rows = cursor.fetchall()
    companies = [row['name'] for row in comp_rows] if comp_rows else ["Privat"]

    cursor.execute("SELECT name, markning, company FROM meta_objects ORDER BY company, name")
    obj_rows = cursor.fetchall()
    objects = []
    for r in obj_rows:
        # Формируем аккуратное отображение для объектов, исключая «тарабарщину»
        mark = f" ({r['markning']})" if r['markning'] else ""
        comp = f" [{r['company']}]" if r['company'] and r['company'] != 'Privat' else ""
        display_str = f"{r['name']}{mark}{comp}"
        objects.append({"name": r['name'], "markning": r['markning'] or "", "company": r['company'], "display": display_str})

    conn.close()
    return render_template('index.html', employees=employees, companies=companies, objects=objects)

@app.route('/submit-shift', methods=['POST'])
def submit_shift():
    data = request.json
    for attempt in range(5):
        try:
            conn = get_db_connection()
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
            return jsonify({"status": "success", "message": "Смена успешно отправлена!"})
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < 4:
                time.sleep(0.5)
                continue
            raise e

@app.route('/check-employee-shifts', methods=['POST'])
def check_employee_shifts():
    data = request.json
    emp = data.get('employee')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT date, object_name, hours FROM cloud_shifts WHERE employee = ? ORDER BY date DESC LIMIT 5", (emp,))
    rows = cursor.fetchall()
    conn.close()
    
    shifts = []
    for r in rows:
        shifts.append({"date": r['date'], "object_name": r['object_name'], "hours": r['hours']})
    return jsonify({"recent_shifts": shifts})

@app.route('/sync-desktop-data', methods=['POST'])
def sync_desktop_data():
    data = request.json
    employees = data.get("employees", [])
    companies = data.get("companies", [])
    objects = data.get("objects", [])
    desktop_shifts = data.get("shifts", [])

    for attempt in range(5):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # Синхронизация сотрудников (включая удаление тех, кого нет в списке с ПК)
            if employees:
                placeholders = ','.join(['?'] * len(employees))
                cursor.execute(f"DELETE FROM meta_employees WHERE name NOT IN ({placeholders})", employees)
                for emp in employees:
                    if emp:
                        cursor.execute("INSERT OR IGNORE INTO meta_employees (name) VALUES (?)", (emp,))

            # Синхронизация компаний (включая удаление)
            if companies:
                placeholders = ','.join(['?'] * len(companies))
                cursor.execute(f"DELETE FROM meta_companies WHERE name NOT IN ({placeholders})", companies)
                for comp in companies:
                    if comp:
                        cursor.execute("INSERT OR IGNORE INTO meta_companies (name) VALUES (?)", (comp,))

            # Синхронизация объектов
            cursor.execute("DELETE FROM meta_objects")
            for obj in objects:
                if isinstance(obj, str) and obj:
                    cursor.execute("INSERT OR IGNORE INTO meta_objects (name, company) VALUES (?, ?)", (obj, "Privat"))
                elif isinstance(obj, dict) and obj.get("name"):
                    cursor.execute('''
                        INSERT OR IGNORE INTO meta_objects (name, markning, company) VALUES (?, ?, ?)
                    ''', (obj.get("name"), obj.get("markning", ""), obj.get("company", "Privat")))

            # Синхронизация смен
            for s in desktop_shifts:
                cursor.execute('''
                    SELECT id FROM cloud_shifts 
                    WHERE date = ? AND employee = ? AND object_name = ? AND hours = ?
                ''', (s['date'], s['employee'], s['object_name'], s['hours']))
                if not cursor.fetchone():
                    cursor.execute('''
                        INSERT INTO cloud_shifts (date, employee, company, object_name, hours, rate, transport, comment, synced)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        s['date'], 
                        s['employee'], 
                        s['company'], 
                        s['object_name'],
                        s['hours'], 
                        s.get('rate', 0.0), 
                        s.get('transport', 0.0), 
                        s.get('comment', ''), 
                        1
                    ))

            conn.commit()
            conn.close()
            return jsonify({"status": "synced"})
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < 4:
                time.sleep(0.5)
                continue
            raise e

@app.route('/get-unsynced', methods=['GET'])
def get_unsynced():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, date, employee, company, object_name, hours, rate, transport, comment FROM cloud_shifts WHERE synced = 0")
    rows = cursor.fetchall()
    conn.close()
    
    shifts = []
    for r in rows:
        shifts.append({
            "id": r['id'], "date": r['date'], "employee": r['employee'], "company": r['company'],
            "object_name": r['object_name'], "hours": r['hours'], "rate": r['rate'], "transport": r['transport'], "comment": r['comment']
        })
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