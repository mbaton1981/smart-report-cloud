"""Пользовательские папки и подключение настольного клиента к серверу (без секретов в коде)."""
import json
import os
import sys
from pathlib import Path

import requests

DEFAULT_SERVER_URL = "https://smart-report-server.onrender.com"
_token = ""


class AuthError(Exception):
    """Сообщение пригодно для показа пользователю."""


def app_data_dir():
    configured = os.environ.get("SMARTREPORT_DATA_DIR", "").strip()
    if configured:
        base = Path(configured)
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "SmartReport"
    else:
        base = Path.home() / ".smartreport"
    base.mkdir(parents=True, exist_ok=True)
    return base


def use_data_dir_as_working_directory():
    """Установленная программа хранит базу, отчёты и резервные копии в профиле пользователя."""
    if getattr(sys, "frozen", False) and not os.environ.get("SMARTREPORT_DB_FILE"):
        os.chdir(app_data_dir())
        return True
    return False


def _settings_path():
    return app_data_dir() / "connection.json"


def load_settings():
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**values):
    settings = load_settings()
    settings.update({key: value for key, value in values.items() if value is not None})
    _settings_path().write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def server_url():
    env = os.environ.get("SMARTREPORT_CLOUD_URL", "").strip()
    return (env or load_settings().get("server_url") or DEFAULT_SERVER_URL).rstrip("/")


def _protect(data):
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    source = Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), "SmartReport", None, None, None, 0, ctypes.byref(result)):
        return None
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def _unprotect(data):
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    source = Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)):
        return None
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def _session_path():
    return app_data_dir() / "session.dat"


def current_token():
    return _token


def set_token(token, persist=True):
    """Токен хранится только зашифрованным средствами Windows (DPAPI) для текущего пользователя."""
    global _token
    _token = token or ""
    if not persist:
        return
    if not token:
        _session_path().unlink(missing_ok=True)
        return
    protected = _protect(token.encode("utf-8"))
    if protected is not None:
        _session_path().write_bytes(protected)


def load_saved_token():
    try:
        raw = _session_path().read_bytes()
    except OSError:
        return ""
    plain = _unprotect(raw)
    return plain.decode("utf-8") if plain else ""


def login(url, username, password):
    try:
        response = requests.post(
            f"{url.rstrip('/')}/api/auth/token",
            json={"username": username, "password": password, "device_name": os.environ.get("COMPUTERNAME", "")},
            timeout=30,
        )
    except requests.RequestException:
        raise AuthError("Не удалось связаться с сервером. Проверьте интернет и повторите; сервер может просыпаться до минуты.")
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code == 200 and payload.get("ok") and payload.get("token"):
        return payload["token"]
    if response.status_code in (404, 405):
        raise AuthError("Сервер ещё не поддерживает вход по логину. Обновите серверную часть.")
    raise AuthError(payload.get("error") or f"Сервер отклонил вход (HTTP {response.status_code}).")


def check_token(url, token):
    """True: действует; False: сервер отказал; None: сервер недоступен (токен не сбрасывать)."""
    try:
        response = requests.get(f"{url.rstrip('/')}/api/auth/me", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    except requests.RequestException:
        return None
    if response.status_code == 200:
        return True
    if response.status_code in (401, 403):
        return False
    return None


def logout(url):
    token = _token
    if token:
        try:
            requests.post(f"{url.rstrip('/')}/api/auth/logout", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        except requests.RequestException:
            pass
    set_token("")
