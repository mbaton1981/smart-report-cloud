"""Explicit bridges to the current desktop profile; no UI click automation."""
from contextlib import closing
from decimal import Decimal
import sys

from PyQt6.QtCore import QSignalBlocker


class DesktopActionError(ValueError):
    pass


def application(window):
    module = sys.modules.get(window.__class__.__module__)
    if module is None or not hasattr(module, "get_db_connection"):
        raise DesktopActionError("Не определён текущий профиль SmartReport.")
    return module


def local_result(window, result):
    plan = result["plan"]
    app = application(window)
    with closing(app.get_db_connection()) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        if plan["purpose"] == "balance":
            if plan["company"] or plan["object_name"]:
                raise DesktopActionError("Баланс ведётся по сотрудникам, без разреза по объекту. Уберите объект.")
            rows = conn.execute("SELECT date,employee,description,change_amount FROM balance "
                                "WHERE date>=? AND date<=? AND (?='' OR employee=?) ORDER BY date,id",
                                (plan["date_from"], plan["date_to"], plan["employee"], plan["employee"])).fetchall()
            all_rows = conn.execute("SELECT change_amount FROM balance WHERE (?='' OR employee=?)",
                                    (plan["employee"], plan["employee"])).fetchall()
            total = sum((Decimal(str(row[0])) for row in all_rows), Decimal("0"))
            result["table"] = dict(headers=["Дата", "Сотрудник", "Описание", "Изменение, SEK"],
                                   rows=[list(row) for row in rows])
            result["summary"] = f"Операций за выбранный период: {len(rows)}. Текущий баланс за всё время: {total:,.2f} SEK. " \
                                "Положительное значение — долг, отрицательное — выплата; это не расчёт зарплаты."
        elif plan["purpose"] == "expenses":
            if plan["company"] or plan["object_name"] or plan["employee"]:
                raise DesktopActionError("Журнал общих расходов не имеет надёжной связи с сотрудником/объектом. "
                                         "Для распределения по объектам откройте документы расходов.")
            pattern = f"Авторасчет%за период {plan['date_from']} - {plan['date_to']}%"
            rows = conn.execute("SELECT date,category,description,amount FROM company_expenses "
                                "WHERE description LIKE ? OR ((description IS NULL OR description NOT LIKE 'Авторасчет%') "
                                "AND date>=? AND date<=?) ORDER BY date,id",
                                (pattern, plan["date_from"], plan["date_to"])).fetchall()
            result["table"] = dict(headers=["Дата", "Категория", "Описание", "Сумма, SEK"],
                                   rows=[list(row) for row in rows])
            result["summary"] = f"Записей общих расходов: {len(rows)}. Сохранённые автоматические оценки показаны отдельно " \
                                "по своим описаниям; пересчёт налогов/FORA не запускался. Документы поставщиков — отдельный раздел."
        else:
            raise DesktopActionError("Этот локальный просмотр не подключён.")
    result["source"] = "Текущий локальный профиль SmartReport; не серверная копия и не проверка других профилей."
    result.pop("native_action", None)
    return result


def native_action(window, result, parent):
    plan = result["plan"]
    action = result["native_action"]
    app = application(window)
    if action in {"employee_card", "accountant_export"}:
        import employee_cards_ui as cards
        client = cards.CardClient(app.get_db_connection)
        with closing(app.get_db_connection()) as conn:
            matches = conn.execute("SELECT id FROM employees WHERE name=?", (plan["employee"],)).fetchall()
        if len(matches) != 1:
            raise DesktopActionError("Нет однозначного сотрудника в текущем локальном справочнике. Синхронизируйте.")
        local_id = matches[0]["id"]
        record = client.resolve(local_id)
        if action == "employee_card":
            cards.EmployeeCardDialog(client, record, local_id, parent).exec()
        else:
            cards.export_records(client, [record["id"]], parent)
        return "Закрыта штатная защищённая форма. Сохранение или экспорт выполнялись только вашими действиями в ней."
    if action == "payroll_history":
        if plan["company"] and not plan["object_name"]:
            raise DesktopActionError("Для истории зарплаты укажите объект фирмы или уберите фильтр фирмы.")
        window.sal_date_from.setText(plan["date_from"])
        window.sal_date_to.setText(plan["date_to"])
        import payroll_ui
        payroll_ui.open_history(window, app, employee_name=plan["employee"],
                                object_key=(plan["company"], plan["object_name"]) if plan["object_name"] else None)
        return "Открыта штатная история зарплаты. Автоматическое подтверждение ставок и выплат не выполнялось."
    if action == "expense_documents":
        if plan["employee"] or plan["company"] and not plan["object_name"]:
            raise DesktopActionError("Документы расходов поддерживают период и объект, но не фильтр сотрудника "
                                     "или фирмы без объекта. Уберите эти фильтры или укажите объект.")
        window.exp_date_from.setText(plan["date_from"])
        window.exp_date_to.setText(plan["date_to"])
        window.tabs.setCurrentWidget(window.tab_expenses)
        object_id = result["object"]["id"] if result.get("object") else None
        index = window.exp_filter_object.findData(object_id)
        if index < 0:
            raise DesktopActionError("Объект недоступен в серверном фильтре расходов. Обновите справочник.")
        with QSignalBlocker(window.exp_filter_object), QSignalBlocker(window.exp_filter_unallocated):
            window.exp_filter_object.setCurrentIndex(index)
            window.exp_filter_unallocated.setChecked(False)
        window.load_expense_documents()
    elif action == "shift_form":
        window.tabs.setCurrentWidget(window.tab_hours)
    elif action == "directories":
        window.tabs.setCurrentWidget(window.tab_sprav)
    elif action == "invoice_period":
        window.tabs.setCurrentWidget(window.tab_invoice)
        window.inv_date_from.setText(plan["date_from"])
        window.inv_date_to.setText(plan["date_to"])
        with QSignalBlocker(window.inv_comp_cb):
            index = window.inv_comp_cb.findText(plan["company"])
            if index < 0:
                raise DesktopActionError("Нет фирмы в локальном справочнике. Выполните синхронизацию.")
            window.inv_comp_cb.setCurrentIndex(index)
        window.load_invoice_objects(plan["company"])
        with closing(app.get_db_connection()) as conn:
            rows = conn.execute("SELECT id FROM objects WHERE company=? AND name=?",
                                (plan["company"], plan["object_name"])).fetchall()
        if len(rows) != 1:
            raise DesktopActionError("Объект отсутствует или неоднозначен в локальном справочнике.")
        index = window.inv_obj_cb.findData(rows[0]["id"])
        if index < 0:
            raise DesktopActionError("Объект недоступен в текущем фильтре.")
        window.inv_obj_cb.setCurrentIndex(index)
        window.generate_invoice()
    elif action == "incoming_documents":
        from incoming_documents import enabled
        if not enabled():
            raise DesktopActionError("Входящие документы выключены в текущем профиле. Ничего не запущено.")
        window.open_incoming_documents()
    else:
        raise DesktopActionError("Это действие не подключено к штатной форме.")
    window.show()
    window.raise_()
    return "Открыт соответствующий раздел с доступными штатными действиями. Ничего не сохранено, не удалено и не отправлено автоматически."
