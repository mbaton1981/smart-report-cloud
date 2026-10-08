from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
import sqlite3
import os

app = FastAPI()

DB_FILE = "smart_report.db"

def init_cloud_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Таблица для смен
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shifts_cloud (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            employee TEXT NOT NULL,
            company TEXT NOT NULL,
            object_name TEXT NOT NULL,
            hours REAL NOT NULL,
            transport INTEGER DEFAULT 0,
            comment TEXT,
            synced INTEGER DEFAULT 0
        )
    ''')
    # Таблица для справочников (сотрудники, объекты, компании), прилетающих с ПК
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_cloud_db()

@app.get("/", response_class=HTMLResponse)
def shift_form():
    # Читаем справочники из базы сервера
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Достаем списки (ожидаем JSON-строки из ПК, либо пустые списки)
    import json
    def get_list(key):
        cursor.execute("SELECT value FROM metadata WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row and row[0]:
            try: return json.loads(row[0])
            except: return []
        return []

    employees = get_list("employees") or ["Aliaksei Patonich"]
    companies = get_list("companies") or ["Bygger och renoverar"]
    objects = get_list("objects") or ["Основной объект"]
    
    conn.close()

    # Формируем HTML с выпадающими списками
    emp_options = "".join([f'<option value="{e}">{e}</option>' for e in employees])
    comp_options = "".join([f'<option value="{c}">{c}</option>' for c in companies])
    obj_options = "".join([f'<option value="{o}">{o}</option>' for o in objects])

    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Ввод смены — Bygger och renoverar</title>
        <style>
            body {{ font-family: 'Segoe UI', Arial, sans-serif; background-color: #1e2229; color: #e2e8f0; padding: 20px; margin: 0; }}
            .container {{ max-width: 400px; margin: 0 auto; background: #242933; padding: 20px; border-radius: 10px; border: 1px solid #323946; }}
            h2 {{ color: #63b3ed; text-align: center; font-size: 18px; margin-bottom: 20px; }}
            label {{ display: block; margin-top: 10px; font-size: 13px; color: #a0aec0; }}
            input, select, textarea {{ width: 100%; padding: 10px; margin-top: 5px; background: #28303d; border: 1px solid #3f4c60; color: #fff; border-radius: 6px; box-sizing: border-box; font-size: 14px; }}
            select {{ cursor: pointer; }}
            button {{ width: 100%; margin-top: 20px; background: #4299e1; color: white; border: none; padding: 12px; border-radius: 6px; font-weight: bold; font-size: 15px; cursor: pointer; }}
            button:hover {{ background: #3182ce; }}
            .checkbox-group {{ display: flex; align-items: center; margin-top: 10px; }}
            .checkbox-group input {{ width: 20px; height: 20px; margin-right: 10px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h2>Bygger och renoverar i Sthlm</h2>
            <form action="/submit" method="post">
                <label>Дата смены:</label>
                <input type="date" name="date" required>
                
                <label>Сотрудник:</label>
                <select name="employee" required>
                    {emp_options}
                </select>
                
                <label>Фирма (Заказчик):</label>
                <select name="company" required>
                    {comp_options}
                </select>
                
                <label>Объект / Адрес:</label>
                <select name="object_name" required>
                    {obj_options}
                </select>
                
                <label>Отработано часов:</label>
                <input type="number" step="0.5" name="hours" value="8.0" required>
                
                <div class="checkbox-group">
                    <input type="checkbox" id="transport" name="transport" value="1">
                    <label for="transport" style="margin-top: 0; color: #fff; cursor: pointer;">Транспорт (учитывать поезду)</label>
                </div>
                
                <label>Описание выполненных работ:</label>
                <textarea name="comment" rows="3" placeholder="Что было сделано за смену..."></textarea>
                
                <button type="submit">Отправить смену</button>
            </form>
        </div>
    </body>
    </html>
    """

@app.post("/submit", response_class=HTMLResponse)
def submit_shift(
    date: str = Form(...),
    employee: str = Form(...),
    company: str = Form(...),
    object_name: str = Form(...),
    hours: float = Form(...),
    transport: int = Form(0),
    comment: str = Form("")
):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO shifts_cloud (date, employee, company, object_name, hours, transport, comment, synced)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
    ''', (date, employee, company, object_name, hours, transport, comment))
    conn.commit()
    conn.close()

    return """
    <!DOCTYPE html>
    <html lang="ru">
    <head><meta charset="UTF-8"><title>Успешно</title>
    <style>
        body { font-family: Arial; background-color: #1e2229; color: #fff; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .box { background: #242933; padding: 30px; border-radius: 10px; border: 1px solid #323946; text-align: center; }
        h2 { color: #38a169; }
        a { display: inline-block; margin-top: 15px; color: #4299e1; text-decoration: none; font-weight: bold; }
    </style>
    </head>
    <body>
        <div class="box">
            <h2>✅ Смена успешно отправлена!</h2>
            <a href="/">← Отправить еще одну смену</a>
        </div>
    </body>
    </html>
    """

@app.get("/get-unsynced")
def get_unsynced_shifts():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shifts_cloud WHERE synced = 0")
    rows = cursor.fetchall()
    shifts_list = [dict(row) for row in rows]
    conn.close()
    return JSONResponse(content={"shifts": shifts_list})

@app.post("/mark-synced")
def mark_shifts_synced(data: dict):
    shift_ids = data.get("ids", [])
    if not shift_ids:
        return {"status": "ok"}
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.executemany("UPDATE shifts_cloud SET synced = 1 WHERE id = ?", [(sid,) for sid in shift_ids])
    conn.commit()
    conn.close()
    return {"status": "success", "synced_count": len(shift_ids)}

@app.post("/update-metadata")
def update_metadata(data: dict):
    """Принимает с ПК актуальные списки объектов, сотрудников и компаний"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    import json
    for key, val in data.items():
        cursor.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", (key, json.dumps(val)))
    conn.commit()
    conn.close()
    return {"status": "success"}