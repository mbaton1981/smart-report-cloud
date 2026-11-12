"""Manual, read-only IMAP access to the separately configured info mailbox."""
from __future__ import annotations

import base64
import hashlib
import imaplib
import json
import os
import re
import ssl
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from object_records import MAX_FILE_BYTES


MAX_MESSAGE_BYTES = 30 * 1024 * 1024
MAX_MESSAGE_HEADERS = 100
ATTACHMENT_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".webp": "image/webp", ".gif": "image/gif", ".heic": "image/heic",
}
CONTENT_SUFFIXES = {value: key for key, value in ATTACHMENT_TYPES.items()}


class InfoMailError(ValueError):
    pass


@dataclass(frozen=True)
class MailboxSettings:
    email_address: str
    host: str
    port: int
    security: str
    inbox_folder: str
    sent_folder: str

    def validate(self):
        if ("@" not in self.email_address or len(self.email_address) > 320
                or any(char.isspace() for char in self.email_address)):
            raise InfoMailError("Укажите корректный адрес ящика info.")
        if (not self.host.strip() or len(self.host) > 255
                or any(char.isspace() for char in self.host)):
            raise InfoMailError("Укажите IMAP-сервер.")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise InfoMailError("Порт IMAP должен быть от 1 до 65535.")
        if self.security not in ("SSL/TLS", "STARTTLS"):
            raise InfoMailError("Выберите защищённый режим соединения.")
        if (not self.inbox_folder.strip() or not self.sent_folder.strip()
                or len(self.inbox_folder) > 500 or len(self.sent_folder) > 500
                or any(ord(char) < 32 or ord(char) == 127
                       for char in self.inbox_folder + self.sent_folder)):
            raise InfoMailError("Укажите папки входящих и отправленных писем.")

    def folder(self, direction: str) -> str:
        if direction == "incoming":
            return self.inbox_folder
        if direction == "sent":
            return self.sent_folder
        raise InfoMailError("Выберите входящие или отправленные письма.")


@dataclass(frozen=True)
class MailMessage:
    uid: str
    uidvalidity: str
    folder: str
    sender: str
    subject: str
    sent_at: str
    message_id: str
    size: int


@dataclass(frozen=True)
class MailAttachment:
    part: int
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class MailContent:
    key: str
    sender: str
    subject: str
    sent_at: str
    body: str
    attachments: tuple[MailAttachment, ...]


def _settings_path(root: Path | None = None) -> Path:
    if root is None:
        import desktop_auth
        root = desktop_auth.app_data_dir()
    return Path(root) / "info_mail_settings.json"


def save_settings(settings: MailboxSettings, password: str, root: Path | None = None):
    settings.validate()
    if not isinstance(password, str) or not password:
        raise InfoMailError("Введите пароль ящика info.")
    import desktop_auth
    protected = desktop_auth._protect(password.encode("utf-8"))
    if not protected:
        raise InfoMailError("Не удалось защитить пароль средствами Windows DPAPI.")
    path = _settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "email_address": settings.email_address.strip(),
        "host": settings.host.strip(),
        "port": settings.port,
        "security": settings.security,
        "inbox_folder": settings.inbox_folder.strip(),
        "sent_folder": settings.sent_folder.strip(),
        "protected_password": base64.b64encode(protected).decode("ascii"),
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump(data, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def load_settings(root: Path | None = None) -> tuple[MailboxSettings, str] | None:
    path = _settings_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise InfoMailError("Неизвестный формат настроек ящика info.")
        settings = MailboxSettings(
            email_address=data["email_address"], host=data["host"], port=data["port"],
            security=data["security"], inbox_folder=data["inbox_folder"],
            sent_folder=data["sent_folder"],
        )
        settings.validate()
        protected = base64.b64decode(data["protected_password"], validate=True)
    except (OSError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, InfoMailError):
            raise
        raise InfoMailError("Не удалось прочитать настройки ящика info.") from None
    import desktop_auth
    password = desktop_auth._unprotect(protected)
    if not password:
        raise InfoMailError(
            "Пароль не расшифрован профилем Windows. Введите пароль заново в настройках info."
        )
    try:
        decoded = password.decode("utf-8")
    except UnicodeError:
        raise InfoMailError("Защищённый пароль ящика info повреждён.") from None
    return settings, decoded


class _PlainText(HTMLParser):
    BLOCKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, _attrs):
        if tag.casefold() in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.casefold() in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


@contextmanager
def _open_mailbox(
    settings: MailboxSettings,
    password: str,
    client_factory=None,
):
    settings.validate()
    context = ssl.create_default_context()
    if client_factory is None:
        client_factory = imaplib.IMAP4_SSL if settings.security == "SSL/TLS" else imaplib.IMAP4
    client = None
    primary_error = False
    try:
        if settings.security == "SSL/TLS":
            client = client_factory(
                settings.host, settings.port, ssl_context=context, timeout=30,
            )
        else:
            client = client_factory(settings.host, settings.port, timeout=30)
            response = client.starttls(ssl_context=context)
            _require_ok(response, "Не удалось установить защищённое IMAP-соединение.")
        _require_ok(client.login(settings.email_address, password), "Не удалось войти в ящик info.")
        yield client
    except (imaplib.IMAP4.error, OSError, ssl.SSLError):
        primary_error = True
        raise InfoMailError(
            "Ошибка подключения к ящику info. Проверьте сервер, порт, защиту и учётные данные."
        ) from None
    except BaseException:
        primary_error = True
        raise
    finally:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                if not primary_error:
                    raise InfoMailError("Не удалось корректно завершить IMAP-сеанс.") from None


def _require_ok(response, message: str):
    if not response or response[0] != "OK":
        raise InfoMailError(message)
    return response[1]


def test_connection(
    settings: MailboxSettings,
    password: str,
    *,
    client_factory=None,
):
    with _open_mailbox(settings, password, client_factory) as client:
        for folder in dict.fromkeys((settings.inbox_folder, settings.sent_folder)):
            _select_readonly(client, folder)


def _select_readonly(client, folder: str):
    quoted = '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return _require_ok(
        client.select(quoted, readonly=True),
        "Не удалось открыть настроенную папку только для чтения.",
    )


def _fetch_bytes(response, message: str) -> bytes:
    data = _require_ok(response, message)
    bodies = [
        item[1] for item in data
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes)
    ]
    if len(bodies) != 1:
        raise InfoMailError(message)
    return bodies[0]


def _uidvalidity(client) -> str:
    response = client.response("UIDVALIDITY")
    if not response or not response[1] or not response[1][0]:
        raise InfoMailError("Почтовый сервер не сообщил UIDVALIDITY.")
    value = response[1][0]
    if not value.isdigit():
        raise InfoMailError("Почтовый сервер вернул некорректный UIDVALIDITY.")
    return value.decode("ascii")


def _parse_date(value: str) -> str:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return ""
    if parsed is None:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _header_message(header: bytes, uid: str, epoch: str, folder: str, size: int) -> MailMessage:
    message = BytesParser(policy=policy.default).parsebytes(header)
    return MailMessage(
        uid=uid, uidvalidity=epoch, folder=folder,
        sender=str(message.get("From", "")),
        subject=str(message.get("Subject", "")),
        sent_at=_parse_date(str(message.get("Date", ""))),
        message_id=str(message.get("Message-ID", "")).strip(),
        size=size,
    )


def list_messages(
    settings: MailboxSettings,
    password: str,
    folder: str,
    *,
    days_back: int = 180,
    now: datetime | None = None,
    client_factory=None,
) -> list[MailMessage]:
    if folder not in (settings.inbox_folder, settings.sent_folder):
        raise InfoMailError("Папка не относится к настроенному ящику info.")
    if type(days_back) is not int or not 1 <= days_back <= 3650:
        raise InfoMailError("Период поиска должен быть от 1 до 3650 дней.")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise InfoMailError("Дата поиска должна содержать часовой пояс.")
    since = now.date() - timedelta(days=days_back - 1)
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    since_text = f"{since.day:02d}-{months[since.month - 1]}-{since.year}"
    with _open_mailbox(settings, password, client_factory) as client:
        _select_readonly(client, folder)
        epoch = _uidvalidity(client)
        search = _require_ok(
            client.uid("SEARCH", None, "SINCE", since_text),
            "Не удалось получить список писем.",
        )
        if len(search) != 1 or not isinstance(search[0], bytes):
            raise InfoMailError("Почтовый сервер вернул некорректный список писем.")
        raw_uids = search[0].split()
        if any(not uid.isdigit() or not 1 <= int(uid) <= 4294967295 for uid in raw_uids):
            raise InfoMailError("Почтовый сервер вернул некорректный UID.")
        uids = sorted(set(raw_uids), key=int, reverse=True)[:MAX_MESSAGE_HEADERS]
        if not uids:
            return []
        sequence = b",".join(uids).decode("ascii")
        data = _require_ok(
            client.uid(
                "FETCH", sequence,
                "(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])",
            ),
            "Не удалось прочитать заголовки писем.",
        )
        messages_by_uid = {}
        for item in data:
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            metadata, header = item
            if not isinstance(metadata, bytes) or not isinstance(header, bytes):
                raise InfoMailError("Почтовый сервер вернул некорректный заголовок.")
            uid_match = re.search(rb"\bUID (\d+)\b", metadata, re.IGNORECASE)
            size_match = re.search(rb"\bRFC822.SIZE (\d+)\b", metadata, re.IGNORECASE)
            if not uid_match or not size_match:
                raise InfoMailError("В заголовке письма отсутствует UID или размер.")
            uid = uid_match[1].decode("ascii")
            if uid.encode("ascii") not in uids or uid in messages_by_uid:
                raise InfoMailError("Почтовый сервер вернул неожиданный UID.")
            messages_by_uid[uid] = _header_message(
                header, uid, epoch, folder, int(size_match[1]),
            )
        if len(messages_by_uid) != len(uids):
            raise InfoMailError("Список писем изменился. Обновите список и повторите выбор.")
        return [messages_by_uid[uid.decode("ascii")] for uid in uids]


def _part_content(part) -> str:
    try:
        value = part.get_content()
        return value if isinstance(value, str) else ""
    except (LookupError, UnicodeError, KeyError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def _message_body(message) -> str:
    plain, html = [], []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type().casefold()
        if content_type == "text/plain":
            plain.append(_part_content(part))
        elif content_type == "text/html":
            parser = _PlainText()
            parser.feed(_part_content(part))
            html.append("".join(parser.parts))
    body = "\n\n".join(plain or html).strip()
    if len(body) > 500_000:
        return body[:500_000] + "\n\n[Текст письма обрезан для просмотра.]"
    return body


def fetch_message(
    settings: MailboxSettings,
    password: str,
    summary: MailMessage,
    *,
    client_factory=None,
) -> MailContent:
    if summary.folder not in (settings.inbox_folder, settings.sent_folder):
        raise InfoMailError("Письмо не относится к настроенному ящику info.")
    if not summary.uid.isdigit() or summary.size > MAX_MESSAGE_BYTES:
        raise InfoMailError("Письмо превышает предел 30 МБ или имеет некорректный UID.")
    with _open_mailbox(settings, password, client_factory) as client:
        _select_readonly(client, summary.folder)
        epoch = _uidvalidity(client)
        if epoch != summary.uidvalidity:
            raise InfoMailError("Папка изменилась после показа списка. Обновите список писем.")
        raw = _fetch_bytes(
            client.uid("FETCH", summary.uid, "(BODY.PEEK[])"),
            "Не удалось прочитать выбранное письмо.",
        )
    if len(raw) > MAX_MESSAGE_BYTES:
        raise InfoMailError("Письмо превышает предел 30 МБ.")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    attachments = []
    for part_number, part in enumerate(message.walk()):
        if part.is_multipart():
            continue
        filename = part.get_filename()
        content_type = part.get_content_type().casefold()
        disposition = part.get_content_disposition()
        is_inline_image = content_type.startswith("image/") and disposition == "inline"
        if not filename and disposition != "attachment" and not is_inline_image:
            continue
        suffix = Path(filename or "").suffix.casefold()
        if suffix not in ATTACHMENT_TYPES:
            suffix = CONTENT_SUFFIXES.get(content_type, "")
        if not suffix:
            continue
        if not filename:
            filename = f"inline-image-{part_number}{suffix}"
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if len(payload) > MAX_FILE_BYTES:
            raise InfoMailError("Вложение превышает предел 100 МБ.")
        attachments.append(MailAttachment(
            part=part_number,
            filename=Path(filename).name,
            content_type=ATTACHMENT_TYPES[suffix],
            content=payload,
        ))
    message_id = str(message.get("Message-ID", "")).strip()
    stable_id = message_id or f"{summary.folder}:{summary.uidvalidity}:{summary.uid}"
    identity = hashlib.sha256(
        (settings.email_address.strip().casefold() + "\0" + stable_id).encode("utf-8")
    ).hexdigest()
    return MailContent(
        key=identity,
        sender=str(message.get("From", summary.sender)),
        subject=str(message.get("Subject", summary.subject)),
        sent_at=_parse_date(str(message.get("Date", ""))) or summary.sent_at,
        body=_message_body(message),
        attachments=tuple(attachments),
    )
