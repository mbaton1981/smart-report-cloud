"""Manual read-only import from the one explicitly approved folder."""
from __future__ import annotations

import hashlib
import imaplib
import re
import ssl
from email import policy
from email.parser import BytesParser

from invoice_processing import recognize_invoice
from mail_import import HOST, ADDRESS, MAX_MESSAGE_BYTES
from offline_review import ReviewError

FOLDER = "INBOX.SmartReport-Test"


class FolderError(ReviewError):
    pass


def checked(response, stage):
    if response[0] != "OK":
        raise FolderError(stage)
    return response[1]


def import_folder(store, password, *, client_factory=imaplib.IMAP4_SSL, recognizer=recognize_invoice):
    report = {"messages": 0, "new_drafts": 0, "duplicates": 0, "without_pdf": 0,
              "recognition_failed": 0}
    client = None
    try:
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
        found = checked(client.uid("SEARCH", None, "ALL"), "search")
        if len(found) != 1 or not isinstance(found[0], bytes):
            raise FolderError("search")
        try:
            uids = sorted(set(int(item) for item in found[0].split()))
        except ValueError:
            raise FolderError("search") from None
        for uid in uids:
            if not 1 <= uid <= 4294967295:
                raise FolderError("search")
            report["messages"] += 1
            # Re-fetching with PEEK verifies earlier message identity as well as SHA deduplication.
            metadata = checked(client.uid("FETCH", str(uid), "(UID RFC822.SIZE)"), "size")
            headers = [item for item in metadata if isinstance(item, bytes) and b"RFC822.SIZE" in item]
            size = re.search(rb"\bRFC822.SIZE\s+(\d+)", headers[0]) if len(headers) == 1 else None
            if not size or int(size[1]) > MAX_MESSAGE_BYTES:
                raise FolderError("size")
            body = checked(client.uid("FETCH", str(uid), "(UID BODY.PEEK[])"), "fetch")
            parts = [item for item in body if isinstance(item, tuple) and len(item) == 2]
            if (len(parts) != 1 or not all(isinstance(item, bytes) for item in parts[0])
                    or re.findall(rb"\bUID\s+(\d+)\b", parts[0][0]) != [str(uid).encode()]):
                raise FolderError("fetch")
            raw = parts[0][1]
            if not raw or len(raw) > MAX_MESSAGE_BYTES:
                raise FolderError("size")
            sha = hashlib.sha256(raw).hexdigest()
            old = store.conn.execute(
                "SELECT sha256 FROM mail_messages WHERE folder=? AND epoch=? AND uid=?",
                (FOLDER, epoch, uid)).fetchone()
            if old and old[0] != sha:
                raise FolderError("message_changed")
            message = BytesParser(policy=policy.default).parsebytes(raw)
            pdf_count = 0
            for part in message.walk():
                if part.is_multipart():
                    continue
                filename = part.get_filename() or ""
                if part.get_content_type() != "application/pdf" and not filename.lower().endswith(".pdf"):
                    continue
                content = part.get_payload(decode=True)
                if not content or len(content) > MAX_MESSAGE_BYTES:
                    raise FolderError("pdf_invalid")
                pdf_count += 1
                digest = hashlib.sha256(content).hexdigest()
                existing = store.conn.execute("SELECT id FROM documents WHERE sha256=?", (digest,)).fetchone()
                if existing:
                    if store.base(existing[0]).get("_recognition_state") == "pending":
                        store.recognize_document(existing[0], recognizer)
                    report["duplicates"] += 1
                    continue
                # Persist a recoverable draft before OCR; failure never discards the original.
                store.add_pdf(content, filename or digest + ".pdf", {"fields": {}, "state": "pending"})
                document_id = store.conn.execute("SELECT id FROM documents WHERE sha256=?", (digest,)).fetchone()[0]
                result = store.recognize_document(document_id, recognizer)
                report["new_drafts"] += 1
                if result.get("state") == "failed":
                    report["recognition_failed"] += 1
            if not pdf_count:
                report["without_pdf"] += 1
            with store.conn:
                store.conn.execute("INSERT OR IGNORE INTO mail_messages VALUES (?,?,?,?)",
                                   (FOLDER, epoch, uid, sha))
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
    "auth": "Сервер отклонил вход. Проверьте пароль приложения; настройки безопасности не ослабляются.",
    "folder": "Сервер не открыл INBOX.SmartReport-Test. Проверьте наличие и права этой папки.",
    "epoch_missing": "Сервер не вернул UIDVALIDITY папки. Импорт остановлен.",
    "epoch_changed": "UIDVALIDITY папки изменился. Автоматический сброс запрещён; нужна сверка очереди.",
    "search": "Сервер не вернул корректный список UID только выбранной папки.",
    "size": "Сервер не вернул размер письма или письмо превышает 30 МБ. Импорт остановлен.",
    "fetch": "Не удалось безопасно получить выбранный UID через BODY.PEEK[].",
    "message_changed": "Содержимое ранее полученного UID изменилось. Импорт остановлен для ручной сверки.",
    "pdf_invalid": "PDF-вложение пустое или превышает 30 МБ. Письмо не изменено.",
}


def open_import_dialog(window):
    from limited_mail_ui import PasswordDialog, ERROR_MESSAGES
    from incoming_documents import IncomingStore

    def operation(password):
        store = IncomingStore(window.store.root, window.store.host)
        try:
            window.mail_report = import_folder(store, password)
        finally:
            store.close()

    # PasswordOperation emits only fixed codes; never provider exception text.
    ERROR_MESSAGES.update(ERRORS)
    class FolderDialog(PasswordDialog):
        def operation_finished(self):
            super().operation_finished()
            if hasattr(window, "mail_report"):
                report = window.mail_report
                self.status.setText(
                    f"Готово. Проверено писем: {report['messages']}; новых черновиков: {report['new_drafts']}; "
                    f"повторов PDF: {report['duplicates']}. Расходы не создавались. Можно закрыть окно.")

    dialog = FolderDialog(operation, offline=False, parent=window)
    dialog.setWindowTitle("SmartReport — получить документы из SmartReport-Test")
    dialog.notice.setText(
        "Ручной импорт всех писем только из INBOX.SmartReport-Test.\n"
        "EXAMINE + BODY.PEEK: письма не меняются, расходы автоматически не создаются.\n"
        "Пароль используется только в памяти этого сеанса и не сохраняется.")
    dialog.buttons.button(dialog.buttons.StandardButton.Ok).setText("Получить документы")

    dialog.exec()
    if hasattr(window, "mail_report"):
        result = window.mail_report
        window.reload_documents()
        window.status.setText(
            f"Проверено писем: {result['messages']}; новых черновиков: {result['new_drafts']}; "
            f"повторов PDF: {result['duplicates']}; без PDF: {result['without_pdf']}; "
            f"ошибок распознавания: {result['recognition_failed']}. Расходы не создавались.")
        del window.mail_report
