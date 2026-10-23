"""Online authorization and encrypted cache in the existing employee database."""
import sqlite3
from urllib.parse import urlsplit
import uuid
import requests

import desktop_auth
from employee_cards import CardError, CardConflict, ensure_schema


class CardClient:
    def __init__(self, connect=None):
        self.connect = connect

    def request(self, method, path="", payload=None):
        url = desktop_auth.server_url()
        parsed = urlsplit(url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}):
            raise CardError("Для карточек требуется HTTPS (локальный HTTP разрешён только для проверки).")
        token = desktop_auth.current_token()
        if not token:
            raise CardError("Для карточек выполните личный вход администратора или бухгалтера. Общего ключа sync недостаточно.")
        headers = {"Authorization": f"Bearer {token}"}
        dataset = None
        if self.connect:
            from sync_dataset import dataset_id, HEADER
            conn = self.connect()
            try:
                dataset = dataset_id(conn)
                if dataset:
                    headers[HEADER] = dataset
            finally:
                conn.close()
        else:
            from sync_dataset import file_dataset, HEADER
            import os
            database = os.environ.get("SMARTREPORT_DB_FILE")
            if database:
                dataset = file_dataset(database)
                if dataset:
                    headers[HEADER] = dataset
        try:
            response = requests.request(method, url + "/api/employee-cards" + path, headers=headers,
                                        json=payload, timeout=20, allow_redirects=False)
        except requests.RequestException:
            raise CardError("Не удалось связаться с защищённым разделом. Данные не сохранены локально; повторите подключение.") from None
        if response.status_code in (404, 405):
            raise CardError("Карточки недоступны: требуется согласованное обновление сервера. Обычный учёт работает.")
        if response.status_code in (401, 403):
            raise CardError("Доступ к карточкам запрещён. Требуется действующий личный вход администратора или бухгалтера.")
        if response.status_code == 409:
            raise CardConflict("Обнаружен конфликт версии или набора данных. Ничего не заменено; повторно откройте карточку.")
        if response.status_code == 503:
            raise CardError("Карточки временно недоступны. Проверьте сервер и отдельный ключ защищённого хранения.")
        if response.status_code not in (200, 201):
            raise CardError("Сервер не принял операцию с карточкой. Проверьте поля и повторно откройте её.")
        from sync_dataset import require_response_dataset
        try:
            require_response_dataset(response, dataset)
        except RuntimeError:
            raise CardConflict("Сервер относится к другому набору данных. Локальный кэш не изменён.") from None
        try:
            data = response.json()
        except ValueError:
            raise CardError("Сервер вернул неверный ответ. Карточка не открыта.") from None
        if data.get("ok") is not True:
            raise CardError("Сервер не подтвердил операцию.")
        return data

    def employees(self):
        return self.request("GET")["employees"]

    def authorize(self):
        self.employees()

    def get(self, server_id):
        return self.request("GET", f"/{server_id}")["employee"]

    def audit(self, server_id):
        return self.request("GET", f"/{server_id}/audit")["changes"]

    def resolve(self, local_id):
        if not self.connect:
            return self.get(local_id)
        conn = self.connect()
        try:
            ensure_schema(conn.cursor(), "employees")
            row = conn.execute("SELECT * FROM employees WHERE id=?", (local_id,)).fetchone()
            if row is None:
                raise CardError("Сотрудник не найден.")
            row = dict(row)
            conn.commit()
        finally:
            conn.close()
        if row["card_server_id"] is None:
            matches = [item for item in self.employees()
                       if " ".join(item["name"].split()).casefold() == " ".join(row["name"].split()).casefold()]
            if len(matches) != 1:
                raise CardError("Нет однозначного соответствия существующему серверному ID. Сначала синхронизируйте справочник.")
            server_id = matches[0]["id"]
        else:
            server_id = row["card_server_id"]
        record = self.get(server_id)
        if record["name"] != row["name"]:
            raise CardError("Идентичность сотрудника изменилась. Автоматическая перепривязка запрещена.")
        self.cache(local_id, record)
        return record

    def cache(self, local_id, record):
        if not self.connect:
            return
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            ensure_schema(conn.cursor(), "employees")
            row = conn.execute("SELECT * FROM employees WHERE id=?", (local_id,)).fetchone()
            if row is None or row["name"] != record["name"] or row["card_server_id"] not in (None, record["id"]):
                raise CardError("Привязка сотрудника изменилась. Карточка не сохранена в локальный кэш.")
            conn.execute("""UPDATE employees SET card_server_id=?,card_cipher=?,card_version=?
                WHERE id=? AND card_version<=?""",
                         (record["id"], record["cipher"], record["revision"], local_id, record["revision"]))
            key = " ".join(record["name"].split()).casefold()
            state = conn.execute("SELECT sync_version FROM employee_sync_state WHERE employee_key=?", (key,)).fetchone()
            pending = conn.execute("""SELECT 1 FROM employee_sync_outbox WHERE employee_key=?
                UNION ALL SELECT 1 FROM employee_restore_outbox WHERE employee_key=? LIMIT 1""", (key, key)).fetchone()
            if not pending and (state is None or state["sync_version"] <= record["employee_revision"]):
                conn.execute("UPDATE employees SET is_active=? WHERE id=?", (int(record["is_active"]), local_id))
                conn.execute("""INSERT INTO employee_sync_state(employee_key,employee_name,state,sync_version,updated_at)
                    VALUES(?,?,?,?,datetime('now')) ON CONFLICT(employee_key) DO UPDATE SET
                    state=excluded.state,sync_version=excluded.sync_version,updated_at=excluded.updated_at""",
                             (key, record["name"], "active" if record["is_active"] else "deleted", record["employee_revision"]))
            conn.commit()
        except (CardError, sqlite3.Error):
            conn.rollback()
            raise CardError("Серверная карточка сохранена, но локальный кэш не обновлён. Откройте её повторно.") from None
        finally:
            conn.close()

    def save(self, record, card, active, local_id=None, operation_id=None):
        if self.connect:
            conn = self.connect()
            try:
                key = " ".join(record["name"].split()).casefold()
                pending = conn.execute("""SELECT 1 FROM employee_sync_outbox WHERE employee_key=?
                    UNION ALL SELECT 1 FROM employee_restore_outbox WHERE employee_key=? LIMIT 1""",
                                       (key, key)).fetchone()
                if pending:
                    raise CardError("Сначала завершите синхронизацию статуса сотрудника.")
            finally:
                conn.close()
        result = self.request("PUT", f"/{record['id']}", {
            "card": card, "is_active": active, "revision": record["revision"],
            "employee_revision": record["employee_revision"], "operation_id": operation_id or str(uuid.uuid4()),
        })["employee"]
        if local_id is not None:
            self.cache(local_id, result)
        return result
