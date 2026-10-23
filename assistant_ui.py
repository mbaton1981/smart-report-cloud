"""Optional director panel; network tasks never block the ordinary desktop workflow."""
import io
import base64
import json
import os
from pathlib import Path
import sys
import uuid
import wave
from decimal import Decimal
from urllib.parse import urlsplit

import requests
from PyQt6.QtCore import QBuffer, QByteArray, QDate, QIODevice, QSignalBlocker, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTextEdit, QVBoxLayout, QLineEdit, QFileDialog, QDateEdit,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView,
)

import desktop_auth
from sync_dataset import HEADER, file_dataset, require_response_dataset


class AssistantClientError(ValueError):
    pass


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
    if "transport" in result:
        t = result["transport"]
        lines.extend([t["scope"], f'Проверено: {t["checked"]}; требуют внимания: {t["attention"]}; '
                      f'не проверено (другой вид / Fast pris): {t["excluded"]}.',
                      f'Подытог известных сумм транспорта: {Decimal(t["known_subtotal"]):,.2f} SEK.',
                      "Расхождения перечислены в таблице." if t["attention"] else
                      "В проверенных полях расхождений не найдено." if t["checked"] else
                      "Нет записей, для которых выполнена проверка транспорта.",
                      t["limits"]])
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
        lines.append("Это не итог фактуры: OB, Fast pris и условия договора проверяйте в штатном отчёте.")
        lines.append(result["notice"])
    lines.append(result["source"])
    if not result["records"]:
        lines.append("По выбранным фильтрам на сервере записей нет. Это не проверка других профилей или несинхронизированных данных.")
    return "\n".join(lines)


def result_table(result):
    transport = result["plan"]["purpose"] == "transport_check"
    headers = ["Дата", "Сотрудник", "Фирма / объект", "Часы", "Выезды", "Цена выезда, SEK",
               "Транспорт, SEK", "Проверка / замечания"]
    if transport:
        headers.insert(6, "Справочник, SEK/выезд")
    else:
        headers.insert(4, "Ставка, SEK/ч")
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
        rows.append(cells)
    return headers, rows


def open_report(window, result):
    plan = result["plan"]
    if plan["purpose"] not in {"payroll_pdf", "customer_pdf", "catalog_preview"}:
        raise AssistantClientError("Для PDF попросите зарплатную сводку либо отчёт по фирме и объекту.")
    import sys
    app = sys.modules[window.__class__.__module__]
    conn = app.get_db_connection()
    try:
        conn.execute("PRAGMA query_only=ON")
        filters, values = ["date>=?", "date<=?"], [plan["date_from"], plan["date_to"]]
        # Existing customer reports are whole-object documents, not a single worker subset.
        if plan["purpose"] in {"customer_pdf", "catalog_preview"} and plan["employee"]:
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
        if plan["purpose"] in {"customer_pdf", "catalog_preview"}:
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
        else:
            window.export_invoice_to_pdf()


class AssistantDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window, self.worker, self.result, self.audio = window, None, None, None
        self.database = getattr(sys.modules.get(window.__class__.__module__), "DB_FILE", None)
        self.setWindowTitle("SmartReport — ИИ-помощник")
        self.resize(1000, 740)
        layout = QVBoxLayout(self)
        notice = QLabel("Текст и голос → официальный API. Не вводите пароли, ключи или реквизиты.\n"
                        "Записи и суммы не отправляются модели: она выбирает разрешённую функцию.\n"
                        "Изменения учётных данных заблокированы. Письмо отправляется только отдельной кнопкой подтверждения.")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        form = QFormLayout()
        self.enabled = QCheckBox("Помощник включён на сервере")
        self.limit = QDoubleSpinBox()
        self.limit.setRange(0, 1000)
        self.limit.setDecimals(2)
        self.limit.setValue(10)
        form.addRow(self.enabled)
        form.addRow("Месячный лимит резервов, USD:", self.limit)
        layout.addLayout(form)
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
        layout.addLayout(controls)
        filters = QHBoxLayout()
        today = QDate.currentDate()
        monday = today.addDays(1 - today.dayOfWeek())
        self.date_from, self.date_to = QDateEdit(monday), QDateEdit(monday.addDays(6))
        for field in (self.date_from, self.date_to):
            field.setDisplayFormat("dd.MM.yyyy")
            field.setCalendarPopup(True)
        self.employee = QLineEdit()
        self.employee.setPlaceholderText("Все сотрудники (или точное ФИО)")
        filters.addWidget(QLabel("С:"))
        filters.addWidget(self.date_from)
        filters.addWidget(QLabel("По:"))
        filters.addWidget(self.date_to)
        filters.addWidget(self.employee)
        layout.addLayout(filters)
        shortcuts = QHBoxLayout()
        for text, purpose in (("Показать смены", "search"), ("Проверить ставки и данные", "check"),
                              ("Проверить транспорт", "transport_check")):
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, purpose=purpose: self.quick_action(purpose))
            shortcuts.addWidget(button)
        layout.addLayout(shortcuts)
        hint = QLabel("Кнопки используют даты и сотрудника выше, без обращения к OpenAI. "
                      "Текстовая команда использует собственные даты и фильтры.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.input = QTextEdit()
        self.input.setAcceptRichText(False)
        self.input.setMaximumHeight(100)
        self.input.setPlaceholderText("Например: проверь смены с 2026-10-01 по 2026-10-31 по фирме Renatur")
        layout.addWidget(self.input)
        row = QHBoxLayout()
        self.send = QPushButton("Выполнить текстовую команду")
        self.send.clicked.connect(self.command)
        row.addWidget(self.send)
        self.voice = QPushButton("Записать голос (до 60 с)")
        self.voice.clicked.connect(self.record)
        row.addWidget(self.voice)
        self.pdf = QPushButton("Открыть существующий PDF по результату")
        self.pdf.setEnabled(False)
        self.pdf.clicked.connect(self.report)
        row.addWidget(self.pdf)
        mail = QPushButton("Подготовить письмо с PDF")
        mail.clicked.connect(self.prepare_mail)
        row.addWidget(mail)
        layout.addLayout(row)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumHeight(180)
        layout.addWidget(self.output)
        self.table = QTableWidget()
        self.table.setMinimumHeight(170)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setVisible(False)
        layout.addWidget(self.table)
        self.confirm_mail = QPushButton("Подтвердить отправку подготовленного письма…")
        self.confirm_mail.setEnabled(False)
        self.confirm_mail.clicked.connect(self.send_mail)
        layout.addWidget(self.confirm_mail)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.stop_recording)
        self.call("status")

    def call(self, path, payload=None, audio=None):
        if self.audio is not None:
            self.state.setText("Сначала остановите запись голоса.")
            return
        if self.worker and self.worker.isRunning():
            self.state.setText("Дождитесь завершения текущего запроса; повтор не отправлен.")
            return
        self.send.setEnabled(False)
        self.voice.setEnabled(False)
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

    def failed(self, message):
        self.state.setText(message)
        self.output.setPlainText(message + "\nПредыдущий результат очищен; данные не изменены.")
        self.table.setVisible(False)
        self.result = None
        self.pdf.setEnabled(False)
        self.confirm_mail.setEnabled(False)

    def received(self, data):
        if "clarification" in data:
            self.result = None
            self.pdf.setEnabled(False)
            self.table.setVisible(False)
            self.output.setPlainText(data["clarification"]["question"])
            self.state.setText("Нужен ответ. Выберите параметры и действие либо уточните текстовую команду.")
            if data["clarification"]["field"] == "scope":
                self.employee.setFocus()
            elif data["clarification"]["field"] == "period":
                self.date_from.setFocus()
            else:
                self.input.setFocus()
        elif "status" in data:
            s = data["status"]
            self.enabled.setChecked(s["enabled"])
            self.limit.setValue(s["limit_usd"])
            key = "настроен" if s["key_configured"] else "НЕ настроен"
            self.state.setText(f'Ключ: {key}; модель {s["model"]}; {s["month"]}: '
                               f'резерв {s["reserved_usd"]:.4f} / {s["limit_usd"]:.2f} USD.\n{s["notice"]}')
        elif "text" in data:
            self.input.setPlainText(data["text"])
            self.state.setText("Речь распознана. Проверьте ФИО, даты и числа, затем нажмите «Выполнить».")
        elif "actions" in data:
            self.table.setVisible(False)
            names = {"search": "Поиск смен", "check": "Проверка данных", "transport_check": "Проверка транспорта",
                     "text": "Текстовая команда", "voice": "Голос", "settings": "Настройки"}
            outcomes = {"reserved": "Начато", "routed": "Действие определено", "read_completed": "Просмотр завершён",
                        "failed": "Не выполнено", "clarification": "Запрошено уточнение"}
            self.output.setPlainText("\n".join(
                f'{r["created_at"]} — {names.get(r["action"], "Действие помощника")}: '
                f'{outcomes.get(r["outcome"], r["outcome"])}' for r in data["actions"]) or "Журнал пуст.")
        elif "draft" in data:
            self.pending_draft = data["draft"]
            self.output.setPlainText("Письмо подготовлено, НЕ отправлено.\n"
                                     f'Кому: {self.mail_data["to"]}\nТема: {self.mail_data["subject"]}\n'
                                     f'{self.mail_data["body"]}\n\nPDF: {self.mail_filename}\n'
                                     f'SHA256: {self.pending_draft["sha256"]}\n'
                                     + self.pending_draft["notice"])
            self.state.setText("Проверьте письмо и вложение. Кнопка подтверждения доступна ниже.")
            self.confirm_mail.setEnabled(True)
        elif "mail" in data:
            self.confirm_mail.setEnabled(False)
            self.output.setPlainText(data["mail"]["notice"] + "\nMessage-ID: " + data["mail"]["message_id"])
        elif "result" in data:
            self.result = data["result"]
            self.output.setPlainText(result_text(self.result))
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
            self.table.setVisible(True)
            self.pdf.setEnabled(self.result["plan"]["purpose"] in {"payroll_pdf", "customer_pdf", "catalog_preview"})
            self.pdf.setText("Открыть штатный просмотр изменений…" if self.result["plan"]["purpose"] == "catalog_preview"
                             else "Открыть существующий PDF по результату")
            self.state.setText("Готово. Результат получен из программы, не сочинён моделью. Данные не изменены.")

    def quick_action(self, purpose):
        if self.date_from.date() > self.date_to.date():
            self.failed("Начальная дата должна быть не позже конечной.")
            return
        self.result = None
        self.pdf.setEnabled(False)
        self.call("inspect", dict(plan=dict(purpose=purpose,
                  date_from=self.date_from.date().toString("yyyy-MM-dd"),
                  date_to=self.date_to.date().toString("yyyy-MM-dd"),
                  employee=self.employee.text().strip(), company="", object_name=""),
                  operation_id=str(uuid.uuid4())))

    def command(self):
        if self.audio is not None:
            self.state.setText("Сначала остановите запись.")
            return
        self.result = None
        self.pdf.setEnabled(False)
        self.call("command", dict(text=self.input.toPlainText(), operation_id=str(uuid.uuid4())))

    def save_settings(self):
        self.call("status", dict(enabled=self.enabled.isChecked(), limit_usd=self.limit.value()))

    def report(self):
        try:
            if self.result:
                open_report(self.window, self.result)
        except (AssistantClientError, ValueError) as error:
            QMessageBox.warning(self, "PDF не сформирован", str(error))

    def prepare_mail(self):
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
