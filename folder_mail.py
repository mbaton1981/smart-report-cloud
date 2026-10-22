"""Date-scoped manual read-only intake, with durable completion and safe retry."""
from __future__ import annotations

import hashlib
import imaplib
import re
import ssl
from datetime import date, timedelta
from email import policy
from email.parser import BytesParser
from threading import Event
from time import monotonic

from invoice_processing import recognize_invoice
from mail_import import HOST, ADDRESS, MAX_MESSAGE_BYTES
from offline_review import ReviewError

FOLDER = "INBOX"
RECENT_DAYS = 14
IMPORT_START_DATE = date(2026, 11, 1)


class FolderError(ReviewError):
    pass


def checked(response, stage):
    if response[0] != "OK":
        raise FolderError(stage)
    return response[1]


def recent_period(today=None):
    end = today or date.today()
    return max(IMPORT_START_DATE, end - timedelta(days=RECENT_DAYS - 1)), end


def checked_period(start, end):
    if not isinstance(start, date) or not isinstance(end, date) or start > end or end > date.today():
        raise FolderError("period")
    return start, end


def imap_date(value):
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{value.day:02d}-{months[value.month - 1]}-{value.year}"


def import_folder(store, password, *, start=None, end=None, should_stop=None, progress=None,
                  client_factory=imaplib.IMAP4_SSL, recognizer=recognize_invoice):
    if date.today() < IMPORT_START_DATE:
        raise FolderError("import_not_started")
    start, end = checked_period(*(recent_period() if start is None and end is None else (start, end)))
    start = max(start, IMPORT_START_DATE)
    if end < start:
        raise FolderError("before_import_start")
    report = {"messages": 0, "new_drafts": 0, "duplicates": 0, "without_pdf": 0,
              "recognition_failed": 0, "skipped": 0, "total": 0, "completed": 0,
              "stopped": False, "phase": "connecting",
              "start": start.isoformat(), "end": end.isoformat()}
    last_progress = [None, 0.0]
    def notify(phase):
        report["phase"] = phase
        if progress:
            now = monotonic()
            if phase != last_progress[0] or now - last_progress[1] >= 0.1:
                progress(dict(report))
                last_progress[:] = [phase, now]

    def stopping():
        if should_stop and should_stop():
            report["stopped"] = True
            notify("stopped")
            return True
        return False

    client = None
    try:
        notify("connecting")
        if stopping():
            return report
        client = client_factory(HOST, 993, ssl_context=ssl.create_default_context(), timeout=30)
        checked(client.login(ADDRESS, password), "auth")
        checked(client.select('"' + FOLDER + '"', readonly=True), "folder")
        epoch_data = client.response("UIDVALIDITY")[1]
        if not epoch_data or not epoch_data[0] or not epoch_data[0].isdigit():
            raise FolderError("epoch_missing")
        epoch = epoch_data[0].decode("ascii")
        previous = store.conn.execute("SELECT epoch FROM mail_epoch WHERE folder=?", (FOLDER,)).fetchone()
        if previous and previous[0] != epoch:
            raise FolderError("epoch_changed")
        with store.conn:
            store.conn.execute("INSERT OR IGNORE INTO mail_epoch VALUES (?,?)", (FOLDER, epoch))
        notify("searching")
        found = checked(client.uid("SEARCH", None, "SINCE", imap_date(start),
                                   "BEFORE", imap_date(end + timedelta(days=1))), "search")
        if len(found) != 1 or not isinstance(found[0], bytes):
            raise FolderError("search")
        try:
            uids = sorted(set(int(item) for item in found[0].split()))
        except ValueError:
            raise FolderError("search") from None
        if any(not 1 <= uid <= 4294967295 for uid in uids):
            raise FolderError("search")
        report["total"] = len(uids)
        notify("processing")
        for uid in uids:
            if stopping():
                return report
            old = store.conn.execute(
                "SELECT 1 FROM mail_messages WHERE folder=? AND epoch=? AND uid=?",
                (FOLDER, epoch, uid)).fetchone()
            if old:
                report["skipped"] += 1
                report["completed"] += 1
                notify("processing")
                continue
            report["messages"] += 1
            notify("downloading")
            metadata = checked(client.uid("FETCH", str(uid), "(UID RFC822.SIZE)"), "size")
            headers = [item for item in metadata if isinstance(item, bytes) and b"RFC822.SIZE" in item]
            size = re.search(rb"\bRFC822.SIZE\s+(\d+)", headers[0]) if len(headers) == 1 else None
            if not size or int(size[1]) > MAX_MESSAGE_BYTES:
                raise FolderError("size")
            if stopping():
                return report
            body = checked(client.uid("FETCH", str(uid), "(UID BODY.PEEK[])"), "fetch")
            parts = [item for item in body if isinstance(item, tuple) and len(item) == 2]
            if (len(parts) != 1 or not all(isinstance(item, bytes) for item in parts[0])
                    or re.findall(rb"\bUID\s+(\d+)\b", parts[0][0]) != [str(uid).encode()]):
                raise FolderError("fetch")
            raw = parts[0][1]
            if not raw or len(raw) > MAX_MESSAGE_BYTES:
                raise FolderError("size")
            sha = hashlib.sha256(raw).hexdigest()
            message = BytesParser(policy=policy.default).parsebytes(raw)
            pdf_count = 0
            failed = False
            for part in message.walk():
                if part.is_multipart():
                    continue
                filename = part.get_filename() or ""
                if part.get_content_type() != "application/pdf" and not filename.lower().endswith(".pdf"):
                    continue
                if stopping():
                    return report
                content = part.get_payload(decode=True)
                if not content or len(content) > MAX_MESSAGE_BYTES:
                    raise FolderError("pdf_invalid")
                pdf_count += 1
                digest = hashlib.sha256(content).hexdigest()
                existing = store.conn.execute("SELECT id FROM documents WHERE sha256=?", (digest,)).fetchone()
                if existing:
                    document_id = existing[0]
                    if (store.base(document_id).get("_recognition_state") in ("pending", "failed")
                            and store.latest(document_id)[0] == 0 and not store.card(document_id)):
                        notify("recognizing")
                        result = store.recognize_document(document_id, recognizer)
                        if result.get("state") == "failed":
                            failed = True
                            report["recognition_failed"] += 1
                    report["duplicates"] += 1
                    continue
                # Persist a recoverable draft before OCR; failure never discards the original.
                store.add_pdf(content, filename or digest + ".pdf", {"fields": {}, "state": "pending"})
                document_id = store.conn.execute("SELECT id FROM documents WHERE sha256=?", (digest,)).fetchone()[0]
                report["new_drafts"] += 1
                if stopping():
                    return report
                notify("recognizing")
                result = store.recognize_document(document_id, recognizer)
                if result.get("state") == "failed":
                    failed = True
                    report["recognition_failed"] += 1
            if not pdf_count:
                report["without_pdf"] += 1
            if stopping():
                return report
            if not failed:
                with store.conn:
                    store.conn.execute("INSERT OR IGNORE INTO mail_messages VALUES (?,?,?,?)",
                                       (FOLDER, epoch, uid, sha))
            report["completed"] += 1
            notify("processing")
        notify("finished")
        return report
    finally:
        password = ""
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                # The read-only import's result remains valid if the server closed the session.
                pass


ERRORS = {
    "import_not_started": "Получение фактур разрешено с 01.11.2026 включительно. Подключение к почте не выполнялось.",
    "before_import_start": "Письма до 01.11.2026 не загружаются. Выберите период с этой даты.",
    "auth": "Сервер отклонил вход. Проверьте пароль приложения; настройки безопасности не ослабляются.",
    "folder": "Сервер не открыл основную папку INBOX. Проверьте доступ к входящим подключённого ящика.",
    "epoch_missing": "Сервер не вернул UIDVALIDITY папки. Импорт остановлен.",
    "epoch_changed": "UIDVALIDITY папки изменился. Автоматический сброс запрещён; нужна сверка очереди.",
    "search": "Сервер не вернул корректный список UID только выбранной папки.",
    "size": "Сервер не вернул размер письма или письмо превышает 30 МБ. Импорт остановлен.",
    "fetch": "Не удалось безопасно получить выбранный UID через BODY.PEEK[].",
    "message_changed": "Содержимое ранее полученного UID изменилось. Импорт остановлен для ручной сверки.",
    "pdf_invalid": "PDF-вложение пустое или превышает 30 МБ. Письмо не изменено.",
    "period": "Укажите корректный период: начало не позже конца, конец не позже сегодняшнего дня.",
}


def report_text(report):
    return (
        f"INBOX · {report['start']} — {report['end']}. "
        f"Проверено: {report['completed']}/{report['total']}; "
        f"пропущено обработанных: {report['skipped']}; "
        f"новых черновиков: {report['new_drafts']}; повторов PDF: {report['duplicates']}; "
        f"без PDF: {report['without_pdf']}; ошибок OCR: {report['recognition_failed']}. "
        "Расходы не создавались.")


def archive_period(parent):
    from PyQt6.QtCore import QDate
    from PyQt6.QtWidgets import QDateEdit, QDialog, QDialogButtonBox, QFormLayout, QMessageBox
    if date.today() < IMPORT_START_DATE:
        QMessageBox.information(parent, "Получение с 01.11.2026", ERRORS["import_not_started"])
        return None
    today = date.today()
    qt_today = QDate(today.year, today.month, today.day)
    dialog = QDialog(parent)
    dialog.setWindowTitle("Загрузить архив — период INBOX")
    layout = QFormLayout(dialog)
    start, end = QDateEdit(), QDateEdit()
    start.setObjectName("archive_start")
    end.setObjectName("archive_end")
    for field in (start, end):
        field.setCalendarPopup(True)
        field.setDisplayFormat("yyyy-MM-dd")
        field.setMaximumDate(qt_today)
        field.setMinimumDate(QDate(IMPORT_START_DATE.year, IMPORT_START_DATE.month, IMPORT_START_DATE.day))
    end.setDate(max(qt_today.addDays(-RECENT_DAYS),
                    QDate(IMPORT_START_DATE.year, IMPORT_START_DATE.month, IMPORT_START_DATE.day)))
    start.setDate(max(end.date().addMonths(-1),
                      QDate(IMPORT_START_DATE.year, IMPORT_START_DATE.month, IMPORT_START_DATE.day)))
    layout.addRow("Дата получения письма, с:", start)
    layout.addRow("По (включительно):", end)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addRow(buttons)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    try:
        period = checked_period(start.date().toPyDate(), end.date().toPyDate())
    except FolderError:
        QMessageBox.warning(parent, "Архив не запущен", ERRORS["period"])
        return None
    if QMessageBox.question(
            parent, "Подтверждение загрузки архива",
            f"Загрузить ранее не обработанные письма INBOX за {period[0]} — {period[1]}?\n"
            "Обработанные письма будут пропущены. Существующие документы не изменяются.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
        return None
    return period


def create_import_dialog(window, start, end, *, archive=False):
    from PyQt6.QtCore import QObject, pyqtSignal
    from PyQt6.QtWidgets import QProgressBar, QPushButton
    from incoming_documents import IncomingStore
    from limited_mail_ui import PasswordDialog, ERROR_MESSAGES

    checked_period(start, end)
    if date.today() < IMPORT_START_DATE:
        raise FolderError("import_not_started")
    if start < IMPORT_START_DATE:
        raise FolderError("before_import_start")
    ERROR_MESSAGES.update(ERRORS)

    class Progress(QObject):
        updated = pyqtSignal(dict)

    class FolderDialog(PasswordDialog):
        def __init__(self):
            self.stop_event = Event()
            self.report = None
            self.successful = False
            super().__init__(self.operation, offline=False, parent=window)
            self.progress_signal = Progress(self)
            self.progress_signal.updated.connect(self.show_progress)
            self.setWindowTitle("SmartReport — загрузить архив INBOX" if archive
                                else "SmartReport — получить документы из INBOX")
            self.notice.setText(
                f"Ящик: {ADDRESS}\nINBOX · {start} — {end} (включительно, по дате получения).\n"
                + ("Архив: выбранный период.\n" if archive else f"Только последние {RECENT_DAYS} дней.\n")
                + "Обработанные письма пропускаются без скачивания и OCR.\n"
                "EXAMINE + BODY.PEEK: письма не меняются. Расписание выключено.")
            self.notice.setText(self.notice.text() + "\nПисьма до 01.11.2026 не загружаются.")
            self.buttons.button(self.buttons.StandardButton.Ok).setText(
                "Загрузить архив" if archive else "Получить документы")
            self.progress_bar = QProgressBar()
            self.progress_bar.setRange(0, 1)
            self.layout().insertWidget(self.layout().count() - 1, self.progress_bar)
            self.stop_button = QPushButton("Остановить импорт")
            self.stop_button.setEnabled(False)
            self.stop_button.clicked.connect(self.request_stop)
            self.layout().insertWidget(self.layout().count() - 1, self.stop_button)

        def operation(self, password):
            store = IncomingStore(window.store.root, window.store.host)
            try:
                report = import_folder(store, password, start=start, end=end,
                                       should_stop=self.stop_event.is_set,
                                       progress=self.progress_signal.updated.emit)
                self.progress_signal.updated.emit(report)
            finally:
                store.close()

        def accept(self):
            if self.worker is not None:
                return
            self.stop_event.clear()
            self.successful = False
            self.report = None
            super().accept()
            self.stop_button.setEnabled(self.worker is not None)

        def show_progress(self, report):
            self.report = report
            self.progress_bar.setRange(0, max(1, report["total"]))
            self.progress_bar.setValue(report["completed"])
            phases = {"connecting": "Соединение", "searching": "Поиск писем выбранного периода",
                      "downloading": "Получение письма", "recognizing": "Распознавание PDF",
                      "processing": "Обработка", "stopped": "Остановлено", "finished": "Завершено"}
            prefix = ("Остановка запрошена — ожидается завершение текущего запроса или OCR"
                      if self.stop_event.is_set() else phases[report["phase"]])
            self.status.setText(prefix + ". " + report_text(report))

        def request_stop(self):
            self.stop_event.set()
            self.stop_button.setEnabled(False)
            self.status.setText(
                "Остановка запрошена. Дождитесь завершения текущего запроса или OCR; "
                "сохранённые PDF останутся, незавершённое письмо можно повторить.")

        def show_outcome(self, code):
            self.successful = code == "success"
            super().show_outcome(code)
            if self.report:
                prefix = ("Остановлено безопасно" if self.report["stopped"] else
                          "Завершено; письма с ошибками OCR будут повторены" if self.successful
                          and self.report["recognition_failed"] else "Готово" if self.successful
                          else self.status.text() + " Незавершённое письмо будет повторено")
                self.status.setText(prefix + ". " + report_text(self.report))

        def operation_finished(self):
            super().operation_finished()
            self.stop_button.setEnabled(False)
            window.reload_documents()
            if self.report:
                window.status.setText(self.status.text())

        def reject(self):
            if self.worker is not None:
                self.clear_password()
                self.request_stop()
                return
            super().reject()

        def closeEvent(self, event):
            if self.worker is not None:
                self.clear_password()
                self.request_stop()
                event.ignore()
                return
            super().closeEvent(event)

    return FolderDialog()


def open_import_dialog(window, *, archive=False):
    if date.today() < IMPORT_START_DATE:
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.information(window, "Получение с 01.11.2026", ERRORS["import_not_started"])
        return
    existing = getattr(window, "_mail_import_dialog", None)
    if existing is not None and existing.isVisible():
        existing.raise_()
        existing.activateWindow()
        return
    if not window.keep_unsaved():
        return
    period = archive_period(window) if archive else recent_period()
    if period is None:
        return
    dialog = create_import_dialog(window, *period, archive=archive)
    window._mail_import_dialog = dialog
    dialog.show()
