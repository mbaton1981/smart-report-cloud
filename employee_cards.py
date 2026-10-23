"""Employee details: encrypted storage and non-sensitive change metadata."""
from datetime import date, datetime, timezone
import hashlib
import hmac
import json
import os
import re
import uuid

FIELDS = {
    "start_date": "Дата начала работы", "end_date": "Дата окончания работы",
    "identifier_type": "Тип идентификатора", "identifier": "Персональный номер",
    "address": "Адрес", "postal_code": "Почтовый индекс", "city": "Город", "country": "Страна",
    "phone": "Телефон", "email": "Электронная почта",
    "account_type": "Тип счёта", "bank_name": "Название банка", "account_holder": "Владелец счёта",
    "clearing": "Clearingnummer", "account_number": "Номер счёта", "iban": "IBAN", "bic": "BIC/SWIFT",
    "verified_date": "Дата подтверждения реквизитов",
}
BANK_FIELDS = {"account_type", "bank_name", "account_holder", "clearing", "account_number", "iban", "bic"}
CARD_ROLES = {"admin", "accountant"}


class CardError(ValueError):
    pass


class CardConflict(CardError):
    pass


class CardUnavailable(CardError):
    pass


def ensure_schema(cursor, table):
    if table not in {"employees", "meta_employees"}:
        raise ValueError("Unsupported employee table")
    columns = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}
    additions = [("card_cipher", "TEXT"), ("card_version", "INTEGER NOT NULL DEFAULT 0")]
    if table == "employees":
        additions.append(("card_server_id", "INTEGER"))
    for key, definition in additions:
        if key not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {key} {definition}")
    if table == "employees":
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_employee_card_server ON employees(card_server_id)")
    else:
        cursor.execute("""CREATE TABLE IF NOT EXISTS employee_card_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL, actor_role TEXT NOT NULL, changed_at TEXT NOT NULL,
                fields_json TEXT NOT NULL, revision INTEGER NOT NULL, operation_id TEXT NOT NULL UNIQUE)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS employee_card_operations(
                operation_id TEXT PRIMARY KEY, employee_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL, request_mac TEXT NOT NULL, revision INTEGER NOT NULL)""")


def empty_card():
    return dict.fromkeys(FIELDS, "") | {"account_type": "swedish"}


def validate_card(value):
    if not isinstance(value, dict) or set(value) - FIELDS.keys():
        raise CardError("Неизвестные поля карточки.")
    result = empty_card()
    for field, text in value.items():
        if not isinstance(text, str) or len(text) > (500 if field == "address" else 200):
            raise CardError(f"{FIELDS[field]}: требуется текст допустимой длины.")
        if any(ord(char) < 32 and char not in "\n\t" for char in text):
            raise CardError(f"{FIELDS[field]}: недопустимые управляющие символы.")
        result[field] = text.strip()
    if result["identifier_type"] not in {"", "personnummer", "samordningsnummer"}:
        raise CardError("Выберите тип персонального идентификатора.")
    if result["account_type"] not in {"swedish", "foreign"}:
        raise CardError("Выберите шведский или иностранный счёт.")
    for field in ("start_date", "end_date", "verified_date"):
        if result[field]:
            try:
                date.fromisoformat(result[field])
            except ValueError:
                raise CardError(f"{FIELDS[field]}: используйте ГГГГ-ММ-ДД или оставьте пустым.") from None
    if result["start_date"] and result["end_date"] and result["end_date"] < result["start_date"]:
        raise CardError("Дата окончания работы раньше даты начала.")
    return result


def warnings(card):
    result = []
    number = re.sub(r"[-+ ]", "", card["identifier"])
    if number and (not re.fullmatch(r"\d{10}|\d{12}", number) or not card["identifier_type"]):
        result.append("Персональный номер: проверьте тип и формат (10 или 12 цифр).")
    if card["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", card["email"]):
        result.append("Электронная почта: проверьте формат.")
    for key in ("clearing", "account_number"):
        if card[key] and not re.fullmatch(r"[\d -]+", card[key]):
            result.append(f"{FIELDS[key]}: проверьте формат.")
    iban = card["iban"].replace(" ", "").upper()
    if iban:
        valid = bool(re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", iban))
        if valid:
            digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in iban[4:] + iban[:4])
            valid = int(digits) % 97 == 1
        if not valid:
            result.append("IBAN: проверьте формат и контрольную сумму.")
    if card["bic"] and not re.fullmatch(r"[A-Za-z]{6}[A-Za-z0-9]{2}([A-Za-z0-9]{3})?", card["bic"]):
        result.append("BIC/SWIFT: ожидается 8 или 11 латинских символов/цифр.")
    return result


def missing_fields(card):
    required = ["start_date", "address", "postal_code", "city", "country", "phone", "email",
                "bank_name", "account_holder", "verified_date"]
    required += ["iban", "bic"] if card["account_type"] == "foreign" else ["clearing", "account_number"]
    missing = [FIELDS[key] for key in required if not card[key]]
    if not card["identifier"]:
        missing.append("Персональный номер (при наличии; отсутствие не блокирует)")
    elif not card["identifier_type"]:
        missing.append(FIELDS["identifier_type"])
    return missing


def cipher():
    from cryptography.fernet import Fernet
    key = os.environ.get("SMARTREPORT_EMPLOYEE_CARD_KEY", "")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeError):
        raise CardUnavailable("Защищённое хранение карточек не настроено. Обратитесь к администратору.") from None


def encrypt_card(card, employee_id):
    return cipher().encrypt(json.dumps({"employee_id": employee_id, "card": card},
                                      ensure_ascii=False).encode()).decode("ascii")


def decrypt_card(token, employee_id):
    from cryptography.fernet import InvalidToken
    if not token:
        return empty_card()
    try:
        envelope = json.loads(cipher().decrypt(token.encode()))
        if envelope["employee_id"] != employee_id:
            raise CardError("Нарушена привязка защищённой карточки.")
        return validate_card(envelope["card"])
    except (InvalidToken, KeyError, TypeError, json.JSONDecodeError, UnicodeError):
        raise CardUnavailable("Не удалось открыть защищённую карточку. Проверьте ключ восстановления.") from None


def employee_revision(cursor, name):
    key = " ".join(name.split()).casefold()
    row = cursor.execute("SELECT MAX(revision) FROM employee_sync_events WHERE employee_key=?", (key,)).fetchone()
    return int(row[0] or 0)


def card_record(cursor, employee_id):
    row = cursor.execute("SELECT * FROM meta_employees WHERE id=?", (employee_id,)).fetchone()
    if row is None:
        raise CardError("Сотрудник не найден. Новый сотрудник не создан.")
    card = decrypt_card(row["card_cipher"], employee_id)
    return dict(id=row["id"], name=row["name"], salary_rate=row["salary_rate"], is_active=bool(row["is_active"]),
                card=card, revision=row["card_version"], employee_revision=employee_revision(cursor, row["name"]),
                cipher=row["card_cipher"], missing=missing_fields(card), warnings=warnings(card))


def save_card(cursor, employee_id, payload, actor, status_action):
    if not isinstance(payload, dict) or set(payload) != {
        "card", "revision", "employee_revision", "is_active", "operation_id"
    }:
        raise CardError("Неверный формат сохранения карточки.")
    try:
        operation_id = str(uuid.UUID(payload["operation_id"]))
    except (ValueError, TypeError, AttributeError):
        raise CardError("Неверный идентификатор операции.") from None
    if type(payload["is_active"]) is not bool or type(payload["revision"]) is not int or type(payload["employee_revision"]) is not int:
        raise CardError("Неверная версия карточки или статус сотрудника.")
    card = validate_card(payload["card"])
    key = os.environ.get("SMARTREPORT_EMPLOYEE_CARD_KEY", "").encode()
    cipher()  # Fail closed, including empty cards.
    mac = hmac.new(key, json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest()
    previous_op = cursor.execute("SELECT * FROM employee_card_operations WHERE operation_id=?", (operation_id,)).fetchone()
    old = card_record(cursor, employee_id)
    if previous_op:
        if (previous_op["employee_id"] != employee_id or previous_op["actor_id"] != actor["user_id"]
                or not hmac.compare_digest(previous_op["request_mac"], mac)
                or previous_op["revision"] != old["revision"]):
            raise CardConflict("Операция уже обработана или карточка изменена. Откройте её заново.")
        return old
    if old["revision"] != payload["revision"] or old["employee_revision"] != payload["employee_revision"]:
        raise CardConflict("Карточка или статус изменены другим пользователем. Данные не заменены; откройте карточку заново.")
    bank_changed = any(old["card"][field] != card[field] for field in BANK_FIELDS)
    if bank_changed:
        card["verified_date"] = ""
    fields = [key for key in FIELDS if old["card"][key] != card[key]]
    if old["is_active"] != payload["is_active"]:
        fields.append("is_active")
        status_action(cursor, {"operation_id": operation_id, "name": old["name"],
                              "employee_key": " ".join(old["name"].split()).casefold(),
                              "revision": old["employee_revision"] + 1},
                      "activate" if payload["is_active"] else "deactivate")
    version = old["revision"] + bool(fields)
    if fields:
        cursor.execute("UPDATE meta_employees SET card_cipher=?,card_version=? WHERE id=?",
                       (encrypt_card(card, employee_id), version, employee_id))
        cursor.execute("""INSERT INTO employee_card_audit
            (employee_id,actor_id,actor_role,changed_at,fields_json,revision,operation_id)
            VALUES(?,?,?,?,?,?,?)""", (employee_id, actor["user_id"], actor["role"],
            datetime.now(timezone.utc).isoformat(), json.dumps(fields), version, operation_id))
    cursor.execute("INSERT INTO employee_card_operations VALUES(?,?,?,?,?)",
                   (operation_id, employee_id, actor["user_id"], mac, version))
    result = card_record(cursor, employee_id)
    result["bank_confirmation_reset"] = bank_changed
    return result
