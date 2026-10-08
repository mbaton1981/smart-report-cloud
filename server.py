import os
import sqlite3
from flask import Flask, request, jsonify, render_template
from datetime import datetime, timedelta

app = Flask(__name__)

DB_FILE = "smart_report.db"

def get_db_connection():
    # Убираем PRAGMA отсюда, чтобы соединения не блокировали друг друга при чтении
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = sqlite3.connect(DB_FILE)
    # Включаем WAL режим один раз при инициализации базы данных
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    
    # Таблица для смен в облаке
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            employee TEXT NOT NULL,
            company TEXT NOT NULL,
            object_name TEXT NOT NULL,
            hours REAL NOT NULL,
            rate REAL DEFAULT 0.0,
            transport REAL DEFAULT 0.0,
            comment TEXT,
            synced INTEGER DEFAULT 0
        )
    ''')
    
    # Справочники для выпадающих списков
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meta_employees (
            name TEXT PRIMARY KEY
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meta_companies (
            name TEXT PRIMARY KEY
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meta_objects (
            name TEXT PRIMARY KEY
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

@app.route('/')
def index():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM meta_employees ORDER BY name")
    employees = [row['name'] for row in cursor.fetchall()]
    
    cursor.execute("SELECT name FROM meta_companies ORDER BY name")
    companies = [row['name'] for row in cursor.fetchall()]
    
    cursor.execute("SELECT name FROM meta_objects ORDER BY name")
    objects = [row['name'] for row in cursor.fetchall()]
    
    conn.close()
    
    if not employees:
        employees = ["Aliaksei Patonich"]
    if not companies:
        companies = ["Privat", "SBT", "Dvaliks", "Renatur"]
    if not objects:
        objects = ["Badbacken 2", "Teknologgatan 7", "Koksgaatan 40"]

    return render_template('index.html', employees=employees, companies=companies, objects=objects)

@app.route('/submit-shift', methods=['POST'])
def submit_shift():
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"status": "error", "message": "Неверный формат данных (нет JSON)!"}), 400
        
        date = data.get('date')
        employee = data.get('employee')
        company = data.get('company')
        object_name = data.get('object_name')
        hours = data.get('hours', 8.0)
        transport = data.get('transport', 0.0)
        comment = data.get('comment', '')
        
        if not date or not employee or not company or not object_name:
            return jsonify({"status": "error", "message": "Заполните все обязательные поля!"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO shifts (date, employee, company, object_name, hours, transport, comment, synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        ''', (date, employee, company, object_name, hours, transport, comment))
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success", "message": "Смена успешно отправлена!"})
    except Exception as e:
        print(f"ERROR in submit_shift: {str(e)}")
        return jsonify({"status": "error", "message": f"Ошибка сервера: {str(e)}"}), 500

@app.route('/get-unsynced', methods=['GET'])
def get_unsynced():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM shifts WHERE synced = 0")
        rows = cursor.fetchall()
        conn.close()
        
        shifts = []
        for row in rows:
            shifts.append({
                "id": row["id"],
                "date": row["date"],
                "employee": row["employee"],
                "company": row["company"],
                "object_name": row["object_name"],
                "hours": row["hours"],
                "rate": row["rate"],
                "transport": row["transport"],
                "comment": row["comment"]
            })
            
        return jsonify({"shifts": shifts})
    except Exception as e:
        print(f"ERROR in get_unsynced: {str(e)}")
        return jsonify({"shifts": [], "error": str(e)}), 500

@app.route('/mark-synced', methods=['POST'])
def mark_synced():
    try:
        data = request.get_json(silent=True) or {}
        ids = data.get("ids", [])
        
        if not ids:
            return jsonify({"status": "ok"})
            
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.executemany("UPDATE shifts SET synced = 1 WHERE id = ?", [(i,) for i in ids])
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success"})
    except Exception as e:
        print(f"ERROR in mark_synced: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/sync-desktop-data', methods=['POST'])
def sync_desktop_data():
    try:
        data = request.get_json(silent=True) or {}
        employees = data.get("employees", [])
        companies = data.get("companies", [])
        objects = data.get("objects", [])
        shifts = data.get("shifts", [])
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM meta_employees")
        for emp in employees:
            cursor.execute("INSERT OR IGNORE INTO meta_employees (name) VALUES (?)", (emp,))
            
        cursor.execute("DELETE FROM meta_companies")
        for comp in companies:
            cursor.execute("INSERT OR IGNORE INTO meta_companies (name) VALUES (?)", (comp,))
            
        cursor.execute("DELETE FROM meta_objects")
        for obj in objects:
            cursor.execute("INSERT OR IGNORE INTO meta_objects (name) VALUES (?)", (obj,))
            
        for s in shifts:
            cursor.execute('''
                SELECT id FROM shifts 
                WHERE date = ? AND employee = ? AND object_name = ? AND hours = ?
            ''', (s['date'], s['employee'], s['object_name'], s['hours']))
            
            if not cursor.fetchone():
                cursor.execute('''
                    INSERT INTO shifts (date, employee, company, object_name, hours, rate, transport, comment, synced)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ''', (
                    s['date'], s['employee'], s['company'], s['object_name'], 
                    s['hours'], s.get('rate', 0.0), s.get('transport', 0.0), s.get('comment', ''), 1
                ))
                
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success"})
    except Exception as e:
        print(f"ERROR in sync_desktop_data: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/update-metadata', methods=['POST'])
def update_metadata():
    try:
        data = request.get_json(silent=True) or {}
        employees = data.get("employees", [])
        companies = data.get("companies", [])
        objects = data.get("objects", [])
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM meta_employees")
        for emp in employees:
            cursor.execute("INSERT OR IGNORE INTO meta_employees (name) VALUES (?)", (emp,))
            
        cursor.execute("DELETE FROM meta_companies")
        for comp in companies:
            cursor.execute("INSERT OR IGNORE INTO meta_companies (name) VALUES (?)", (comp,))
            
        cursor.execute("DELETE FROM meta_objects")
        for obj in objects:
            cursor.execute("INSERT OR IGNORE INTO meta_objects (name) VALUES (?)", (obj,))
            
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success"})
    except Exception as e:
        print(f"ERROR in update_metadata: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/check-employee-shifts', methods=['POST'])
def check_employee_shifts():
    try:
        data = request.get_json(silent=True) or {}
        employee_name = data.get('employee')
        
        if not employee_name:
            return jsonify({"warning": "", "recent_shifts": []})
            
        conn = get_db_connection()
        cursor = conn.cursor()
        
        today = datetime.now().date()
        start_date = today - timedelta(days=14)
        
        cursor.execute('''
            SELECT date, object_name, hours FROM shifts 
            WHERE employee = ? 
            ORDER BY date DESC, id DESC 
            LIMIT 10
        ''', (employee_name,))
        recent_rows = cursor.fetchall()
        
        recent_shifts = []
        for r in recent_rows:
            recent_shifts.append({
                "date": r["date"],
                "object_name": r["object_name"],
                "hours": r["hours"]
            })

        cursor.execute('''
            SELECT date FROM shifts 
            WHERE employee = ? AND date >= ? AND date <= ?
        ''', (employee_name, start_date.isoformat(), today.isoformat()))
        
        worked_dates = {row[0] for row in cursor.fetchall()}
        conn.close()
        
        missing_dates = []
        current = start_date
        while current <= today:
            if current.weekday() != 6:  # Исключая воскресенья
                d_str = current.isoformat()
                if d_str not in worked_dates:
                    missing_dates.append(d_str)
            current += timedelta(days=1)
            
        if missing_dates:
            msg = f"⚠️ Внимание, {employee_name}! У вас есть незаполненные смены за последние 2 недели (пропущено дней: {len(missing_dates)}). Пожалуйста, проверьте и внесите часы."
        else:
            msg = ""
            
        return jsonify({"warning": msg, "missing_count": len(missing_dates), "recent_shifts": recent_shifts})
    except Exception as e:
        print(f"ERROR in check_employee_shifts: {str(e)}")
        return jsonify({"warning": "", "recent_shifts": [], "error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
   
