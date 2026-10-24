"""Optional director panel; network tasks never block the ordinary desktop workflow."""
import io
import base64
import json
import os
import sqlite3
from pathlib import Path
import sys
import uuid
import wave
from decimal import Decimal
from urllib.parse import urlsplit

import requests
from PyQt6.QtCore import QBuffer, QDate, QIODevice, QSignalBlocker, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTextEdit, QVBoxLayout, QLineEdit, QFileDialog, QDateEdit,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView, QComboBox,
)

import desktop_auth
from sync_dataset import HEADER, file_dataset, require_response_dataset
from assistant_actions import CAPABILITIES, REPORT_ACTIONS
from assistant_desktop import DesktopActionError, local_result, native_action


class AssistantClientError(ValueError):
    pass


CANDIDATE_READ_ACTIONS = frozenset({
    "search", "hours_summary", "check", "anomalies", "transport_check", "explain",
    "employees", "objects", "payroll_pdf", "customer_pdf", "journal_pdf",
})
CANDIDATE_REQUESTS = frozenset({"status", "capabilities", "command", "inspect", "transcribe"})


def request_api(path, payload=None, audio=None, database=None):
    base = desktop_auth.server_url().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}):
        raise AssistantClientError("Помощнику требуется HTTPS.")
    token = desktop_auth.current_token()
    if not token:
        raise AssistantClientError("Выполните личный вход администратора. Общий ключ синхронизации не подходит.")
    database = database or os.environ.get("SMARTREPORT_DB_FILE")
    if not database:
        raise AssistantClientError("Не определена база текущего окна SmartReport. Закройте и откройте помощника из программы.")
    dataset = file_dataset(database)
    if not dataset:
        raise AssistantClientError("Нет идентификатора набора данных текущей базы. Сначала выполните штатную синхронизацию.")
    headers = {"Authorization": "Bearer " + token, HEADER: dataset}
    if audio is not None:
        headers.update({"Content-Type": "audio/wav", "X-Operation-ID": str(uuid.uuid4())})
    try:
        response = requests.request("POST" if payload is not None or audio is not None else "GET",
                                    base + "/api/assistant/" + path, headers=headers, json=payload,
                                    data=audio, timeout=(10, 75), allow_redirects=False)
        require_response_dataset(response, dataset)
        if response.status_code in (404, 405):
            raise AssistantClientError("На сервере ещё нет помощника. Обычная программа работает; требуется обновление сервера.")
        if response.status_code in (401, 403):
            raise AssistantClientError("Нет доступа. Нужен действующий личный вход администратора.")
        if response.status_code not in (200, 400, 413):
            raise AssistantClientError("Сервер помощника недоступен. Обычная программа продолжает работать.")
        data = response.json()
        if data.get("ok") is not True:
            # Server errors are controlled messages; never show raw HTTP/provider content.
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

    def __init__(self, path, payload=None, audio=None, parent=None, database=None):
        super().__init__(parent)
        self.path, self.payload, self.audio = path, payload, audio
        self.database = database

    def run(self):
        try:
            self.ready.emit(request_api(self.path, self.payload, self.audio, self.database))
        except AssistantClientError as error:
            self.failed.emit(str(error))


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


def open_report(window, result):
    plan = result["plan"]
    if plan["purpose"] not in REPORT_ACTIONS:
        raise AssistantClientError("Для PDF попросите зарплатную сводку либо отчёт по фирме и объекту.")
    import sys
    app = sys.modules[window.__class__.__module__]
    conn = app.get_db_connection()
    try:
        conn.execute("PRAGMA query_only=ON")
        filters, values = ["date>=?", "date<=?"], [plan["date_from"], plan["date_to"]]
        # Existing customer reports are whole-object documents, not a single worker subset.
        if plan["purpose"] in {"customer_pdf", "journal_pdf", "catalog_preview"} and plan["employee"]:
            raise AssistantClientError("Клиентский PDF включает весь объект. Повторите команду без фильтра сотрудника.")
        for name, value in (("employee", plan["employee"]), ("company", plan["company"]), ("object_name", plan["object_name"])):
            if value:
                filters.append(name + "=?")
                values.append(value)
        from customer_pricing import row_prices
        has_segments = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ob_segments'").fetchone()
        local = {}
        for raw in conn.execute("SELECT * FROM shifts WHERE " + " AND ".join(filters), values):
            row = dict(raw)
            times = conn.execute("SELECT start_time,end_time,hours FROM ob_segments WHERE shift_id=? ORDER BY start_time,end_time,hours",
                                 (row["sync_id"],)).fetchall() if has_segments else []
            local[row["sync_id"]] = dict(hours=row["hours"], prices=row_prices(row), trips=row["transport"],
                                         entry_type=row["entry_type"], start_time=row.get("start_time"),
                                         end_time=row.get("end_time"), invoiced=bool(row["invoiced"]),
                                         intervals=[dict(t) for t in times])
        fields = ("hours", "prices", "trips", "entry_type", "start_time", "end_time", "invoiced", "intervals")
        remote = {r["id"]: {key: r[key] for key in fields} for r in result["records"]}
        if local != remote:
            raise AssistantClientError("Локальные записи отличаются от серверной выборки. Сначала синхронизируйте и повторите команду.")
        if plan["purpose"] in {"customer_pdf", "journal_pdf", "catalog_preview"}:
            obj = result["object"]
            if obj is None:
                raise AssistantClientError("Для клиентского PDF укажите точную фирму и объект.")
            count = conn.execute("SELECT COUNT(*) FROM objects WHERE company=? AND name=?",
                                 (obj["company"], obj["name"])).fetchone()[0]
            if count != 1:
                raise AssistantClientError("Нет однозначного локального объекта. Сначала синхронизируйте справочник.")
    finally:
        conn.close()
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
        self.context = None
        self.choices = []
        self.capability_data = []
        self.auto_report = False
        self.workflow_results = []
        app_module = sys.modules.get(window.__class__.__module__)
        self.assistant_candidate = bool(getattr(app_module, "ASSISTANT_CANDIDATE", False))
        self.database = getattr(app_module, "DB_FILE", None)
        self.setWindowTitle("SmartReport — ИИ-помощник")
        self.resize(1000, 740)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMinMaxButtonsHint)
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)
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
        self.diagnostics.resize(800, 600)
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
        filters = QHBoxLayout()
        today = QDate.currentDate()
        monday = today.addDays(1 - today.dayOfWeek())
        self.date_from, self.date_to = QDateEdit(monday), QDateEdit(monday.addDays(6))
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
        shortcuts = QHBoxLayout()
        for text, purpose in (("Кто сколько отработал", "hours_summary"), ("Дубли и пропущенные ставки", "anomalies"),
                              ("Проверить транспорт", "transport_check")):
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, purpose=purpose: self.quick_action(purpose))
            shortcuts.addWidget(button)
        layout.addLayout(shortcuts)
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
        self.voice = QPushButton("Записать голос (до 60 с)")
        self.voice.clicked.connect(self.record)
        row.addWidget(self.voice)
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
        layout.addLayout(tools)
        self.native = QPushButton("Открыть штатную форму")
        self.native.setEnabled(False)
        self.native.clicked.connect(self.open_native)
        tools.addWidget(self.native)
        self.open_records = QPushButton("Открыть выбранную запись")
        self.open_records.setEnabled(False)
        self.open_records.clicked.connect(self.record_details)
        tools.addWidget(self.open_records)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(150)
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
        if self.assistant_candidate:
            self.native.hide()
            self.mail_button.hide()
            self.confirm_mail.hide()
            self.native.setEnabled(False)
            self.mail_button.setEnabled(False)
            self.confirm_mail.setEnabled(False)
            self.diagnostics.hide()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.stop_recording)
        self.call("status")

    def append_message(self, role, text):
        cursor = self.output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(("\n\n" if self.output.toPlainText() else "") + role + ":\n" + text)
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    def new_conversation(self):
        if self.audio is not None or self.worker and self.worker.isRunning():
            self.state.setText("Сначала завершите запись или запрос.")
            return
        self.context = None
        self.result = None
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

    def call(self, path, payload=None, audio=None):
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
        self.worker = RequestWorker(path, payload, audio, self, database=self.database)
        self.worker.ready.connect(self.received)
        self.worker.failed.connect(self.failed)
        self.worker.finished.connect(self.finished)
        self.worker.start()

    def finished(self):
        if self.worker is not None:
            self.worker.audio = None
        self.send.setEnabled(True)
        self.voice.setEnabled(True)
        self.workflow_choice.setEnabled(True)
        self.native.setEnabled(bool(self.result and "native_action" in self.result))
        self.open_records.setEnabled(bool(self.result and self.result["records"] and "table" not in self.result))
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
            self.context = dict(plan=pending_plan, pending=clarification["field"])
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
            self.input.setPlainText(data["text"])
            self.state.setText("Речь распознана. Проверьте ФИО, даты и числа, затем нажмите «Отправить».")
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
            self.context = dict(plan=plan, pending="")
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
            self.table.setColumnCount(len(headers))
            self.table.setHorizontalHeaderLabels(headers)
            self.table.setRowCount(len(rows))
            for index, cells in enumerate(rows):
                for column, value in enumerate(cells):
                    self.table.setItem(index, column, QTableWidgetItem(value))
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

    def quick_action(self, purpose):
        if self.assistant_candidate and purpose not in CANDIDATE_READ_ACTIONS:
            self.failed("В кандидате доступны только чтение, проверки и предпросмотр зарплатного/клиентского PDF.")
            return
        if self.date_from.date() > self.date_to.date():
            self.failed("Начальная дата должна быть не позже конечной.")
            return
        self.result = None
        self.pdf.setEnabled(False)
        self.append_message("Вы", CAPABILITIES[purpose][0])
        self.call("inspect", dict(plan=self.selection(purpose),
                  operation_id=str(uuid.uuid4())))

    def command(self):
        if self.audio is not None:
            self.state.setText("Сначала остановите запись.")
            return
        self.result = None
        self.pdf.setEnabled(False)
        text = self.input.toPlainText().strip()
        if not text:
            self.state.setText("Напишите или продиктуйте задачу.")
            return
        if self.worker and self.worker.isRunning():
            self.state.setText("Дождитесь ответа на предыдущую задачу.")
            return
        self.append_message("Вы", text)
        if self.choices and text.isdecimal() and 1 <= int(text) <= len(self.choices):
            plan = self.context["plan"].copy()
            plan.update({
                key: value for key, value in self.choices[int(text) - 1].items()
                if key != "label"
            })
            self.call("inspect", dict(plan=plan, operation_id=str(uuid.uuid4())))
        else:
            context = self.context or dict(plan=self.selection(), pending="")
            if not context["pending"]:
                context = dict(plan=self.selection(context["plan"]["purpose"]), pending="")
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
            from PyQt6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
            device = QMediaDevices.defaultAudioInput()
            fmt = QAudioFormat()
            fmt.setSampleRate(16000)
            fmt.setChannelCount(1)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            if device.isNull() or not device.isFormatSupported(fmt):
                raise AssistantClientError("Микрофон PCM mono 16 kHz недоступен. Используйте текстовую команду.")
            self.buffer = QBuffer(self)
            self.buffer.open(QIODevice.OpenModeFlag.ReadWrite)
            self.audio = QAudioSource(device, fmt, self)
            self.audio.start(self.buffer)
            if self.audio.error().value:
                raise AssistantClientError("Нет доступа к микрофону. Проверьте разрешение Windows.")
            self.voice.setText("Остановить и распознать")
            self.send.setEnabled(False)
            self.state.setText("Идёт запись. После остановки аудио будет отправлено OpenAI; до 60 секунд.")
            self.timer.start(59000)
        except (ImportError, AssistantClientError) as error:
            if self.audio is not None:
                self.audio.stop()
                self.audio.deleteLater()
                self.audio = None
            if hasattr(self, "buffer"):
                self.buffer.close()
            self.send.setEnabled(True)
            self.state.setText(str(error))

    def stop_recording(self):
        if self.audio is None:
            return
        self.timer.stop()
        self.audio.stop()
        self.audio.deleteLater()
        self.audio = None
        raw = bytes(self.buffer.data())
        self.buffer.close()
        self.buffer.deleteLater()
        self.voice.setText("Записать голос (до 60 с)")
        if not raw or len(raw) > 1920000:
            self.failed("Запись пуста или слишком длинная. Используйте текст или запишите ещё раз.")
            self.finished()
            return
        stream = io.BytesIO()
        with wave.open(stream, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(raw)
        self.call("transcribe", audio=stream.getvalue())

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.state.setText("Дождитесь завершения запроса перед закрытием помощника.")
            event.ignore()
            return
        if self.audio is not None:
            self.timer.stop()
            self.audio.stop()
            self.audio.deleteLater()
            self.audio = None
        super().closeEvent(event)


def open_dialog(window):
    dialog = getattr(window, "_assistant_dialog", None)
    if dialog is None:
        dialog = AssistantDialog(window)
        window._assistant_dialog = dialog
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
