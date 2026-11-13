"""Optional director panel; network tasks never block the ordinary desktop workflow."""
import io
import base64
from html import escape
import json
import logging
import math
import os
import sqlite3
import struct
from pathlib import Path
import sys
import time
import uuid
import wave
from decimal import Decimal
from urllib.parse import urlsplit

import requests
from PyQt6.QtCore import QBuffer, QDate, QIODevice, QSignalBlocker, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTextEdit, QVBoxLayout, QLineEdit, QFileDialog,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView, QComboBox, QProgressBar,
    QScrollArea, QWidget, QFrame,
)
from date_widgets import CalendarDateEdit

import desktop_auth
import desktop_theme
from sync_dataset import HEADER, file_dataset, require_response_dataset
from assistant_actions import CAPABILITIES, REPORT_ACTIONS
from assistant_desktop import DesktopActionError, local_result, native_action
import assistant_attention


class AssistantClientError(ValueError):
    pass


class AssistantAuthorizationError(AssistantClientError):
    pass


MAX_REPORT_PULL_CHANGES = 5000


CANDIDATE_READ_ACTIONS = frozenset({
    "search", "hours_summary", "check", "anomalies", "transport_check", "explain",
    "employees", "objects", "payroll_pdf", "customer_pdf", "journal_pdf",
})
CANDIDATE_REQUESTS = frozenset({"status", "capabilities", "command", "inspect", "transcribe"})


def request_api(path, payload=None, audio=None, database=None, audio_terms=None):
    base = desktop_auth.server_url().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}):
        raise AssistantClientError("Помощнику требуется HTTPS.")
    token = desktop_auth.current_token()
    if not token:
        raise AssistantAuthorizationError("Выполните личный вход администратора. Общий ключ синхронизации не подходит.")
    database = database or os.environ.get("SMARTREPORT_DB_FILE")
    if not database:
        raise AssistantClientError("Не определена база текущего окна SmartReport. Закройте и откройте помощника из программы.")
    dataset = file_dataset(database)
    if not dataset:
        raise AssistantClientError("Нет идентификатора набора данных текущей базы. Сначала выполните штатную синхронизацию.")
    headers = {"Authorization": "Bearer " + token, HEADER: dataset}
    request_data = None
    files = None
    if audio is not None:
        headers["X-Operation-ID"] = str(uuid.uuid4())
        if audio_terms is None:
            headers["Content-Type"] = "audio/wav"
            request_data = audio
        else:
            request_data = {"terms": json.dumps(audio_terms, ensure_ascii=False)}
            files = {"file": ("command.wav", audio, "audio/wav")}
    try:
        response = requests.request("POST" if payload is not None or audio is not None else "GET",
                                    base + "/api/assistant/" + path, headers=headers, json=payload,
                                    data=request_data, files=files, timeout=(10, 75), allow_redirects=False)
        if response.status_code in (401, 403):
            raise AssistantAuthorizationError("Нет доступа. Нужен действующий личный вход администратора.")
        require_response_dataset(response, dataset)
        if response.status_code in (404, 405):
            raise AssistantClientError("На сервере ещё нет помощника. Обычная программа работает; требуется обновление сервера.")
        if response.status_code not in (200, 400, 413):
            raise AssistantClientError("Сервер помощника недоступен. Обычная программа продолжает работать.")
        data = response.json()
        if not isinstance(data, dict):
            raise AssistantClientError("Некорректный ответ помощника; ничего не изменено.")
        if data.get("ok") is not True:
            # Server errors are controlled messages; never show raw HTTP/provider content.
            if str(data.get("error", "")).startswith("Помощник выключен."):
                raise AssistantAuthorizationError(data["error"])
            raise AssistantClientError(data.get("error", "Операция не подтверждена."))
        return data
    except (requests.RequestException, RuntimeError):
        raise AssistantClientError("Нет связи или набор данных не совпадает. Автоматического повтора нет.") from None
    except ValueError as error:
        if isinstance(error, AssistantClientError):
            raise
        raise AssistantClientError("Некорректный ответ помощника; ничего не изменено.") from None


class RequestWorker(QThread):
    ready = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, path, payload=None, audio=None, parent=None, database=None, audio_terms=None):
        super().__init__(parent)
        self.path, self.payload, self.audio = path, payload, audio
        self.database = database
        self.audio_terms = audio_terms

    def run(self):
        try:
            self.ready.emit(request_api(
                self.path, self.payload, self.audio, self.database, self.audio_terms,
            ))
        except AssistantClientError as error:
            self.failed.emit(str(error))


class AttentionWorker(QThread):
    ready = pyqtSignal(dict)

    def __init__(self, database, app, text, periods, parent=None, *, hours_only=False):
        super().__init__(parent)
        self.database, self.app, self.text, self.periods = database, app, text, periods
        self.hours_only = hours_only

    def run(self):
        result = {"errors": [], "local": None, "local_available": False, "hours": None, "prices": None,
                  "employee_status": None, "documents": None, "hours_only": self.hours_only}
        try:
            if not self.database:
                raise ValueError("Не определена база текущего профиля.")
            path = Path(self.database)
            if not path.is_file():
                raise ValueError("Локальная база текущего профиля отсутствует.")
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN")
                objects = [dict(row) for row in conn.execute(
                    "SELECT company,name FROM objects WHERE is_active=1")]
                company, obj = assistant_attention.scope_for_text(self.text, objects)
                start = (self.periods["hours"][0] if self.hours_only else
                         min(self.periods["hours"][0], self.periods["prices"][0]))
                end = (self.periods["hours"][1] if self.hours_only else
                       max(self.periods["hours"][1], self.periods["prices"][1]))
                rows = [dict(row) for row in conn.execute(
                    "SELECT * FROM shifts WHERE date>=? AND date<=?", (start, end))]
                segments = {}
                if not self.hours_only:
                    if conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ob_segments'").fetchone():
                        for segment in conn.execute(
                                "SELECT o.* FROM ob_segments o JOIN shifts s ON s.sync_id=o.shift_id "
                                "WHERE s.date>=? AND s.date<=?", (start, end)):
                            segments.setdefault(segment["shift_id"], []).append(dict(segment))
                    else:
                        result["errors"].append("Локальные OB-сегменты отсутствуют; часть цен нельзя проверить.")
                for row in rows:
                    row["entry_type"] = row.get("entry_type") or "work"
                    row["_attention_segments"] = segments.get(row.get("sync_id"), [])
                employees = [dict(row) for row in conn.execute(
                    "SELECT id,name,is_active FROM employees")]
                pending = {row[0] for row in conn.execute(
                    "SELECT DISTINCT sync_id FROM shift_sync_outbox "
                    "WHERE state IN ('pending','blocked','conflict')")}
                result["local"] = dict(rows=rows, employees=employees, pending=list(pending),
                                       company=company, object_name=obj)
                result["local_available"] = True
            finally:
                conn.close()
        except ValueError as error:
            result["errors"].append("Локальные смены и справочники: не удалось проверить: " + str(error))
            self.ready.emit(result)
            return
        except (OSError, sqlite3.Error) as error:
            result["errors"].append("Локальные смены и справочники: не удалось проверить: " + str(error))
            result["local"] = dict(rows=[], employees=[], pending=[], company="", object_name="")
            if any(word in self.text.casefold() for word in ("объект", "фирм", "компан")):
                result["errors"].append("Не удалось определить область по локальному справочнику.")
                self.ready.emit(result)
                return

        company, obj = result["local"]["company"], result["local"]["object_name"]
        start, end = self.periods["hours"]
        try:
            response = request_api("command", {
                "text": f"Кто не внёс часы с {start} по {end}?",
                "operation_id": str(uuid.uuid4()),
            }, database=self.database)
            status = response["result"]
            if not isinstance(status, dict) or not isinstance(status.get("table"), dict):
                raise AssistantClientError("Некорректный ответ о сотрудниках.")
            status_rows = status["table"].get("rows")
            if not isinstance(status_rows, list) or any(
                    not isinstance(row, list) or len(row) < 2
                    or not isinstance(row[0], str) or not isinstance(row[1], str)
                    for row in status_rows):
                raise AssistantClientError("Некорректный список сотрудников.")
            result["employee_status"] = status
        except AssistantAuthorizationError as error:
            result["local"] = None
            result["errors"].append(str(error))
            self.ready.emit(result)
            return
        except (AssistantClientError, KeyError) as error:
            result["errors"].append("Серверные сотрудники: не удалось проверить: " + str(error))

        def search(period):
            response = request_api("inspect", {
                "plan": dict(purpose="search", date_from=period[0], date_to=period[1],
                             employee="", company=company, object_name=obj),
                "operation_id": str(uuid.uuid4()),
            }, database=self.database)
            payload = response["result"]
            if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
                raise AssistantClientError("Некорректный список смен.")
            required = {"id", "date", "employee", "company", "object_name", "entry_type",
                        "hours", "customer_rate", "travel_rate", "trips", "fixed"}
            if any(not isinstance(row, dict) or not required.issubset(row)
                   or not isinstance(row["date"], str) or not row["id"]
                   for row in payload["records"]):
                raise AssistantClientError("Некорректная запись смены.")
            return payload["records"]

        if result["employee_status"] is not None:
            for key in (("hours",) if self.hours_only else ("hours", "prices")):
                if key == "prices" and self.periods["prices"] == self.periods["hours"]:
                    result[key] = result["hours"]
                    continue
                try:
                    result[key] = search(self.periods[key])
                except AssistantAuthorizationError as error:
                    result["local"] = None
                    result["errors"].append(str(error))
                    self.ready.emit(result)
                    return
                except (AssistantClientError, KeyError) as error:
                    result["errors"].append(
                        f"Серверные смены ({key}): не удалось проверить: {error}")
        else:
            result["errors"].append("Серверные смены: не удалось проверить.")
        if self.hours_only:
            self.ready.emit(result)
            return
        try:
            response = self.app.request_expense_api("GET", "/api/expense-documents")
            if not isinstance(response, dict) or not isinstance(response.get("documents"), list) \
                    or any(not isinstance(row, dict) for row in response["documents"]):
                raise ValueError("Некорректный список документов.")
            result["documents"] = response["documents"]
            if len(result["documents"]) >= 1000:
                result["errors"].append("Серверный реестр ограничен 1000 документами; "
                                        "старые документы могли не попасть в проверку.")
        except (requests.RequestException, RuntimeError, ValueError, KeyError, AttributeError) as error:
            result["errors"].append("Серверные документы: не удалось проверить: " + str(error))
        self.ready.emit(result)


def result_text(result):
    plan = result["plan"]
    lines = [f'Период: {plan["date_from"]} — {plan["date_to"]}.',
             "Сотрудник: " + (plan["employee"] or "все сотрудники") + ".",
             f'Найдено записей: {len(result["records"])}.']
    if "table" in result or "native_action" in result:
        lines[2] = CAPABILITIES[plan["purpose"]][0]
        lines.extend([result.get("summary", "Готово к открытию штатной формы." if "native_action" in result
                                 else f'Строк в таблице: {len(result["table"]["rows"])}.'),
                      result["source"], result["notice"]])
        return "\n".join(lines)
    if result.get("summary"):
        lines.append(result["summary"])
    if "transport" in result:
        t = result["transport"]
        lines.extend([t["scope"], f'Проверено: {t["checked"]}; требуют внимания: {t["attention"]}; '
                      f'не проверено (другой вид / Fast pris): {t["excluded"]}.',
                      f'Подытог известных сумм транспорта: {Decimal(t["known_subtotal"]):,.2f} SEK.',
                      "Расхождения перечислены в таблице." if t["attention"] else
                      "В проверенных полях расхождений не найдено." if t["checked"] else
                      "Нет записей, для которых выполнена проверка транспорта.",
                      t["limits"]])
    elif plan["purpose"] == "anomalies":
        checked = result.get("checked_count")
        if checked is not None:
            lines[2] = f"Проверено записей: {checked}."
        lines.append("Искались возможные дубли, смены без клиентской ставки и сохранённые ставки, "
                     "отличающиеся от справочника. "
                     "Проверены только серверные записи выбранного периода и фильтров.")
        lines.append("Требующие внимания записи перечислены в таблице." if result["records"] else
                     "Дублей и проблем со ставками в проверенном охвате не найдено.")
        lines.append("Автоматического удаления и замены цен нет.")
        lines.append(result["notice"])
    else:
        lines.append("Проверены сохранённые ставки, связь фирмы и объекта, время и возможные дубликаты."
                     if plan["purpose"] == "check" else "Показаны сохранённые записи выбранного периода.")
        count = sum(bool(row["warnings"]) for row in result["records"])
        lines.append(f'Записей с предупреждениями: {count}. Подробности — в таблице.')
        known = [row["base_only"] for row in result["records"] if row["base_only"] is not None]
        if known:
            subtotal = sum((Decimal(str(value)) for value in known), Decimal("0"))
            lines.append(f'Подытог известных базовых стоимостей без OB: {subtotal:,.2f} SEK '
                         f'({len(known)} записей; остальные не включены).')
        if plan["purpose"] in {"check", "explain"}:
            lines.append("Это не итог фактуры: OB, Fast pris и условия договора проверяйте в штатном отчёте.")
        lines.append(result["notice"])
    lines.append(result["source"])
    if not result["records"]:
        if plan["purpose"] != "anomalies":
            lines.append("По выбранным фильтрам на сервере записей нет. "
                         "Это не проверка других профилей или несинхронизированных данных.")
    return "\n".join(lines)


def result_table(result):
    if "table" in result:
        table = result["table"]
        return table["headers"], [["Не задано" if value is None else str(value) for value in row] for row in table["rows"]]
    transport = result["plan"]["purpose"] == "transport_check"
    headers = ["Дата", "Сотрудник", "Фирма / объект", "Часы", "Выезды", "Цена выезда, SEK",
               "Транспорт, SEK", "Проверка / замечания"]
    if transport:
        headers.insert(6, "Справочник, SEK/выезд")
    else:
        headers.insert(4, "Ставка, SEK/ч")
        if result["plan"]["purpose"] in {"check", "anomalies"}:
            headers.insert(5, "Ставка справочника, SEK/ч")
    rows = []
    for r in result["records"]:
        cells = [r["date"], r["employee"], f'{r["company"]} / {r["object_name"]}',
                 f'{r["hours"]:g}', f'{r["trips"]:g}',
                 "Не задана" if r["travel_rate"] is None else f'{r["travel_rate"]:g}',
                 "Не определён" if r["travel_only"] is None else f'{r["travel_only"]:,.2f}',
                 "\n".join(r.get("transport_findings", []) if transport else r["warnings"]) or "Без предупреждений"]
        if transport:
            rate = r.get("catalog_travel_rate")
            cells.insert(6, "Не определена" if rate is None else f"{rate:g}")
        else:
            cells.insert(4, "Не задана" if r["customer_rate"] is None else f'{r["customer_rate"]:g}')
            if result["plan"]["purpose"] in {"check", "anomalies"}:
                rate = r.get("catalog_rate")
                cells.insert(5, "Не определена" if rate is None else f"{rate:g}")
        rows.append(cells)
    return headers, rows


def _report_local_selection(cursor, plan):
    filters, values = ["date>=?", "date<=?"], [plan["date_from"], plan["date_to"]]
    for name, value in (("employee", plan["employee"]), ("company", plan["company"]),
                        ("object_name", plan["object_name"])):
        if value:
            filters.append(name + "=?")
            values.append(value)
    from customer_pricing import row_prices
    has_segments = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ob_segments'"
    ).fetchone()
    local = {}
    rows = cursor.execute("SELECT * FROM shifts WHERE " + " AND ".join(filters), values).fetchall()
    for raw in rows:
        row = dict(raw)
        times = cursor.execute(
            "SELECT start_time,end_time,hours FROM ob_segments WHERE shift_id=? "
            "ORDER BY start_time,end_time,hours", (row["sync_id"],)
        ).fetchall() if has_segments else []
        local[row["sync_id"]] = dict(
            hours=row["hours"], prices=row_prices(row), trips=row["transport"],
            entry_type=row["entry_type"], start_time=row.get("start_time"),
            end_time=row.get("end_time"), invoiced=bool(row["invoiced"]),
            intervals=[dict(item) for item in times],
        )
    return local


def _report_pull_changes(app, expected_ids, plan, records):
    if not getattr(app, "ASSISTANT_CANDIDATE", False) or getattr(app, "ISOLATED_CANDIDATE", False):
        raise AssistantClientError("Обновление кэша доступно только подключённому read-only кандидату.")
    token = desktop_auth.current_token()
    dataset = file_dataset(app.DB_FILE)
    if not token or not dataset:
        raise AssistantClientError("Нужен действующий вход администратора и согласованный набор данных.")
    headers = {"Authorization": "Bearer " + token, HEADER: dataset}
    try:
        response = requests.get(
            f"{app.CLOUD_URL.rstrip('/')}/sync/pull",
            headers=headers, params={"since": 0}, timeout=(4, 20), allow_redirects=False,
        )
        require_response_dataset(response, dataset)
        app.raise_for_sync_response(response)
        payload = response.json()
    except AssistantClientError:
        raise
    except (requests.RequestException, RuntimeError, ValueError) as error:
        raise AssistantClientError(
            "Безопасный серверный pull не выполнен; локальный кэш не изменён. " + str(error)
        ) from None
    if (not isinstance(payload, dict) or payload.get("ok") is not True
            or not isinstance(payload.get("changes"), list)
            or len(payload["changes"]) > MAX_REPORT_PULL_CHANGES):
        raise AssistantClientError("Серверный pull имеет неверный формат или слишком большой объём; кэш не изменён.")

    latest = {}
    for item in payload["changes"]:
        if (not isinstance(item, dict) or not isinstance(item.get("sync_id"), str)
                or item["sync_id"] not in expected_ids):
            continue
        try:
            revision = (int(item.get("version")), int(item.get("change_seq")))
        except (TypeError, ValueError):
            raise AssistantClientError("Серверная запись имеет неверную версию; кэш не изменён.")
        previous = latest.get(item["sync_id"])
        if previous is None or revision > previous[0]:
            latest[item["sync_id"]] = (revision, item)

    if set(latest) != set(expected_ids):
        raise AssistantClientError("Сервер не вернул все выбранные записи; локальный кэш не изменён.")
    selected = [latest[identifier][1] for identifier in sorted(expected_ids)]
    if any(item.get("deleted") for item in selected):
        raise AssistantClientError("Выборка изменилась на сервере; повторите запрос. Локальный кэш не изменён.")
    if not isinstance(records, list) or any(not isinstance(record, dict) or not record.get("id") for record in records):
        raise AssistantClientError("Ответ помощника не содержит проверяемый список записей.")
    expected_by_id = {record["id"]: record for record in records}
    for item in selected:
        record = expected_by_id[item["sync_id"]]
        if (not isinstance(item.get("date"), str)
                or item["date"] < plan["date_from"] or item["date"] > plan["date_to"]
                or item.get("company") != plan["company"]
                or item.get("object_name") != plan["object_name"]
                or (plan["employee"] and item.get("employee") != plan["employee"])
                or item.get("date") != record["date"] or item.get("employee") != record["employee"]
                or item.get("company") != record["company"]
                or item.get("object_name") != record["object_name"]):
            raise AssistantClientError("Серверная выборка устарела; локальный кэш не изменён.")
    return selected


def _report_object_catalog_entry(app, result_object):
    if not isinstance(result_object, dict):
        raise AssistantClientError("Для клиентского PDF не определён объект.")
    try:
        payload = app.request_expense_api("GET", "/api/expense-objects?include_inactive=1")
    except (requests.RequestException, RuntimeError, ValueError) as error:
        raise AssistantClientError(
            "Не удалось безопасно получить справочник объектов; локальный кэш не изменён. " + str(error)
        ) from None
    if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("objects"), list):
        raise AssistantClientError("Сервер не вернул корректный справочник объектов; локальный кэш не изменён.")
    try:
        object_id = int(result_object["id"])
    except (KeyError, TypeError, ValueError):
        raise AssistantClientError("Ответ сервера не содержит однозначный объект; локальный кэш не изменён.")
    matches = [
        item for item in payload["objects"]
        if isinstance(item, dict) and item.get("id") == object_id
        and item.get("name") == result_object.get("name")
        and item.get("company") == result_object.get("company")
    ]
    if len(matches) != 1 or not matches[0].get("sync_id"):
        raise AssistantClientError("Объект нельзя однозначно связать со справочником; локальный кэш не изменён.")
    return matches[0]


def _refresh_report_cache(window, app, plan, result, local, remote, object_count):
    missing_ids = set(remote) - set(local)
    if (set(local) - set(remote)
            or any(local[key] != remote[key] for key in set(local) & set(remote))
            or (not missing_ids and local != remote)):
        raise AssistantClientError(
            "Локальные записи отличаются по существующим значениям. Обновление остановлено без изменений."
        )
    if object_count > 1:
        raise AssistantClientError("Локальная связь фирмы и объекта неоднозначна; кэш не изменён.")
    selected = _report_pull_changes(app, missing_ids, plan, result["records"]) if missing_ids else []
    object_entry = None
    if plan["purpose"] in {"customer_pdf", "journal_pdf", "catalog_preview"} and object_count == 0:
        object_entry = _report_object_catalog_entry(app, result.get("object"))

    conn = app.get_db_connection()
    committed = False
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        current = _report_local_selection(cursor, plan)
        if current != local:
            raise AssistantClientError("Кандидатский кэш изменился во время проверки; повторите создание PDF.")
        if set(current) - set(remote) or any(current[key] != remote[key] for key in current):
            raise AssistantClientError("Найдены локальные отличия; импорт выбранных записей отменён.")
        for shift in selected:
            if cursor.execute(
                    "SELECT 1 FROM shift_sync_outbox WHERE sync_id=? LIMIT 1", (shift["sync_id"],)
            ).fetchone():
                raise AssistantClientError("Для выбранной записи есть локальная операция; импорт остановлен.")
            if shift.get("source") == "legacy":
                collision = cursor.execute(
                    "SELECT 1 FROM shifts WHERE date=? AND employee=? AND company=? AND object_name=? AND hours=? LIMIT 1",
                    (shift["date"], shift["employee"], shift["company"], shift["object_name"], shift["hours"]),
                ).fetchone()
                if collision:
                    raise AssistantClientError("Найдена несвязанная локальная запись; импорт остановлен.")
        if object_entry:
            if cursor.execute("SELECT COUNT(*) FROM companies WHERE name=?",
                              (object_entry["company"],)).fetchone()[0] != 1:
                raise AssistantClientError("Фирма отсутствует или неоднозначна в локальном справочнике; кэш не изменён.")
            if cursor.execute("SELECT 1 FROM objects WHERE sync_id=? LIMIT 1",
                              (object_entry["sync_id"],)).fetchone():
                raise AssistantClientError("Связь объекта уже существует под другим названием; кэш не изменён.")
            before_outbox = cursor.execute("SELECT COUNT(*) FROM object_sync_outbox").fetchone()[0]
            stats = app.merge_server_object_catalog(cursor, [object_entry])
            after_outbox = cursor.execute("SELECT COUNT(*) FROM object_sync_outbox").fetchone()[0]
            if (stats["imported"] != 1 or stats["updated"] or stats["linked"] or stats["conflicts"]
                    or stats["unresolved_legacy"] or after_outbox != before_outbox):
                raise AssistantClientError("Объект не удалось безопасно добавить в кэш; все изменения отменены.")
        for shift in selected:
            window.apply_remote_shift(cursor, shift)
        refreshed = _report_local_selection(cursor, plan)
        if refreshed != remote:
            raise AssistantClientError("Полученные данные не совпали с проверенной выборкой; все изменения отменены.")
        if object_entry and cursor.execute(
                "SELECT COUNT(*) FROM objects WHERE company=? AND name=?",
                (object_entry["company"], object_entry["name"]),
        ).fetchone()[0] != 1:
            raise AssistantClientError("Локальная связь объекта не стала однозначной; все изменения отменены.")
        conn.commit()
        committed = True
    finally:
        if not committed:
            conn.rollback()
        conn.close()


def open_report(window, result):
    plan = result["plan"]
    if plan["purpose"] not in REPORT_ACTIONS:
        raise AssistantClientError("Для PDF попросите зарплатную сводку либо отчёт по фирме и объекту.")
    import sys
    app = sys.modules[window.__class__.__module__]
    conn = app.get_db_connection()
    try:
        conn.execute("PRAGMA query_only=ON")
        # Existing customer reports are whole-object documents, not a single worker subset.
        if plan["purpose"] in {"customer_pdf", "journal_pdf", "catalog_preview"} and plan["employee"]:
            raise AssistantClientError("Клиентский PDF включает весь объект. Повторите команду без фильтра сотрудника.")
        local = _report_local_selection(conn.cursor(), plan)
        fields = ("hours", "prices", "trips", "entry_type", "start_time", "end_time", "invoiced", "intervals")
        remote = {r["id"]: {key: r[key] for key in fields} for r in result["records"]}
        object_count = 0
        if plan["purpose"] in {"customer_pdf", "journal_pdf", "catalog_preview"}:
            obj = result["object"]
            if obj is None:
                raise AssistantClientError("Для клиентского PDF укажите точную фирму и объект.")
            object_count = conn.execute("SELECT COUNT(*) FROM objects WHERE company=? AND name=?",
                                        (obj["company"], obj["name"])).fetchone()[0]
    finally:
        conn.close()
    needs_local_object = plan["purpose"] in {"customer_pdf", "journal_pdf", "catalog_preview"} \
        and object_count != 1
    if local != remote or needs_local_object:
        _refresh_report_cache(window, app, plan, result, local, remote, object_count)
        conn = app.get_db_connection()
        try:
            conn.execute("PRAGMA query_only=ON")
            local = _report_local_selection(conn.cursor(), plan)
            if plan["purpose"] in {"customer_pdf", "journal_pdf", "catalog_preview"}:
                object_count = conn.execute("SELECT COUNT(*) FROM objects WHERE company=? AND name=?",
                                            (result["object"]["company"], result["object"]["name"])).fetchone()[0]
        finally:
            conn.close()
    if local != remote:
        raise AssistantClientError("Локальные записи всё ещё отличаются от серверной выборки; PDF не сформирован.")
    if plan["purpose"] in {"customer_pdf", "journal_pdf", "catalog_preview"} and object_count != 1:
        raise AssistantClientError("Нет однозначного локального объекта; PDF не сформирован.")
    if plan["purpose"] == "payroll_pdf":
        if plan["company"] or plan["object_name"]:
            raise AssistantClientError("Зарплатная сводка поддерживает сотрудника и период, но не фильтр объекта. Ничего не сформировано.")
        index = window.sal_employee_filter.findData(plan["employee"] or None)
        if index < 0:
            raise AssistantClientError("Сотрудник отсутствует в локальном фильтре. Обновите справочник.")
        with QSignalBlocker(window.sal_employee_filter), QSignalBlocker(window.sal_report_mode):
            window.sal_date_from.setText(plan["date_from"])
            window.sal_date_to.setText(plan["date_to"])
            window.sal_report_mode.setCurrentIndex(window.sal_report_mode.findData("management"))
            window.sal_employee_filter.setCurrentIndex(index)
        window.load_salary_table_template()
        import payroll_ui
        payroll_ui.preview(window, app)
    else:
        index = window.inv_comp_cb.findText(plan["company"])
        if index < 0:
            raise AssistantClientError("Фирма отсутствует в локальном справочнике.")
        window.inv_date_from.setText(plan["date_from"])
        window.inv_date_to.setText(plan["date_to"])
        window.inv_comp_cb.setCurrentIndex(index)
        window.load_invoice_objects(plan["company"])
        index = window.inv_obj_cb.findText(plan["object_name"])
        # Object display text may include a project code; resolve through the existing ID.
        if index < 0:
            conn = app.get_db_connection()
            try:
                row = conn.execute("SELECT id FROM objects WHERE company=? AND name=?",
                                   (plan["company"], plan["object_name"])).fetchone()
                index = window.inv_obj_cb.findData(row["id"]) if row else -1
            finally:
                conn.close()
        if index < 0:
            raise AssistantClientError("Объект отсутствует в локальном фильтре.")
        window.inv_obj_cb.setCurrentIndex(index)
        window.generate_invoice()
        if plan["purpose"] == "catalog_preview":
            window.preview_catalog_prices()
        elif plan["purpose"] == "journal_pdf":
            window.export_dagbok_to_pdf()
        else:
            window.export_invoice_to_pdf()


class AssistantDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window, self.worker, self.result, self.audio = window, None, None, None
        self.buffer = None
        self.recording_started_at = None
        self.voice_warning = ""
        self.context = None
        self.chat_messages = []
        self.choices = []
        self.capability_data = []
        self.auto_report = False
        self.workflow_results = []
        self.attention = None
        self.attention_expanded = False
        app_module = sys.modules.get(window.__class__.__module__)
        self.assistant_candidate = bool(getattr(app_module, "ASSISTANT_CANDIDATE", False))
        self.database = getattr(app_module, "DB_FILE", None)
        self.setWindowTitle("SmartReport — ИИ-помощник")
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        self.resize(
            min(1000, max(440, available.width() - 48)) if available is not None else 1000,
            min(740, max(360, available.height() - 80)) if available is not None else 740,
        )
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMinMaxButtonsHint)
        self.setSizeGripEnabled(True)
        outer_layout = QVBoxLayout(self)
        self.content_scroll = QScrollArea(self)
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(self.content_scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        self.content_scroll.setWidget(content)
        outer_layout.addWidget(self.content_scroll)
        notice_text = ("Кандидат подключён к рабочему серверу: только чтение и подготовка PDF. "
                       "Синхронизация, рабочие формы, изменения и отправка писем отключены."
                       if self.assistant_candidate else
                       "SmartReport — рабочий помощник. Опишите задачу обычными словами.\n"
                       "Период и выбранные объекты сохраняются в этом диалоге. Не вводите пароли и реквизиты.")
        notice = QLabel(notice_text)
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.diagnostics = QDialog(self)
        self.diagnostics.setWindowTitle("Помощник — диагностика и настройки")
        self.diagnostics.resize(
            min(800, max(420, available.width() - 64)) if available is not None else 800,
            min(600, max(320, available.height() - 96)) if available is not None else 600,
        )
        diagnostic_layout = QVBoxLayout(self.diagnostics)
        form = QFormLayout()
        self.enabled = QCheckBox("Помощник включён на сервере")
        self.limit = QDoubleSpinBox()
        self.limit.setRange(0, 1000)
        self.limit.setDecimals(2)
        self.limit.setValue(10)
        form.addRow(self.enabled)
        form.addRow("Месячный лимит резервов, USD:", self.limit)
        diagnostic_layout.addLayout(form)
        self.diagnostic_state = QLabel()
        self.diagnostic_state.setWordWrap(True)
        diagnostic_layout.addWidget(self.diagnostic_state)
        self.state = QLabel("Подключение…")
        self.state.setWordWrap(True)
        layout.addWidget(self.state)
        controls = QHBoxLayout()
        for text, action in (("Обновить состояние", lambda: self.call("status")),
                             ("Сохранить выключатель/лимит", self.save_settings),
                             ("Журнал", lambda: self.call("audit")), ("Подключить OpenAI", self.instructions)):
            button = QPushButton(text)
            button.clicked.connect(action)
            controls.addWidget(button)
        diagnostic_layout.addLayout(controls)
        desktop_theme.adapt_box_layout(self.diagnostics, controls, compact_width=900)
        self.diagnostic_output = QTextEdit()
        self.diagnostic_output.setReadOnly(True)
        diagnostic_layout.addWidget(self.diagnostic_output)
        header = QHBoxLayout()
        header_actions = [("Новый диалог", self.new_conversation), ("Возможности", self.show_capabilities)]
        if not self.assistant_candidate:
            header_actions.append(("Диагностика и настройки", self.diagnostics.show))
        for text, action in header_actions:
            button = QPushButton(text)
            button.clicked.connect(action)
            header.addWidget(button)
        layout.addLayout(header)
        desktop_theme.adapt_box_layout(content, header, compact_width=900)
        filters = QHBoxLayout()
        today = QDate.currentDate()
        monday = today.addDays(1 - today.dayOfWeek())
        self.date_from, self.date_to = CalendarDateEdit(monday), CalendarDateEdit(monday.addDays(6))
        for field in (self.date_from, self.date_to):
            field.setDisplayFormat("dd.MM.yyyy")
            field.setCalendarPopup(True)
        self.employee = QLineEdit()
        self.employee.setPlaceholderText("Все сотрудники (или точное ФИО)")
        self.company, self.object_name = QLineEdit(), QLineEdit()
        self.company.setPlaceholderText("Все фирмы")
        self.object_name.setPlaceholderText("Все объекты")
        filters.addWidget(QLabel("С:"))
        filters.addWidget(self.date_from)
        filters.addWidget(QLabel("По:"))
        filters.addWidget(self.date_to)
        filters.addWidget(self.employee)
        filters.addWidget(self.company)
        filters.addWidget(self.object_name)
        layout.addLayout(filters)
        desktop_theme.adapt_box_layout(content, filters, compact_width=1080)
        shortcuts = QHBoxLayout()
        attention_button = QPushButton("Требует внимания")
        attention_button.clicked.connect(lambda: self.start_attention("Что требует моего внимания?"))
        attention_button.setEnabled(not self.assistant_candidate)
        shortcuts.addWidget(attention_button)
        for text, purpose in (("Кто сколько отработал", "hours_summary"), ("Дубли и пропущенные ставки", "anomalies"),
                              ("Проверить транспорт", "transport_check")):
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, purpose=purpose: self.quick_action(purpose))
            shortcuts.addWidget(button)
        layout.addLayout(shortcuts)
        desktop_theme.adapt_box_layout(content, shortcuts, compact_width=900)
        actions = QHBoxLayout()
        self.action_choice = QComboBox()
        for key, (title, mode) in CAPABILITIES.items():
            if self.assistant_candidate and key not in CANDIDATE_READ_ACTIONS:
                continue
            self.action_choice.addItem(title, key)
        actions.addWidget(self.action_choice)
        run_action = QPushButton("Подготовить выбранное действие")
        run_action.clicked.connect(lambda: self.quick_action(self.action_choice.currentData()))
        actions.addWidget(run_action)
        layout.addLayout(actions)
        run_action.setObjectName("primaryAction")
        desktop_theme.adapt_box_layout(content, actions, compact_width=900)
        self.workflow_choice = QComboBox()
        self.workflow_choice.setVisible(False)
        self.workflow_choice.currentIndexChanged.connect(self.select_workflow_result)
        layout.addWidget(self.workflow_choice)
        clarification_row = QHBoxLayout()
        self.clarification_choice = QComboBox()
        self.clarification_choice.setVisible(False)
        clarification_row.addWidget(self.clarification_choice)
        self.choose_clarification = QPushButton("Выбрать")
        self.choose_clarification.setVisible(False)
        self.choose_clarification.clicked.connect(self.choose_answer)
        clarification_row.addWidget(self.choose_clarification)
        self.continue_selection = QPushButton("Продолжить с выбранными параметрами")
        self.continue_selection.setVisible(False)
        self.continue_selection.clicked.connect(lambda: self.quick_action(self.context["plan"]["purpose"])
                                                if self.context else None)
        clarification_row.addWidget(self.continue_selection)
        layout.addLayout(clarification_row)
        desktop_theme.adapt_box_layout(content, clarification_row, compact_width=900)
        hint = QLabel("Выбор выше — контекст диалога. Можно изменить его словами: «за прошлую неделю», "
                      "«по другому объекту». Открытие формы не означает сохранения данных.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.input = QTextEdit()
        self.input.setAcceptRichText(False)
        self.input.setMaximumHeight(100)
        self.input.setPlaceholderText("Например: покажи, кто сколько отработал на этой неделе")
        layout.addWidget(self.input)
        row = QHBoxLayout()
        self.send = QPushButton("Отправить")
        self.send.clicked.connect(self.command)
        row.addWidget(self.send)
        self.voice = QPushButton("Записать голос")
        self.voice.clicked.connect(self.record)
        row.addWidget(self.voice)
        self.voice_stop = QPushButton("Остановить и распознать")
        self.voice_stop.setVisible(False)
        self.voice_stop.clicked.connect(self.stop_recording)
        row.addWidget(self.voice_stop)
        self.voice_cancel = QPushButton("Отменить запись")
        self.voice_cancel.setVisible(False)
        self.voice_cancel.clicked.connect(self.cancel_recording)
        row.addWidget(self.voice_cancel)
        tools = QHBoxLayout()
        self.pdf = QPushButton("Создать / открыть PDF")
        self.pdf.setEnabled(False)
        self.pdf.clicked.connect(self.report)
        tools.addWidget(self.pdf)
        mail = QPushButton("Подготовить письмо с PDF")
        mail.clicked.connect(self.prepare_mail)
        tools.addWidget(mail)
        self.mail_button = mail
        layout.addLayout(row)
        voice_status = QHBoxLayout()
        self.microphone_label = QLabel()
        self.microphone_label.setAccessibleName("Выбранный микрофон")
        self.refresh_microphone_label()
        voice_status.addWidget(self.microphone_label)
        self.voice_level = QProgressBar()
        self.voice_level.setRange(0, 100)
        self.voice_level.setTextVisible(False)
        self.voice_level.setAccessibleName("Уровень звука микрофона")
        voice_status.addWidget(self.voice_level, 1)
        self.recording_time = QLabel("00:00 / 01:00")
        self.recording_time.setAccessibleName("Длительность записи")
        voice_status.addWidget(self.recording_time)
        layout.addLayout(voice_status)
        layout.addLayout(tools)
        self.native = QPushButton("Открыть штатную форму")
        self.native.setEnabled(False)
        self.native.clicked.connect(self.open_native)
        tools.addWidget(self.native)
        self.open_records = QPushButton("Открыть выбранную запись")
        self.open_records.setEnabled(False)
        self.open_records.clicked.connect(self.record_details)
        tools.addWidget(self.open_records)
        self.show_all_attention = QPushButton("Показать все")
        self.show_all_attention.setVisible(False)
        self.show_all_attention.clicked.connect(self.expand_attention)
        tools.addWidget(self.show_all_attention)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(150)
        theme_manager = getattr(QApplication.instance(), "_smartreport_theme", None)
        if theme_manager is not None:
            theme_manager.changed.connect(self.refresh_chat_theme)
        layout.addWidget(self.output)
        self.table = QTableWidget()
        self.table.setMinimumHeight(170)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setVisible(False)
        layout.addWidget(self.table)
        layout.removeWidget(self.output)
        layout.insertWidget(layout.indexOf(self.input), self.output, 2)
        layout.removeWidget(self.table)
        layout.insertWidget(layout.indexOf(self.input), self.table, 2)
        self.result_heading = QLabel()
        self.result_heading.setTextFormat(Qt.TextFormat.PlainText)
        self.result_heading.setWordWrap(True)
        layout.insertWidget(layout.indexOf(self.table), self.result_heading)
        self.table.cellDoubleClicked.connect(lambda *_: self.record_details())
        self.confirm_mail = QPushButton("Подтвердить отправку подготовленного письма…")
        self.confirm_mail.setEnabled(False)
        self.confirm_mail.clicked.connect(self.send_mail)
        layout.addWidget(self.confirm_mail)
        desktop_theme.adapt_box_layout(content, row, compact_width=1000)
        desktop_theme.adapt_box_layout(content, voice_status, compact_width=1000)
        desktop_theme.adapt_box_layout(content, tools, compact_width=1000)
        if self.assistant_candidate:
            self.native.hide()
            self.mail_button.hide()
            self.confirm_mail.hide()
            self.native.setEnabled(False)
            self.mail_button.setEnabled(False)
            self.confirm_mail.setEnabled(False)
            self.diagnostics.hide()
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.update_recording_status)
        self.call("status")

    def refresh_microphone_label(self):
        from PyQt6.QtMultimedia import QMediaDevices
        device = QMediaDevices.defaultAudioInput()
        self.microphone_label.setText(
            "Микрофон: " + (device.description() if not device.isNull() else "не найден")
        )

    def append_message(self, role, text):
        message = (str(role), str(text))
        self.chat_messages.append(message)
        self._insert_chat_message(*message)

    def _insert_chat_message(self, role, text):
        cursor = self.output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        if self.output.document().blockCount() > 1 or self.output.toPlainText():
            cursor.insertHtml('<p style="margin:0; font-size:8pt">&nbsp;</p>')
        cursor.insertHtml(self._chat_message_html(role, text))
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    @staticmethod
    def _chat_message_html(role, text):
        label = {"Вы": "Вы", "SmartReport": "SmartReport"}.get(str(role), str(role))
        background_color = "#313c4e" if label == "Вы" else "#2d3748"
        background = desktop_theme.color(background_color).name()
        body = escape(str(text)).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
        return (
            f'<table width="100%" cellspacing="0" cellpadding="10" bgcolor="{background}">'
            '<tr><td>'
            f'<p style="margin:0 0 5px 0"><span style="font-size:12pt; font-weight:700">{escape(label)}</span></p>'
            f'<p style="margin:0; font-size:10pt; font-weight:400">{body}</p>'
            '</td></tr></table>'
        )

    def refresh_chat_theme(self, _theme_name):
        scrollbar = self.output.verticalScrollBar()
        previous_value = scrollbar.value()
        was_at_bottom = previous_value >= scrollbar.maximum()
        chunks = []
        for index, (role, text) in enumerate(self.chat_messages):
            if index:
                chunks.append('<p style="margin:0; font-size:8pt">&nbsp;</p>')
            chunks.append(self._chat_message_html(role, text))
        self.output.setHtml("".join(chunks))
        scrollbar.setValue(scrollbar.maximum() if was_at_bottom else previous_value)

    def new_conversation(self):
        if self.audio is not None or self.worker and self.worker.isRunning():
            self.state.setText("Сначала завершите запись или запрос.")
            return
        self.context = None
        self.result = None
        self.attention = None
        self.show_all_attention.setVisible(False)
        self.auto_report = False
        self.choices = []
        self.workflow_results = []
        self.workflow_choice.setVisible(False)
        self.clear_clarification()
        self.pending_draft = None
        self.input.clear()
        for field in (self.employee, self.company, self.object_name):
            field.clear()
        today = QDate.currentDate()
        monday = today.addDays(1 - today.dayOfWeek())
        self.date_from.setDate(monday)
        self.date_to.setDate(monday.addDays(6))
        self.chat_messages.clear()
        self.output.clear()
        self.table.setVisible(False)
        self.result_heading.clear()
        for button in (self.pdf, self.native, self.open_records, self.confirm_mail):
            button.setEnabled(False)
        self.state.setText("Новый диалог. Укажите задачу или выберите действие.")

    def selection(self, purpose="search"):
        return dict(purpose=purpose, date_from=self.date_from.date().toString("yyyy-MM-dd"),
                    date_to=self.date_to.date().toString("yyyy-MM-dd"), employee=self.employee.text().strip(),
                    company=self.company.text().strip(), object_name=self.object_name.text().strip())

    def clear_clarification(self):
        for widget in (self.clarification_choice, self.choose_clarification, self.continue_selection):
            widget.setVisible(False)

    def choose_answer(self):
        if self.choices:
            self.input.setPlainText(str(self.clarification_choice.currentIndex() + 1))
            self.command()

    def show_capabilities(self):
        self.call("capabilities")

    def call(self, path, payload=None, audio=None, audio_terms=None):
        if self.assistant_candidate:
            allowed = path in CANDIDATE_REQUESTS and not (path == "status" and payload is not None)
            if path == "inspect":
                allowed = allowed and isinstance(payload, dict) and isinstance(payload.get("plan"), dict) \
                    and payload["plan"].get("purpose") in CANDIDATE_READ_ACTIONS
            if not allowed:
                self.failed("Это действие отключено в кандидате: доступны только чтение, проверки и предпросмотр PDF.")
                return
        if self.audio is not None:
            self.state.setText("Сначала остановите запись голоса.")
            return
        if self.worker and self.worker.isRunning():
            self.state.setText("Дождитесь завершения текущего запроса; повтор не отправлен.")
            return
        self.send.setEnabled(False)
        self.voice.setEnabled(False)
        self.native.setEnabled(False)
        self.open_records.setEnabled(False)
        self.workflow_choice.setEnabled(False)
        self.worker = RequestWorker(
            path, payload, audio, self, database=self.database, audio_terms=audio_terms,
        )
        self.worker.ready.connect(self.received)
        self.worker.failed.connect(self.failed)
        self.worker.finished.connect(self.finished)
        self.worker.start()

    def finished(self):
        if self.worker is not None:
            self.worker.audio = None
            self.worker.audio_terms = None
        self.send.setEnabled(True)
        self.voice.setEnabled(True)
        self.workflow_choice.setEnabled(True)
        self.native.setEnabled(bool(self.result and "native_action" in self.result))
        self.open_records.setEnabled(bool(self.attention and self.attention_rows) or bool(
            self.result and self.result["records"] and "table" not in self.result))
        if self.auto_report:
            self.auto_report = False
            self.report()

    def failed(self, message):
        self.state.setText(message)
        self.append_message("SmartReport", message + "\nНовая операция не выполнена; автоматического повтора нет.")
        self.table.setVisible(False)
        self.result = None
        self.result_heading.clear()
        self.pdf.setEnabled(False)
        self.confirm_mail.setEnabled(False)
        self.native.setEnabled(False)
        self.open_records.setEnabled(False)
        self.auto_report = False

    def received(self, data):
        if "results" in data:
            self.workflow_results = data["results"]
            with QSignalBlocker(self.workflow_choice):
                self.workflow_choice.clear()
                for index, result in enumerate(self.workflow_results):
                    self.workflow_choice.addItem(f'{index + 1}. {CAPABILITIES[result["plan"]["purpose"]][0]}')
                    self.received(dict(result=result, _workflow=True))
                self.workflow_choice.setCurrentIndex(len(self.workflow_results) - 1)
            self.workflow_choice.setVisible(True)
            self.append_message("SmartReport", f'Выполнено шагов: {len(self.workflow_results)}. '
                                     "Показан выбранный результат; переключите шаг в списке над перепиской.")
        elif "clarification" in data:
            self.result = None
            self.result_heading.clear()
            self.pdf.setEnabled(False)
            self.table.setVisible(False)
            clarification = data["clarification"]
            pending_plan = clarification.get("plan") or (self.context["plan"] if self.context else self.selection())
            self.context = clarification.get("context") or dict(
                plan=pending_plan, pending=clarification["field"],
            )
            self.choices = clarification.get("choices", [])
            self.clear_clarification()
            self.date_from.setDate(QDate.fromString(pending_plan["date_from"], "yyyy-MM-dd"))
            self.date_to.setDate(QDate.fromString(pending_plan["date_to"], "yyyy-MM-dd"))
            self.employee.setText(pending_plan["employee"])
            self.company.setText(pending_plan["company"])
            self.object_name.setText(pending_plan["object_name"])
            self.append_message("SmartReport", clarification["question"])
            if self.choices:
                self.clarification_choice.clear()
                self.clarification_choice.addItems([
                    choice.get("label") or " / ".join(str(value) for key, value in choice.items() if key != "label")
                    for choice in self.choices
                ])
                self.clarification_choice.setVisible(True)
                self.choose_clarification.setVisible(True)
                self.append_message("SmartReport", "\n".join(
                    f"{index + 1}. " + (
                        choice.get("label") or " / ".join(
                            str(value) for key, value in choice.items() if key != "label"
                        )
                    ) for index, choice in enumerate(self.choices)))
            self.continue_selection.setVisible(clarification["field"] != "unsupported")
            self.state.setText("Нужен ответ. Выберите параметры и действие либо уточните текстовую команду.")
            if data["clarification"]["field"] == "scope":
                self.employee.setFocus()
            elif data["clarification"]["field"] == "period":
                self.date_from.setFocus()
            else:
                self.input.setFocus()
        elif "capabilities" in data:
            self.capability_data = data["capabilities"]
            text = "\n".join(item["title"] + " — " + item["notice"] for item in self.capability_data)
            self.append_message("SmartReport", text + "\n\nОграничения:\n" + "\n".join(data["restrictions"]))
        elif "status" in data:
            s = data["status"]
            self.enabled.setChecked(s["enabled"])
            self.limit.setValue(s["limit_usd"])
            key = "настроен" if s["key_configured"] else "НЕ настроен"
            self.diagnostic_state.setText(f'Ключ: {key}; модель {s["model"]}; {s["month"]}: '
                               f'резерв {s["reserved_usd"]:.4f} / {s["limit_usd"]:.2f} USD.\n{s["notice"]}')
            self.state.setText("Помощник готов. Опишите задачу." if s["enabled"] else
                               "Помощник выключен. Включить можно в диагностике и настройках.")
        elif "text" in data:
            transcript = data["text"]
            if not isinstance(transcript, str) or not transcript.strip():
                self.failed("Распознавание вернуло пустой текст. Проверьте микрофон и попробуйте ещё раз.")
                return
            transcript = transcript.strip()
            current = self.input.toPlainText()
            self.input.setPlainText(current.rstrip() + ("\n" if current.strip() else "") + transcript)
            warning = (self.voice_warning + " ") if self.voice_warning else ""
            self.state.setText(
                warning + "Речь распознана и добавлена в поле. Проверьте ФИО, даты и числа, "
                "исправьте при необходимости и нажмите «Отправить»."
            )
            self.voice_warning = ""
        elif "actions" in data:
            names = {"search": "Поиск смен", "check": "Проверка данных", "transport_check": "Проверка транспорта",
                     "text": "Текстовая команда", "voice": "Голос", "settings": "Настройки"}
            outcomes = {"reserved": "Начато", "routed": "Действие определено", "read_completed": "Просмотр завершён",
                        "failed": "Не выполнено", "clarification": "Запрошено уточнение"}
            self.diagnostic_output.setPlainText("\n".join(
                f'{r["created_at"]} — {names.get(r["action"], "Действие помощника")}: '
                f'{outcomes.get(r["outcome"], r["outcome"])}' for r in data["actions"]) or "Журнал пуст.")
        elif "draft" in data:
            self.pending_draft = data["draft"]
            self.append_message("SmartReport", "Письмо подготовлено, НЕ отправлено.\n"
                                     f'Кому: {self.mail_data["to"]}\nТема: {self.mail_data["subject"]}\n'
                                     f'{self.mail_data["body"]}\n\nPDF: {self.mail_filename}\n'
                                     + self.pending_draft["notice"])
            self.diagnostic_output.setPlainText("SHA256 вложения: " + self.pending_draft["sha256"])
            self.state.setText("Проверьте письмо и вложение. Кнопка подтверждения доступна ниже.")
            self.confirm_mail.setEnabled(True)
        elif "mail" in data:
            self.confirm_mail.setEnabled(False)
            self.append_message("SmartReport", data["mail"]["notice"])
            self.diagnostic_output.setPlainText("Message-ID: " + data["mail"]["message_id"])
        elif "result" in data:
            purpose = data["result"].get("plan", {}).get("purpose")
            if self.assistant_candidate and purpose not in CANDIDATE_READ_ACTIONS:
                self.failed("Этот раздел недоступен в кандидате. Здесь разрешены только чтение, проверки и отчёты.")
                return
            self.clear_clarification()
            if not data.get("_workflow") and not data.get("_repeat"):
                self.workflow_results = []
                self.workflow_choice.setVisible(False)
            self.result = data["result"]
            try:
                if self.result["plan"]["purpose"] in {"balance", "expenses"}:
                    self.result = local_result(self.window, self.result)
            except DesktopActionError as error:
                self.failed(str(error))
                return
            except sqlite3.Error:
                self.failed("Не удалось прочитать выбранный локальный раздел или фильтр не поддерживается. "
                            "Откройте раздел вручную; другие профили не использовались.")
                return
            plan = self.result["plan"]
            self.context = self.result.get("context") or dict(plan=plan, pending="")
            self.choices = []
            self.date_from.setDate(QDate.fromString(plan["date_from"], "yyyy-MM-dd"))
            self.date_to.setDate(QDate.fromString(plan["date_to"], "yyyy-MM-dd"))
            self.employee.setText(plan["employee"])
            self.company.setText(plan["company"])
            self.object_name.setText(plan["object_name"])
            if not data.get("_repeat") and not data.get("_workflow"):
                self.append_message("SmartReport", result_text(self.result))
            self.result_heading.setText(CAPABILITIES[plan["purpose"]][0] +
                                        f' · {plan["date_from"]} — {plan["date_to"]}' +
                                        ("\n" + self.result["summary"] if self.result.get("summary") else ""))
            headers, rows = result_table(self.result)
            sorting = self.table.isSortingEnabled()
            self.table.setSortingEnabled(False)
            self.table.setColumnCount(len(headers))
            self.table.setHorizontalHeaderLabels(headers)
            self.table.setRowCount(len(rows))
            for index, cells in enumerate(rows):
                for column, value in enumerate(cells):
                    self.table.setItem(index, column, QTableWidgetItem(value))
            self.table.setSortingEnabled(sorting)
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
            self.table.resizeColumnsToContents()
            for column in range(len(headers)):
                self.table.setColumnWidth(column, min(310, self.table.columnWidth(column)))
            self.table.resizeRowsToContents()
            self.table.setVisible(bool(rows))
            self.pdf.setEnabled(plan["purpose"] in REPORT_ACTIONS)
            self.pdf.setText("Открыть штатный просмотр изменений…" if self.result["plan"]["purpose"] == "catalog_preview"
                             else "Создать / открыть PDF")
            self.state.setText("Готово. Результат получен из программы, не сочинён моделью. Данные не изменены.")
            if data.get("_workflow"):
                self.state.setText("Цепочка выполнена только для чтения; выбран результат шага. "
                                   "Переключите шаг над перепиской.")
            self.native.setEnabled("native_action" in self.result)
            self.open_records.setEnabled(bool(self.result["records"]) and "table" not in self.result)
            self.auto_report = plan["purpose"] in REPORT_ACTIONS and not data.get("_repeat")

    def select_workflow_result(self, index):
        if self.worker and self.worker.isRunning():
            return
        if 0 <= index < len(self.workflow_results):
            self.received(dict(result=self.workflow_results[index], _repeat=True))

    def start_attention(self, text, already_appended=False, *, hours_only=False):
        if self.assistant_candidate:
            self.state.setText("Сводка недоступна в изолированном кандидате.")
            return
        if self.audio is not None or self.worker and self.worker.isRunning():
            self.state.setText("Сначала завершите текущую запись или запрос.")
            return
        self.result = None
        self.attention = None
        self.table.setVisible(False)
        self.result_heading.clear()
        self.show_all_attention.setVisible(False)
        self.pdf.setEnabled(False)
        self.native.setEnabled(False)
        self.open_records.setEnabled(False)
        if not desktop_auth.current_token() or desktop_auth.current_user().get("role") != "admin":
            self.state.setText("Для сводки нужен личный вход администратора.")
            return
        try:
            periods = assistant_attention.periods(text)
        except ValueError as error:
            self.state.setText(str(error))
            self.append_message("SmartReport", str(error))
            return
        if not already_appended:
            self.append_message("Вы", text)
        self.send.setEnabled(False)
        self.voice.setEnabled(False)
        self.workflow_choice.setEnabled(False)
        self.state.setText("Проверяю доступные источники; ничего не изменяется.")
        from assistant_desktop import application
        try:
            app = application(self.window)
        except DesktopActionError as error:
            self.failed(str(error))
            self.finished()
            return
        self.attention_periods = periods
        self.worker = AttentionWorker(self.database, app, text, periods, self, hours_only=hours_only)
        self.worker.ready.connect(self.attention_received)
        self.worker.finished.connect(self.finished)
        self.worker.start()

    def attention_received(self, data):
        local = data["local"]
        if local is None:
            self.failed("\n".join(data["errors"]))
            return
        errors = list(data["errors"])
        company, obj = local["company"], local["object_name"]
        pending = set(local["pending"])
        local_rows = local["rows"]
        overrides = {row["sync_id"] for row in local_rows if row.get("sync_id") in pending}
        if pending:
            errors.append("Есть несинхронизированные или конфликтующие изменения смен; "
                          "локальные записи показаны отдельно от подтверждённого состояния сервера.")

        def combined(remote):
            server_ids = {row["id"] for row in remote or []}
            local_only = [row for row in local_rows
                          if not row.get("sync_id") or row["sync_id"] not in server_ids
                          or row["sync_id"] in overrides]
            return [row for row in remote or [] if row["id"] not in overrides], local_only

        employees = local["employees"]
        status = data["employee_status"]
        normalize = lambda name: " ".join(str(name or "").split()).casefold()
        if status:
            candidates = [
                {"name": row[1], "is_active": 1}
                for row in status.get("table", {}).get("rows", [])
                if row[0] in ("Без внесённых часов", "Внесли часы")
            ]
            ambiguous = status.get("employees_with_ambiguous_records")
            if ambiguous:
                errors.append("Неоднозначные сотрудники на сервере: проверка неполная.")
            counts = {}
            for item in candidates:
                name = normalize(item["name"])
                counts[name] = counts.get(name, 0) + 1
            duplicate_names = {name for name, count in counts.items() if not name or count > 1}
            if duplicate_names:
                errors.append("Одинаковые имена сотрудников неоднозначны; проверка неполная.")
            server_employees = [item for item in candidates if normalize(item["name"]) not in duplicate_names]
            server_names = {normalize(item["name"]) for item in server_employees}
            extra = [item for item in employees if item["is_active"]
                     and normalize(item["name"]) not in server_names]
            if extra:
                errors.append("Локальный и серверный справочники сотрудников различаются.")
            employees = server_employees
        else:
            counts = {}
            for item in employees:
                name = normalize(item["name"])
                counts[name] = counts.get(name, 0) + 1
            duplicate_names = {name for name, count in counts.items() if not name or count > 1}
            if duplicate_names:
                errors.append("Одинаковые имена сотрудников неоднозначны; проверка неполная.")
            employees = [item for item in employees if normalize(item["name"]) not in duplicate_names]
        remote_hours, local_hours = combined(data["hours"])
        try:
            if data["hours"] is None and status and (company or obj):
                missing, hour_warnings = [], []
                errors.append("Нет полного списка серверных смен: сотрудников без часов "
                              "для выбранной области определить не удалось.")
            elif data["hours"] is None and status:
                start, end = self.attention_periods["hours"]
                local_worked = {" ".join(str(row.get("employee") or "").split()).casefold()
                                for row in local_hours
                                if row.get("entry_type") in ("work", "ob")
                                and start <= row["date"] <= end}
                missing = [
                    {"kind": "employee", "employee": row[1], "reason": "Нет записей часов за период"}
                    for row in status.get("table", {}).get("rows", [])
                    if row[0] == "Без внесённых часов"
                    and " ".join(row[1].split()).casefold() not in local_worked
                    and " ".join(row[1].split()).casefold() not in duplicate_names
                ]
                hour_warnings = ["Без графика отсутствие записи не доказывает отсутствие работы."]
            else:
                missing, hour_warnings = assistant_attention.missing_hours(
                    employees, remote_hours + local_hours, self.attention_periods["hours"], company, obj)
        except (sqlite3.Error, ValueError, KeyError) as error:
            missing, hour_warnings = [], []
            errors.append("Часы: не удалось проверить: " + str(error))
        if data.get("hours_only"):
            self.display_attention(data, errors, hour_warnings,
                                   [("Нет записей часов", missing)], [], [])
            return
        remote_prices, local_prices = combined(data["prices"])
        start, end = self.attention_periods["prices"]
        local_prices = [row for row in local_prices if start <= row["date"] <= end
                        and (not company or row["company"] == company)
                        and (not obj or row["object_name"] == obj)]
        try:
            remote_issues, remote_unverified = assistant_attention.remote_price_issues(remote_prices)
            prices = remote_issues + assistant_attention.price_issues(local_prices)
            if remote_unverified:
                errors.append(f"Клиентские условия интервалов не удалось полностью проверить "
                              f"для серверных смен: {len(remote_unverified)}.")
            if any(row.get("unverified") for row in prices):
                errors.append("Некоторые клиентские условия не удалось полностью проверить.")
        except (ValueError, KeyError, TypeError) as error:
            prices = []
            errors.append("Клиентские цены: не удалось проверить: " + str(error))

        try:
            server = data["documents"]
            if server is None:
                server = getattr(self.window, "_expense_read_cache", {}).get((None, None), [])
            if data.get("local_available", True):
                documents, _online = self.window._expense_document_snapshot(
                    server_snapshot=(server, data["documents"] is not None))
            else:
                documents = list(server)
        except (sqlite3.Error, OSError, ValueError, KeyError) as error:
            documents = list(server or [])
            errors.append("Локальная очередь расходов: не удалось проверить: " + str(error))
        try:
            incoming = self.window._local_incoming_expense_documents(readonly=True)
            documents = self.window._merge_incoming_expense_documents(documents, incoming)
        except (sqlite3.Error, OSError, ValueError, KeyError) as error:
            errors.append("Локальные входящие: не удалось проверить: " + str(error))
        try:
            if company or obj:
                errors.append("Для документов без однозначного назначения область определить нельзя; "
                              "они показаны вместе с выбранной областью.")
                object_map = {item["id"]: item for item in getattr(self.window, "_expense_objects", [])}
                def in_scope(document):
                    assignments = document.get("allocations") or []
                    if not assignments:
                        return True
                    known = [object_map.get(part.get("object_id")) for part in assignments]
                    if any(item is None for item in known):
                        return True
                    return any(item.get("company") == company and
                               (not obj or item.get("name") == obj) for item in known)
                documents = [document for document in documents if in_scope(document)]
            review, allocation = assistant_attention.document_issues(
                documents, self.window._expense_register_status_key,
                self.window._expense_register_is_unallocated,
                self.window._expense_register_unallocated_amount,
                self.attention_periods["documents"])
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            review, allocation = [], []
            errors.append("Документы и распределение: не удалось проверить: " + str(error))
        sections = [("Нет записей часов", missing), ("Проверить клиентскую цену", prices),
                    ("Документы требуют проверки", review), ("Не завершено распределение", allocation)]
        self.display_attention(data, errors, hour_warnings, sections, review, allocation)

    def display_attention(self, data, errors, hour_warnings, sections, review, allocation):
        company, obj = data["local"]["company"], data["local"]["object_name"]
        unique = {(row.get("client_id") or row.get("id") or row.get("incoming_document_id"))
                  for row in review + allocation}
        self.attention = dict(sections=sections, periods=self.attention_periods,
                              company=company, object_name=obj, errors=errors,
                              documents=data["documents"], unique_count=len(unique))
        self.attention_expanded = False
        scope = " / ".join(part for part in (company, obj) if part) or "все фирмы и объекты"
        hours_only = data.get("hours_only", False)
        lines = [f'{"Проверка записей часов" if hours_only else "Требует внимания"} · область: {scope}.']
        for title, rows in sections:
            period = (self.attention_periods["hours"] if title == "Нет записей часов" else
                      self.attention_periods["prices"] if title == "Проверить клиентскую цену" else
                      self.attention_periods["documents"])
            label = f"{period[0]} — {period[1]}" if period else "все даты"
            lines.append(f"— {title}: {len(rows)} · {label}.")
        if not hours_only:
            lines.append(f"Документов с одной или обеими причинами: {len(unique)}.")
        undated = {(row.get("client_id") or row.get("id") or row.get("incoming_document_id"))
                   for row in review + allocation if not row.get("date")}
        if undated:
            lines.append(f"Без даты: {len(undated)} документов; они включены независимо от периода.")
        lines.extend(hour_warnings)
        today_date = assistant_attention.datetime.now(
            assistant_attention.ZoneInfo("Europe/Stockholm")).date()
        week_start = (today_date - assistant_attention.timedelta(days=today_date.weekday())).isoformat()
        week_end = (today_date + assistant_attention.timedelta(days=6 - today_date.weekday())).isoformat()
        if (self.attention_periods["hours"][0] <= week_end
                and self.attention_periods["hours"][1] >= week_start
                and not any("предварительн" in warning.casefold() for warning in hour_warnings)):
            lines.append("Результат по часам предварительный: текущая неделя ещё не завершена.")
        checked_at = assistant_attention.datetime.now(
            assistant_attention.ZoneInfo("Europe/Stockholm")).strftime("%Y-%m-%d %H:%M")
        server_keys = ("employee_status", "hours") if hours_only else (
            "employee_status", "hours", "prices", "documents")
        available = sum(data.get(key) is not None for key in server_keys)
        server_state = ("получены" if available == len(server_keys) else
                        "получены частично" if available else "недоступны")
        lines.append(f"Источники: серверные данные {server_state} (запрос {checked_at}, Стокгольм) "
                     "и локальный профиль; время последней синхронизации профиля неизвестно.")
        if errors:
            lines.append("Проверка неполная:\n" + "\n".join(errors))
        elif not any(rows for _title, rows in sections):
            lines.append("По выполненной проверке записей часов замечаний нет; график и причины отсутствия неизвестны."
                         if hours_only else
                         "По выполненным проверкам замечаний нет; это не проверка всей бухгалтерии.")
        self.append_message("SmartReport", "\n".join(lines))
        self.result_heading.setText(("Проверка записей часов · " if hours_only else "Требует внимания · ") + scope)
        self.render_attention()
        self.state.setText("Готово; выберите строку для перехода. Данные не изменены.")

    def render_attention(self):
        rows = []
        for title, records in self.attention["sections"]:
            for record in records if self.attention_expanded else records[:3]:
                rows.append((title, record))
        self.attention_rows = rows
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Раздел", "Дата", "Запись", "Причина"])
        self.table.setRowCount(len(rows))
        for index, (title, record) in enumerate(rows):
            label = (" / ".join(str(record.get(key) or "") for key in ("employee", "object_name")).strip(" /")
                     if record["kind"] != "document" else
                     " / ".join(str(record.get(key) or "") for key in ("supplier", "number")).strip(" /"))
            date = record.get("date")
            if record["kind"] == "employee":
                date = " — ".join(self.attention["periods"]["hours"])
            values = [title, date or "Дата не указана", label or "Не указано", record["reason"]]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, record)
                self.table.setItem(index, column, item)
        self.table.setSortingEnabled(sorting)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.resizeColumnsToContents()
        for column in range(4):
            self.table.setColumnWidth(column, min(310, self.table.columnWidth(column)))
        self.table.resizeRowsToContents()
        self.table.setVisible(bool(rows))
        self.open_records.setEnabled(bool(rows))
        self.show_all_attention.setVisible(not self.attention_expanded and any(
            len(records) > 3 for _title, records in self.attention["sections"]))

    def expand_attention(self):
        if self.attention:
            self.attention_expanded = True
            self.render_attention()

    def open_attention_record(self):
        index = self.table.currentRow()
        item = self.table.item(index, 0) if index >= 0 else None
        record = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(record, dict):
            self.state.setText("Выберите строку сводки.")
            return
        window = self.window
        if record["kind"] == "document":
            incoming_id = record.get("incoming_document_id")
            if incoming_id is not None:
                window.open_incoming_documents(incoming_id)
                return
            identity = record.get("client_id") or record.get("id")
            if identity is None:
                self.state.setText("У документа нет устойчивого идентификатора; запись не открыта.")
                return
            date = record.get("date")
            start, end = (date, date) if date else ("1900-01-01", "2999-12-31")
            window._selected_expense_identity = identity
            if self.attention["documents"] is not None:
                window._expense_read_cache[(start, end)] = list(self.attention["documents"])
            fields = [window.exp_date_from, window.exp_date_to, window.exp_filter_object,
                      window.exp_filter_unallocated, window.exp_filter_status]
            if hasattr(window, "exp_search"):
                fields.append(window.exp_search)
            blockers = [QSignalBlocker(field) for field in fields]
            window.exp_date_from.setText(start)
            window.exp_date_to.setText(end)
            window.exp_filter_object.setCurrentIndex(0)
            window.exp_filter_unallocated.setChecked(False)
            status = window.exp_filter_status.findData("all")
            if status >= 0:
                window.exp_filter_status.setCurrentIndex(status)
            if hasattr(window, "exp_search"):
                window.exp_search.clear()
            del blockers
            window.table_supplier_documents.clearSelection()
            window.table_supplier_documents.setCurrentCell(-1, -1)
            window.tabs.setCurrentWidget(window.tab_expenses)
            window.load_expense_documents()
            table = window.table_supplier_documents
            for row in range(table.rowCount()):
                item = table.item(row, 0)
                document = item.data(Qt.ItemDataRole.UserRole) if item else None
                if isinstance(document, dict) and identity in (document.get("id"), document.get("client_id")):
                    table.selectRow(row)
                    table.setCurrentCell(row, 1)
                    table.scrollToItem(table.item(row, 1))
                    self.hide()
                    return
            table.clearSelection()
            table.setCurrentCell(-1, -1)
            self.state.setText("Документ не найден в реестре; другая запись не открыта.")
            return

        period = self.attention["periods"]["hours"] if record["kind"] == "employee" else (
            record.get("date"), record.get("date"))
        start, end = (QDate.fromString(value or "", "yyyy-MM-dd") for value in period)
        if not start.isValid() or not end.isValid():
            self.state.setText("Дата записи недоступна; переход не выполнен.")
            return
        name = " ".join(str(record.get("employee") or "").split()).casefold()
        employee_matches = [index for index in range(1, window.filter_emp.count())
                            if " ".join(window.filter_emp.itemText(index).split()).casefold() == name]
        if len(employee_matches) != 1:
            self.state.setText("Сотрудник не найден однозначно в журнале; другая запись не открыта.")
            return
        employee_index = employee_matches[0]
        company = self.attention["company"] if record["kind"] == "employee" else ""
        obj = self.attention["object_name"] if record["kind"] == "employee" else ""
        company_matches = [index for index in range(1, window.filter_comp.count())
                           if window.filter_comp.itemText(index) == company] if company else [0]
        if len(company_matches) != 1:
            self.state.setText("Фирма не найдена однозначно в журнале; переход не выполнен.")
            return
        blockers = [QSignalBlocker(field) for field in (
            window.filter_date_from, window.filter_date_to, window.filter_emp,
            window.filter_comp, window.filter_obj, window.filter_status)]
        window.filter_date_from.setDate(start)
        window.filter_date_to.setDate(end)
        window.filter_emp.setCurrentIndex(employee_index)
        company_changed = window.filter_comp.currentIndex() != company_matches[0]
        window.filter_comp.setCurrentIndex(company_matches[0])
        if (company_changed or obj) and hasattr(window, "_refresh_shift_object_filter"):
            try:
                if not self.database:
                    raise ValueError("Не определена база текущего профиля.")
                conn = sqlite3.connect(Path(self.database).resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    window._refresh_shift_object_filter(conn.cursor())
                finally:
                    conn.close()
            except (sqlite3.Error, OSError, ValueError) as error:
                del blockers
                self.state.setText("Не удалось обновить фильтр объектов; переход не выполнен: " + str(error))
                return
        if obj:
            label = f"{obj} · {company}"
            object_matches = [index for index in range(1, window.filter_obj.count())
                              if window.filter_obj.itemText(index) == label
                              and window.filter_obj.itemData(index)]
            if len(object_matches) != 1:
                del blockers
                self.state.setText("Объект не найден однозначно в журнале; переход не выполнен.")
                return
            object_index = object_matches[0]
        else:
            object_index = 0
        window.filter_obj.setCurrentIndex(object_index)
        window.filter_status.setCurrentIndex(0)
        del blockers
        table = window.table_shifts
        table.clearSelection()
        table.setCurrentCell(-1, -1)
        window.load_shifts_history()
        if record["kind"] == "employee":
            table.clearSelection()
            table.setCurrentCell(-1, -1)
            window.tabs.setCurrentWidget(window.tab_hours)
            self.hide()
            return
        identity = record.get("id")
        if not identity:
            table.clearSelection()
            table.setCurrentCell(-1, -1)
            self.state.setText("У смены нет устойчивого идентификатора; другая запись не открыта.")
            return
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            local_identity = f"local:{item.data(Qt.ItemDataRole.UserRole)}" if item else None
            if item and (item.data(Qt.ItemDataRole.UserRole + 1) == identity or local_identity == identity):
                table.selectRow(row)
                table.setCurrentCell(row, 1)
                table.scrollToItem(table.item(row, 1))
                window.tabs.setCurrentWidget(window.tab_hours)
                self.hide()
                return
        table.clearSelection()
        table.setCurrentCell(-1, -1)
        self.state.setText("Смена не найдена в локальном журнале; другая запись не открыта.")

    def quick_action(self, purpose):
        if self.assistant_candidate and purpose not in CANDIDATE_READ_ACTIONS:
            self.failed("В кандидате доступны только чтение, проверки и предпросмотр зарплатного/клиентского PDF.")
            return
        if self.date_from.date() > self.date_to.date():
            self.failed("Начальная дата должна быть не позже конечной.")
            return
        self.result = None
        self.attention = None
        self.table.setVisible(False)
        self.result_heading.clear()
        self.show_all_attention.setVisible(False)
        self.pdf.setEnabled(False)
        self.append_message("Вы", CAPABILITIES[purpose][0])
        self.call("inspect", dict(plan=self.selection(purpose),
                  operation_id=str(uuid.uuid4())))

    def command(self):
        if self.audio is not None:
            self.state.setText("Сначала остановите запись.")
            return
        text = self.input.toPlainText().strip()
        if not text:
            self.state.setText("Напишите или продиктуйте задачу.")
            return
        if self.worker and self.worker.isRunning():
            self.state.setText("Дождитесь ответа на предыдущую задачу.")
            return
        self.result = None
        self.pdf.setEnabled(False)
        self.append_message("Вы", text)
        if assistant_attention.is_attention_request(text):
            self.input.clear()
            self.start_attention(text, already_appended=True)
            return
        if assistant_attention.is_missing_hours_request(text):
            self.input.clear()
            self.start_attention(text, already_appended=True, hours_only=True)
            return
        self.attention = None
        self.table.setVisible(False)
        self.result_heading.clear()
        self.show_all_attention.setVisible(False)
        if self.choices and text.isdecimal() and 1 <= int(text) <= len(self.choices):
            plan = self.context["plan"].copy()
            plan.update({
                key: value for key, value in self.choices[int(text) - 1].items()
                if key != "label"
            })
            self.call("inspect", dict(
                plan=plan, operation_id=str(uuid.uuid4()), context=self.context,
            ))
        else:
            context = self.context or dict(plan=self.selection(), pending="")
            if not context["pending"]:
                context = dict(
                    context,
                    plan=self.selection(context["plan"]["purpose"]),
                    pending="",
                )
            self.context = context
            self.call("command", dict(text=text, context=context, operation_id=str(uuid.uuid4())))
        self.input.clear()

    def open_native(self):
        if self.assistant_candidate:
            self.state.setText("Открытие изменяющих разделов отключено в кандидате.")
            return
        if not self.result or "native_action" not in self.result:
            return
        if self.audio is not None or self.worker and self.worker.isRunning():
            self.state.setText("Сначала завершите текущую запись или запрос.")
            return
        try:
            if self.result["native_action"] == "mail_prepare":
                self.prepare_mail()
            else:
                self.append_message("SmartReport", native_action(self.window, self.result, self))
        except (DesktopActionError, ValueError) as error:
            self.failed(str(error))
        except sqlite3.Error:
            self.failed("Не удалось открыть штатный раздел текущего профиля. Ничего не сохранено помощником.")

    def record_details(self):
        if self.attention is not None:
            self.open_attention_record()
            return
        if not self.result or "table" in self.result:
            return
        index = self.table.currentRow()
        if not 0 <= index < len(self.result["records"]):
            self.state.setText("Выберите строку результата.")
            return
        row = self.result["records"][index]
        dialog = QDialog(self)
        dialog.setWindowTitle("Запись SmartReport — только просмотр")
        dialog.resize(700, 500)
        layout = QVBoxLayout(dialog)
        details = QTextEdit()
        details.setReadOnly(True)
        details.setPlainText(f'{row["date"]} — {row["employee"]}\n{row["company"]} / {row["object_name"]}\n'
                             f'Отработано/указано: {row["hours"]:g} ч; выезды: {row["trips"]:g}\n'
                             f'Начало: {row["start_time"] or "не задано"}; конец: {row["end_time"] or "не задано"}\n'
                             f'Источник цен: {row["price_source"]}\n'
                             + "\n".join(row["warnings"]) + "\n\nПравки здесь не сохраняются.")
        layout.addWidget(details)
        dialog.exec()

    def save_settings(self):
        self.call("status", dict(enabled=self.enabled.isChecked(), limit_usd=self.limit.value()))

    def report(self):
        if self.assistant_candidate and (
                not self.result or self.result["plan"]["purpose"] not in CANDIDATE_READ_ACTIONS):
            self.state.setText("В кандидате доступны только отчёты и предпросмотр без сохранения изменений.")
            return
        try:
            if self.result:
                open_report(self.window, self.result)
        except (AssistantClientError, ValueError) as error:
            self.append_message("SmartReport", "Отчёт пока не подготовлен: " + str(error))
            QMessageBox.warning(self, "PDF не сформирован", str(error))
        except sqlite3.Error:
            message = "Не удалось прочитать локальную базу для отчёта. PDF не сформирован. Проверьте профиль и синхронизацию."
            self.append_message("SmartReport", message)
            QMessageBox.warning(self, "PDF не сформирован", message)

    def prepare_mail(self):
        if self.assistant_candidate:
            self.state.setText("Подготовка и отправка писем отключены в кандидате.")
            return
        if self.worker and self.worker.isRunning():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Черновик письма — без отправки")
        layout = QFormLayout(dialog)
        recipient, subject, body = QLineEdit(), QLineEdit("SmartReport"), QTextEdit()
        layout.addRow("Один адресат:", recipient)
        layout.addRow("Тема:", subject)
        layout.addRow("Текст:", body)
        choose = QPushButton("Выбрать и проверить PDF…")
        layout.addRow(choose)
        selected = {}

        def select():
            filename, _ = QFileDialog.getOpenFileName(dialog, "Выбрать готовый PDF SmartReport", "", "PDF (*.pdf)")
            if filename:
                selected["filename"] = filename
                choose.setText(Path(filename).name)

        choose.clicked.connect(select)
        prepare = QPushButton("Подготовить, не отправлять")
        layout.addRow(prepare)
        prepare.clicked.connect(dialog.accept)
        if dialog.exec() != QDialog.DialogCode.Accepted or not selected:
            return
        try:
            path = Path(selected["filename"])
            if path.stat().st_size > 1000000:
                raise AssistantClientError("PDF больше 1 МБ. Выберите компактный отчёт.")
            content = path.read_bytes()
            from payroll_preview import PayrollPdfPreviewDialog
            preview = PayrollPdfPreviewDialog(content, path, self)
            preview.setWindowTitle("Точное вложение письма — проверьте до отправки")
            try:
                preview.exec()
            finally:
                preview.release_resources()
        except (ImportError, OSError, ValueError, RuntimeError) as error:
            QMessageBox.warning(self, "Письмо не подготовлено", str(error))
            return
        self.mail_data = dict(to=recipient.text().strip(), subject=subject.text(), body=body.toPlainText(),
                              pdf=base64.b64encode(content).decode("ascii"))
        self.mail_filename = selected["filename"]
        self.confirm_mail.setEnabled(False)
        self.call("mail/prepare", self.mail_data)

    def send_mail(self):
        if self.assistant_candidate:
            self.state.setText("Отправка писем отключена в кандидате.")
            return
        if self.worker and self.worker.isRunning():
            return
        answer = QMessageBox.question(self, "Отправить конкретное письмо?", (
            f'Кому: {self.mail_data["to"]}\nТема: {self.mail_data["subject"]}\n'
            f'{self.mail_data["body"]}\n\nВложение: {self.mail_filename}\n'
            "Будет отправлен именно PDF, загруженный при подготовке. Проверьте его до подтверждения.\n"
            "Разрешаете отправку?"), QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.confirm_mail.setEnabled(False)
            self.call("mail/confirm", dict(draft_id=self.pending_draft["draft_id"], payload=self.mail_data, confirmed=True))

    def instructions(self):
        QMessageBox.information(self, "Официальный OpenAI API", (
            "1. Откройте platform.openai.com под своим аккаунтом. ChatGPT и API оплачиваются отдельно.\n"
            "2. Создайте отдельный Project «SmartReport». В Billing подключите оплату/кредиты API.\n"
            "3. В Project → API keys создайте ключ. Не отправляйте его в чат.\n"
            "4. На сервере Render → Environment добавьте OPENAI_API_KEY. Вводите ключ только в это "
            "защищённое поле. Сохранение переменной может перезапустить сервис; не нажимайте Deploy latest.\n"
            "5. Ключ не вводится в настольное приложение и не возвращается клиентам. Здесь задайте "
            "лимит и включите помощника. При отключённом API обычный учёт продолжает работать.\n\n"
            "Помощник передаёт OpenAI текст команды; голос — только при записи. Не диктуйте реквизиты. "
            "Нужны договорные настройки обработки данных; store=false не означает Zero Data Retention."
        ))

    def record(self):
        if self.audio is not None:
            return self.stop_recording()
        try:
            from PyQt6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices, QtAudio
            device = QMediaDevices.defaultAudioInput()
            self.refresh_microphone_label()
            fmt = QAudioFormat()
            fmt.setSampleRate(16000)
            fmt.setChannelCount(1)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            if device.isNull() or not device.isFormatSupported(fmt):
                raise AssistantClientError("Микрофон PCM mono 16 kHz недоступен. Используйте текстовую команду.")
            self.buffer = QBuffer(self)
            if not self.buffer.open(QIODevice.OpenModeFlag.ReadWrite):
                raise AssistantClientError("Не удалось подготовить запись. Повторите попытку или используйте текст.")
            self.audio = QAudioSource(device, fmt, self)
            self.audio.stateChanged.connect(self._recording_state_changed)
            self.audio.start(self.buffer)
            if self.audio is None:
                return
            if self.audio.error() != QtAudio.Error.NoError:
                raise AssistantClientError("Нет доступа к микрофону. Проверьте разрешение Windows.")
            self.recording_started_at = time.monotonic()
            self.voice.setEnabled(False)
            self.voice_stop.setVisible(True)
            self.voice_cancel.setVisible(True)
            self.voice_level.setValue(0)
            self.recording_time.setText("00:00 / 01:00")
            self.send.setEnabled(False)
            self.state.setText("Идёт запись. Остановите её для распознавания или отмените без отправки.")
            self.timer.start()
        except (ImportError, AssistantClientError, RuntimeError, OSError) as error:
            logging.exception("Assistant microphone recording could not start")
            self._recording_failed(
                str(error) if isinstance(error, AssistantClientError) else
                "Не удалось начать запись. Проверьте подключение микрофона и разрешение Windows."
            )

    def _recording_failed(self, message):
        self._discard_recording()
        self.send.setEnabled(True)
        self.state.setText(message)
        QMessageBox.warning(self, "Запись голоса недоступна", message)

    def _recording_state_changed(self, state):
        from PyQt6.QtMultimedia import QtAudio
        if self.audio is None or state != QtAudio.State.StoppedState:
            return
        error = self.audio.error()
        logging.error("Assistant microphone stopped unexpectedly: %s", error.name)
        self._recording_failed(
            "Запись прервана: микрофон недоступен или отключён. Проверьте подключение и разрешение "
            "Windows для настольных приложений. Запись не отправлена; набранный текст сохранён."
        )

    def update_recording_status(self):
        if self.audio is None:
            return
        elapsed = max(0, int(time.monotonic() - self.recording_started_at))
        self.recording_time.setText(f"{elapsed // 60:02d}:{elapsed % 60:02d} / 01:00")
        raw = bytes(self.buffer.data())[-6400:]
        if raw:
            samples = [sample[0] for sample in struct.iter_unpack("<h", raw[:len(raw) // 2 * 2])]
            if samples:
                peak = max(abs(sample) for sample in samples)
                rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
                db = 20 * math.log10(max(rms, 1) / 32768)
                self.voice_level.setValue(max(0, min(100, int((db + 60) * 100 / 60))))
                self.voice_level.setToolTip(f"Уровень: RMS {db:.0f} dBFS; пик {peak / 32768:.2f}")
        if elapsed >= 59:
            self.stop_recording()

    def voice_context_terms(self):
        terms = [
            self.employee.text().strip(),
            self.company.text().strip(),
            self.object_name.text().strip(),
        ]
        if self.context and isinstance(self.context.get("plan"), dict):
            plan = self.context["plan"]
            terms.extend((plan.get("employee", ""), plan.get("company", ""), plan.get("object_name", "")))
        return list(dict.fromkeys(value for value in terms if value))[:6]

    def _finish_recording_controls(self):
        self.voice.setEnabled(True)
        self.voice_stop.setVisible(False)
        self.voice_cancel.setVisible(False)
        self.voice_level.setValue(0)

    def _discard_recording(self):
        self.timer.stop()
        if self.audio is not None:
            audio = self.audio
            self.audio = None
            audio.stop()
            audio.deleteLater()
        if self.buffer is not None:
            self.buffer.close()
            self.buffer.deleteLater()
            self.buffer = None
        self.recording_started_at = None
        self._finish_recording_controls()

    def cancel_recording(self):
        if self.audio is None:
            return
        self._discard_recording()
        self.send.setEnabled(True)
        self.state.setText("Запись отменена и не отправлена. Уже набранный текст сохранён.")

    def stop_recording(self):
        if self.audio is None:
            return
        self.timer.stop()
        audio = self.audio
        self.audio = None
        audio.stop()
        audio.deleteLater()
        raw = bytes(self.buffer.data())
        self._discard_recording()
        if not raw or len(raw) > 1920000:
            self.send.setEnabled(True)
            self.state.setText(
                "Запись пуста или длилась слишком долго. Проверьте, что микрофон выбран и разрешён, "
                "затем запишите фразу короче минуты."
            )
            return
        sample_count = len(raw) // 2
        samples = [sample[0] for sample in struct.iter_unpack("<h", raw[:sample_count * 2])]
        peak = max((abs(sample) for sample in samples), default=0)
        rms = math.sqrt(sum(sample * sample for sample in samples) / max(1, len(samples)))
        self.voice_warning = ""
        if rms < 100:
            self.voice_warning = (
                "Запись почти беззвучная: проверьте микрофон, расстояние и разрешение Windows."
            )
        elif peak >= 32760:
            self.voice_warning = "В записи возможны перегрузка и искажения; проверьте микрофон."
        stream = io.BytesIO()
        with wave.open(stream, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(raw)
        self.call("transcribe", audio=stream.getvalue(), audio_terms=self.voice_context_terms())

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.state.setText("Дождитесь завершения запроса перед закрытием помощника.")
            event.ignore()
            return
        if self.audio is not None:
            self._discard_recording()
        super().closeEvent(event)


def open_dialog(window):
    dialog = getattr(window, "_assistant_dialog", None)
    if dialog is None:
        dialog = AssistantDialog(window)
        window._assistant_dialog = dialog
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
