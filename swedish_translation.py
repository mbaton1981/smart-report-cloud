"""Whole-description Swedish translation for customer PDFs via the SmartReport assistant server.

The model translates; this module only checks what can be checked mechanically
(empty text, leftover Cyrillic, numbers and technical designations). Meaning is
never claimed as verified: the desktop shows every machine translation for review.
"""
import hashlib
import re
import sqlite3
import uuid
from collections import Counter
from urllib.parse import urlsplit

import requests

CYRILLIC = re.compile(r"[\u0400-\u04ff]")
MAX_ITEMS = 25
MAX_TEXT = 1500
# Terminology hints for the model, not a word-by-word replacement table.
GLOSSARY = (
    ("демонтаж", "demontering / rivning"), ("монтаж", "montering"),
    ("холодильные камеры", "kylrum"), ("трубы отопления", "värmerör"), ("отопление", "värme"),
    ("радиатор", "radiator / element"), ("сантехника", "VVS / sanitet"), ("канализация", "avlopp"),
    ("водяные трубы", "vattenrör"), ("вентиляция", "ventilation"), ("короба вентиляции", "ventilationskanaler"),
    ("кухонные вытяжки", "köksfläktar"), ("гипсокартон", "gipsskivor"), ("шпаклёвка", "spackling"),
    ("покраска", "målning"), ("отделка", "ytskikt / färdigställning"), ("уборка", "städning"),
    ("вывоз мусора", "bortforsling av avfall"), ("плитка", "kakel / klinker"), ("стяжка", "avjämning"),
    ("утепление", "isolering"), ("внутренние окна", "invändiga fönster"), ("двери", "dörrar"),
)

JOURNAL_PHRASES = {
    "демонтаж вентиляции": "Demontering av ventilation",
    "демонтаж стекла": "Demontering av glas",
    "демонтаж стекол": "Demontering av glas",
    "демонтаж стен": "Demontering av väggar",
    "подготовка стекла к демонтажу": "Förberedelse av glas inför demontering",
}


def suggest_journal_phrase(text):
    """Suggest translations only for exact matches of complete descriptions."""
    source = " ".join(text.casefold().replace("ё", "е").split()).removesuffix(".")
    return JOURNAL_PHRASES.get(source, "")


class TranslationError(Exception):
    pass


def _numbers(text):
    text = text.replace("²", "2").replace("³", "3")
    return Counter(match.replace(",", ".") for match in re.findall(r"\d+(?:[.,]\d+)?", text))


def _designations(source):
    tokens = set(re.findall(r"\b(?=[A-Za-z0-9/-]*[A-Za-z])(?=[A-Za-z0-9/-]*\d)[A-Za-z0-9/-]+\b", source))
    tokens.update(re.findall(r"\b[A-Z]{2,}\b", source))
    if CYRILLIC.search(source):
        # Latin names inside Russian text (objects, brands, streets) must survive unchanged.
        tokens.update(re.findall(r"\b[A-ZÅÄÖ][a-zåäöé]+(?:[A-ZÅÄÖa-zåäöé]*)\b", source))
    return tokens


def translation_problems(source, translated):
    """Mechanical checks only; an empty list does not mean the meaning is correct."""
    source = (source or "").strip()
    translated = (translated or "").strip()
    if not translated:
        return ["перевод пустой"]
    problems = []
    if CYRILLIC.search(translated):
        problems.append("остался русский текст (кириллица)")
    missing_numbers = _numbers(source) - _numbers(translated)
    if missing_numbers:
        problems.append("не найдены числа: " + ", ".join(sorted(missing_numbers)))
    missing = sorted(token for token in _designations(source) if token not in translated)
    if missing:
        problems.append("не найдены обозначения/названия: " + ", ".join(missing))
    return problems


def _key(text):
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def load_confirmed_translations(connection, rows):
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='journal_translation_confirmations'"
    ).fetchone()
    if table is None:
        return {}
    translations = {}
    for row in rows:
        row_id = row.get("sync_id")
        sync_id = str(row_id or "")
        source = str(row.get("comment") or "").strip()
        if not sync_id or not source:
            continue
        saved = connection.execute(
            "SELECT translation FROM journal_translation_confirmations "
            "WHERE sync_id=? AND source_sha256=?",
            (sync_id, _key(source)),
        ).fetchone()
        if saved is not None:
            translations[row_id] = saved[0]
    return translations


def save_confirmed_translations(connection, rows, translations):
    sources = {
        str(row.get("sync_id") or ""): str(row.get("comment") or "").strip()
        for row in rows
        if row.get("sync_id") and str(row.get("comment") or "").strip()
    }
    unknown_ids = {str(sync_id) for sync_id in translations} - set(sources)
    if unknown_ids:
        raise ValueError("Нельзя сохранить перевод без связанной записи и исходного текста.")
    records = []
    for row_id, translated in translations.items():
        sync_id = str(row_id)
        source = sources[sync_id]
        translated = str(translated or "").strip()
        if not translated or CYRILLIC.search(translated):
            raise ValueError("Подтверждённый перевод должен быть непустым и на шведском.")
        records.append((sync_id, _key(source), translated))
    if not records:
        return
    with connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS journal_translation_confirmations (
                sync_id TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                translation TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (sync_id, source_sha256)
            )
        """)
        connection.executemany("""
            INSERT INTO journal_translation_confirmations
                (sync_id, source_sha256, translation)
            VALUES (?, ?, ?)
            ON CONFLICT(sync_id, source_sha256) DO UPDATE SET
                translation=excluded.translation,
                updated_at=CURRENT_TIMESTAMP
        """, records)


_cache = {}


def clear_cache():
    _cache.clear()


def _request(items, database):
    import desktop_auth
    from sync_dataset import HEADER, file_dataset, require_response_dataset

    base = desktop_auth.server_url().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}):
        raise TranslationError("Перевод требует защищённого соединения HTTPS с сервером.")
    token = desktop_auth.current_token()
    if not token:
        raise TranslationError("Нет личного входа администратора: серверный перевод недоступен.")
    dataset = file_dataset(database) if database else None
    if not dataset:
        raise TranslationError("Нет идентификатора набора данных: сначала выполните штатную синхронизацию.")
    try:
        response = requests.post(
            base + "/api/assistant/translate",
            headers={"Authorization": "Bearer " + token, HEADER: dataset},
            json={"operation_id": str(uuid.uuid4()), "items": [{"id": i, "text": t} for i, t in items]},
            timeout=(10, 90), allow_redirects=False,
        )
    except requests.RequestException:
        raise TranslationError("Нет связи с сервером перевода.") from None
    try:
        require_response_dataset(response, dataset)
    except RuntimeError:
        raise TranslationError("Сервер ответил для другого набора данных; перевод не принят.") from None
    if response.status_code in (404, 405):
        raise TranslationError("Сервер ещё не поддерживает перевод: требуется обновление сервера.")
    if response.status_code in (401, 403):
        raise TranslationError("Нет доступа к переводу: нужен действующий личный вход администратора.")
    if response.status_code == 413:
        raise TranslationError("Описания слишком длинные для одного запроса перевода.")
    if response.status_code not in (200, 400):
        raise TranslationError("Сервер перевода временно недоступен.")
    try:
        data = response.json()
    except ValueError:
        raise TranslationError("Некорректный ответ сервера перевода.") from None
    if not isinstance(data, dict) or data.get("ok") is not True:
        message = data.get("error") if isinstance(data, dict) else None
        raise TranslationError(message if isinstance(message, str) and message else "Перевод не выполнен.")
    result = data.get("translations")
    expected = {identifier for identifier, _ in items}
    if (not isinstance(result, list)
            or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                   or not isinstance(item.get("sv"), str) for item in result)
            or sorted(item["id"] for item in result) != sorted(expected)):
        raise TranslationError("Некорректный ответ переводчика: записи не совпадают с запросом.")
    return {item["id"]: item["sv"].strip() for item in result}


def translate_texts(texts, database):
    """Translate distinct source texts. Returns {text: (swedish, from_cache)}.

    Only results without mechanical problems are cached, keyed by the exact
    source text, so an edited description never reuses an old translation.
    """
    unique = []
    for text in texts:
        text = (text or "").strip()
        if text and text not in unique:
            unique.append(text)
    results = {}
    missing = []
    for text in unique:
        if _key(text) in _cache:
            results[text] = (_cache[_key(text)], True)
        else:
            missing.append(text)
    for text in missing:
        if len(text) > MAX_TEXT:
            raise TranslationError(f"Описание длиннее {MAX_TEXT} символов; переведите его вручную.")
    for start in range(0, len(missing), MAX_ITEMS):
        batch = missing[start:start + MAX_ITEMS]
        items = [("t" + str(index), text) for index, text in enumerate(batch)]
        translated = _request(items, database)
        for identifier, text in items:
            swedish = translated[identifier]
            results[text] = (swedish, False)
            if not translation_problems(text, swedish):
                _cache[_key(text)] = swedish
    return results
