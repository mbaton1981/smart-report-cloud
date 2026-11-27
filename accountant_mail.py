"""Send a user-approved accountant package and keep its original PDF in this profile."""
from contextlib import closing, contextmanager
from datetime import datetime
from email.headerregistry import Address
from email.errors import HeaderParseError
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import smtplib
import sqlite3
import ssl

import desktop_auth


MAIL_HOST = "mailcluster.loopia.se"
STATUS = {"unknown": "Отправка не подтверждена", "sent": "Принято Loopia", "failed": "Не отправлено"}


def address_value(text):
    text = text.strip()
    try:
        addr = Address(addr_spec=text)
        if (not addr.username or not addr.domain or "." not in addr.domain or not text.isascii()
                or any(ord(c) < 33 or ord(c) == 127 for c in text)):
            raise ValueError
    except (ValueError, HeaderParseError):
        raise ValueError("Укажите один полный адрес электронной почты.") from None
    return addr.addr_spec


@contextmanager
def archive():
    with closing(sqlite3.connect(desktop_auth.app_data_dir() / "accountant_mail.sqlite3")) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS mail (
            id INTEGER PRIMARY KEY, project INTEGER NOT NULL, created TEXT NOT NULL,
            sender TEXT NOT NULL, recipient TEXT NOT NULL, subject TEXT NOT NULL,
            period TEXT NOT NULL, status TEXT NOT NULL, filename TEXT NOT NULL, pdf BLOB NOT NULL)""")
        yield db


def send_package(settings, password, recipient, subject, body, path, project, period):
    if settings.host.strip().lower() != MAIL_HOST:
        raise ValueError("Для отправки выберите ящик info, подключённый к почте Loopia.")
    sender, recipient = address_value(settings.email_address), address_value(recipient)
    pdf = path.read_bytes()
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, recipient, subject
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid(domain=sender.split("@")[-1])
    msg.set_content(body)
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=path.name)
    # If the process exits during SMTP, the retained attempt must not imply success.
    with archive() as db:
        row_id = db.execute("""INSERT INTO mail
            (project,created,sender,recipient,subject,period,status,filename,pdf)
            VALUES (?,?,?,?,?,?,'unknown',?,?)""", (project, datetime.now().astimezone().isoformat(timespec="seconds"),
            sender, recipient, subject, period, path.name, pdf)).lastrowid
    submitting = False
    smtp = None
    try:
        smtp = smtplib.SMTP_SSL(MAIL_HOST, 465, timeout=45, context=ssl.create_default_context())
        smtp.login(sender, password)
        submitting = True
        smtp.send_message(msg, from_addr=sender, to_addrs=[recipient])
        status, error = "sent", ""
    except smtplib.SMTPAuthenticationError:
        status, error = "failed", "Loopia отклонила вход. Проверьте настройки ящика info."
    except (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused):
        status, error = "failed", "Почтовый сервер отклонил запрос. Проверьте адрес и размер вложения."
    except (OSError, smtplib.SMTPException):
        status = "unknown" if submitting else "failed"
        error = ("Соединение прервалось во время отправки. Перед повтором уточните у бухгалтера, пришло ли письмо."
                 if submitting else "Не удалось подключиться к почте Loopia. Проверьте соединение и настройки ящика.")
    finally:
        if smtp is not None:
            smtp.close()
    try:
        with archive() as db:
            db.execute("UPDATE mail SET status=? WHERE id=?", (status, row_id))
    except sqlite3.Error:
        if status == "sent":
            raise RuntimeError("Loopia приняла письмо. Не отправляйте его повторно. Обновить статус в истории не удалось.") from None
        raise
    if error:
        raise RuntimeError(error)
