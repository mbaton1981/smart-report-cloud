from datetime import datetime, timedelta

@app.route('/check-employee-shifts', methods=['POST'])
def check_employee_shifts():
    data = request.json
    employee_name = data.get('employee')
    
    if not employee_name:
        return jsonify({"warning": ""})
        
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Считаем диапазон за последние 14 дней до сегодняшнего дня
    today = datetime.now().date()
    start_date = today - timedelta(days=14)
    
    # Получаем все смены сотрудника за этот период
    cursor.execute('''
        SELECT date FROM shifts 
        WHERE employee = ? AND date >= ? AND date <= ?
    ''', (employee_name, start_date.isoformat(), today.isoformat()))
    
    worked_dates = {row[0] for row in cursor.fetchall()}
    conn.close()
    
    # Ищем пропущенные дни (например, рабочие дни без записей)
    missing_dates = []
    current = start_date
    while current <= today:
        # Исключаем воскресенья (или можно убрать эту проверку, если работают и в воскресенье)
        if current.weekday() != 6: 
            d_str = current.isoformat()
            if d_str not in worked_dates:
                missing_dates.append(d_str)
        current += timedelta(days=1)
        
    if missing_dates:
        # Формируем красивое предупреждение
        msg = f"⚠️ Внимание, {employee_name}! У вас есть незаполненные смены за последние 2 недели (всего дней: {len(missing_dates)}). Пожалуйста, проверьте и внесите пропущенные дни."
    else:
        msg = ""
        
    return jsonify({"warning": msg, "missing_count": len(missing_dates)})