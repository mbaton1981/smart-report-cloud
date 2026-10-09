from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
import sqlite3
import os

app = FastAPI()

DB_FILE = "smart_report.db"

def init_cloud_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shifts_cloud (
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
            salary_rate REAL DEFAULT 0.0,
            pin TEXT DEFAULT '0000'
        )
    ''')
    conn.commit()
    conn.close()

init_cloud_db()

@app.get("/", response_class=HTMLResponse)
def shift_form():
    return """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Ввод смены — Bygger och renoverar</title>
        <style>
            body { font-family: 'Segoe UI', Arial, sans-serif; background-color: #1e2229; color: #e2e8f0; padding: 20px; margin: 0; }
            .container { max-width: 400px; margin: 0 auto; background: #242933; padding: 20px; border-radius: 10px; border: 1px solid #323946; }
            h2 { color: #63b3ed; text-align: center; font-size: 18px; margin-bottom: 20px; }
            label { display: block; margin-top: 10px; font-size: 13px; color: #a0aec0; }
            input, select, textarea { width: 100%; padding: 10px; margin-top: 5px; background: #28303d; border: 1px solid #3f4c60; color: #fff; border-radius: 6px; box-sizing: border-box; font-size: 14px; }
            button { width: 100%; margin-top: 20px; background: #4299e1; color: white; border: none; padding: 12px; border-radius: 6px; font-weight: bold; font-size: 15px; cursor: pointer; }
            button:hover { background: #3182ce; }
            button:disabled { background: #4a5568; cursor: not-allowed; }
            .pin-container { background: #1a1f26; padding: 10px; border-radius: 6px; margin-top: 5px; border: 1px solid #4a5568; display: none; }
            .error-msg { color: #e53e3e; font-size: 12px; margin-top: 5px; display: none; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>Bygger och renoverar i Sthlm</h2>
            <form action="/submit" method="post" id="shiftForm">
                <label>Дата смены:</label>
                <input type="date" name="date" required id="dateInput">
                
                <label>Сотрудник:</label>
                <select name="employee" id="empSelect" required onchange="onEmployeeChanged()">
                    <option value="">Загрузка списка...</option>
                </select>

                <div id="pinBlock" class="pin-container">
                    <label style="margin-top:0;">Введите PIN-код сотрудника:</label>
                    <input type="password" id="pinInput" maxlength="4" placeholder="••••" oninput="checkPin()">
                    <div id="pinError" class="error-msg">Неверный PIN-код</div>
                </div>
                
                <label>Фирма (Заказчик):</label>
                <input type="text" name="company" placeholder="Например: Renatur / Privat" required>
                
                <label>Объект / Адрес:</label>
                <input type="text" name="object_name" placeholder="Название объекта" required>
                
                <label>Отработано часов:</label>
                <input type="number" step="0.5" name="hours" value="8.0" required>
                
                <label>Ставка (kr/ч):</label>
                <input type="number" step="1" name="rate" placeholder="520" required>
                
                <label>Транспорт (kr):</label>
                <input type="number" step="1" name="transport" value="0">
                
                <label>Описание выполненных работ:</label>
                <textarea name="comment" rows="3" placeholder="Что было сделано за смену..."></textarea>
                
                <button type="submit" id="submitBtn" disabled>Введите PIN-код</button>
            </form>
        </div>

        <script>
            let employeesData = [];

            // Установка текущей даты по умолчанию
            document.getElementById('dateInput').valueAsDate = new Date();

            // Загружаем список сотрудников с сервера при открытии страницы
            fetch('/get-meta')
                .then(res => res.json())
                .then(data => {
                    employeesData = data.employees || [];
                    const select = document.getElementById('empSelect');
                    select.innerHTML = '<option value="">-- Выберите сотрудника --</option>';
                    employeesData.forEach(emp => {
                        const opt = document.createElement('option');
                        // На всякий случай обрабатываем, если имя пришло объектом или строкой
                        let empName = (typeof emp === 'object' && emp !== null) ? (emp.name || '') : String(emp);
                        if (empName.startsWith('{')) {
                            try {
                                let parsed = eval('(' + empName + ')');
                                empName = parsed.name || empName;
                            } catch(e) {}
                        }
                        opt.value = empName;
                        opt.textContent = empName;
                        select.appendChild(opt);
                    });
                })
                .catch(err => {
                    console.error('Ошибка загрузки сотрудников:', err);
                });

            function onEmployeeChanged() {
                const empName = document.getElementById('empSelect').value;
                const pinBlock = document.getElementById('pinBlock');
                const submitBtn = document.getElementById('submitBtn');
                
                if (!empName) {
                    pinBlock.style.display = 'none';
                    submitBtn.disabled = true;
                    submitBtn.textContent = 'Выберите сотрудника';
                    return;
                }

                pinBlock.style.display = 'block';
                document.getElementById('pinInput').value = '';
                document.getElementById('pinError').style.display = 'none';
                submitBtn.disabled = true;
                submitBtn.textContent = 'Введите PIN-код';
            }

            function checkPin() {
                const empName = document.getElementById('empSelect').value;
                const enteredPin = document.getElementById('pinInput').value;
                const submitBtn = document.getElementById('submitBtn');
                const pinError = document.getElementById('pinError');

                const currentEmp = employeesData.find(e => {
                    let name = (typeof e === 'object' && e !== null) ? e.name : String(e);
                    return name === empName;
                });
                
                if (!currentEmp) return;

                const correctPin = (typeof currentEmp === 'object' && currentEmp.pin) ? currentEmp.pin : '0000';

                if (enteredPin === correctPin) {
                    pinError.style.display = 'none';
                    submitBtn.disabled = false;
                    submitBtn.textContent = 'Отправить смену';
                } else {
                    if (enteredPin.length >= 4) {
                        pinError.style.display = 'block';
                    } else {
                        pinError.style.display = 'none';
                    }
                    submitBtn.disabled = true;
                    submitBtn.textContent = 'Неверный PIN-код';
                }
            }
        </script>
    </body>
    </html>
    """

@app.post("/sync-desktop-data")
def sync_desktop_data(data: dict):
    """Принимает актуальные справочники и смены с десктопного приложения"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    employees = data.get("employees", [])
    for emp in employees:
        if isinstance(emp, dict):
            name = emp.get("name")
            rate = emp.get("salary_rate", 0.0)
            pin = emp.get("pin", "0000")
        else:
            name = str(emp)
            rate = 0.0
            pin = "0000"
            
        if name:
            cursor.execute('''
                INSERT INTO meta_employees (name, salary_rate, pin) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET salary_rate = ?, pin = ?
            ''', (name, rate, pin, rate, pin))
        
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.get("/get-meta")
def get_meta():
    """Отдает список сотрудников с пин-кодами для веб-формы"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT name, salary_rate, pin FROM meta_employees ORDER BY name")
    rows = cursor.fetchall()
    employees = [dict(r) for r in rows]
    conn.close()
    return {"employees": employees}

@app.post("/submit", response_class=HTMLResponse)
def submit_shift(
    date: str = Form(...),
    employee: str = Form(...),
    company: str = Form(...),
    object_name: str = Form(...),
    hours: float = Form(...),
    rate: float = Form(...),
    transport: float = Form(0.0),
    comment: str = Form("")
):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO shifts_cloud (date, employee, company, object_name, hours, rate, transport, comment, synced)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
    ''', (date, employee, company, object_name, hours, rate, transport, comment))
    conn.commit()
    conn.close()

    return """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8"><title>Успешно</title>
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
            <p>Данные записаны и скоро попадут в общую базу.</p>
            <a href="/">← Отправить еще одну смену</a>
        </div>
    </body>
    </html>
    """

@app.get("/get-unsynced")
def get_unsynced_shifts():
    """Отдает на ПК все несмещенные смены"""
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
    """Помечает смены как синхронизированные"""
    shift_ids = data.get("ids", [])
    if not shift_ids:
        return {"status": "ok"}
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.executemany("UPDATE shifts_cloud SET synced = 1 WHERE id = ?", [(sid,) for sid in shift_ids])
    conn.commit()
    conn.close()
    return {"status": "success", "synced_count": len(shift_ids)}