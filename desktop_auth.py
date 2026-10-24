"""Пользовательские папки и подключение настольного клиента к серверу (без секретов в коде)."""
import json
import os
import sys
from pathlib import Path

import requests

DEFAULT_SERVER_URL = "https://smart-report-server.onrender.com"
_token = ""
_user = {}


def current_user():
    return dict(_user)


def saved_account_role():
    path = app_data_dir() / "account-role.dat"
    if not path.is_file():
        return None
    try:
        value = _unprotect(path.read_bytes())
        return value.decode("ascii") if value else "unknown"
    except (OSError, UnicodeError):
        return "unknown"


class AuthError(Exception):
    """Сообщение пригодно для показа пользователю."""


def isolated_candidate_mode():
    marker = os.environ.get("SMARTREPORT_CANDIDATE_PROFILE", "").strip()
    if not marker:
        return False
    path = Path(marker).resolve()
    profile = json.loads(path.read_text(encoding="utf-8"))
    database = Path(os.environ.get("SMARTREPORT_DB_FILE", "")).resolve()
    if (profile.get("isolated_candidate") is not True
            or database != Path(profile["database"]).resolve()
            or database.parent != path.parent
            or database == Path(profile["source_database"]).resolve()
            or not database.is_file()
            or os.environ.get("SMARTREPORT_TRIAL_MODE") != "1"
            or os.environ.get("SMARTREPORT_CLOUD_URL") != "http://127.0.0.1:5999"
            or Path(os.environ.get("SMARTREPORT_DATA_DIR", "")).resolve() != path.parent / "profile"):
        raise AuthError("Неверный изолированный профиль кандидата. Рабочая база не открыта.")
    return True


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
    expected_dataset = os.environ.get("SMARTREPORT_EXPECTED_DATASET")
    if expected_dataset:
        from sync_dataset import file_dataset
        database_path = os.environ.get("SMARTREPORT_DB_FILE")
        if not database_path or file_dataset(database_path) != expected_dataset:
            raise AuthError("База не соответствует согласованному рабочему профилю. Запуск остановлен.")
    if getattr(sys, "frozen", False):
        install = Path(sys.executable).resolve().parent
        if (install / "paired_install.json").is_file() and "--diagnostic" not in sys.argv:
            import sqlite3
            from contextlib import closing
            from sync_dataset import dataset_id
            data = Path(os.environ["LOCALAPPDATA"]) / install.name
            marker = data / "working_profile.json"
            if not marker.is_file():
                raise AuthError("Рабочий профиль обновления не подготовлен. База не открыта.")
            profile = json.loads(marker.read_text(encoding="utf-8-sig"))
            database = data / "smart_report.db"
            if (profile.get("approved_transition") is not True
                    or profile.get("server_url") != DEFAULT_SERVER_URL
                    or not database.is_file()):
                raise AuthError("Неверный согласованный профиль обновления.")
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
                if not profile.get("dataset_id") or dataset_id(conn) != profile["dataset_id"]:
                    raise AuthError("База не соответствует согласованному переходу.")
            os.environ.update({
                "SMARTREPORT_DB_FILE": str(database),
                "SMARTREPORT_DATA_DIR": str(data / "profile"),
                "SMARTREPORT_DOCUMENTS_DIR": str(data / "documents"),
                "SMARTREPORT_MAIL_SETTINGS_FILE": str(data / "mail_settings.json"),
                "SMARTREPORT_CLOUD_URL": profile["server_url"],
            })
            for key in ("SYNC_API_KEY", "SMARTREPORT_TRIAL_MODE", "SMARTREPORT_CANDIDATE_PROFILE"):
                os.environ.pop(key, None)
            os.chdir(data)
            return True
        if (install / "candidate_install.json").is_file() and "--diagnostic" not in sys.argv \
                and os.environ.get("SMARTREPORT_ASSISTANT_CANDIDATE") != "1":
            data = Path(os.environ["LOCALAPPDATA"]) / install.name
            marker = data / "candidate_profile.json"
            if not marker.is_file():
                raise AuthError("Тестовый профиль не подготовлен. Рабочая база не открыта.")
            profile = json.loads(marker.read_text(encoding="utf-8"))
            os.environ.update({
                "SMARTREPORT_DB_FILE": str(data / "smart_report.db"),
                "SMARTREPORT_DATA_DIR": str(data / "profile"),
                "SMARTREPORT_DOCUMENTS_DIR": str(data / "documents"),
                "SMARTREPORT_MAIL_SETTINGS_FILE": str(data / "mail_settings.json"),
                "SMARTREPORT_CLOUD_URL": "http://127.0.0.1:5999",
                "SMARTREPORT_TRIAL_MODE": "1",
                "SMARTREPORT_CANDIDATE_PROFILE": str(marker),
                "SMARTREPORT_CANDIDATE_VERSION": profile.get("release_version") or "1.0.9-rc1",
            })
            os.environ.pop("SYNC_API_KEY", None)
            isolated_candidate_mode()
            os.chdir(data)
            return True
    if getattr(sys, "frozen", False) and not os.environ.get("SMARTREPORT_DB_FILE"):
        raise AuthError(
            "Рабочий профиль не указан. Запустите SmartReport через установленный ярлык "
            "согласованного профиля (launch_existing_profile.ps1). "
            "Прямой запуск EXE не открывает другую базу и не создаёт новую."
        )
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
        global _user
        _user = response.json().get("user", {})
        protected = _protect(str(_user.get("role", "unknown")).encode("ascii"))
        if protected is not None:
            (app_data_dir() / "account-role.dat").write_bytes(protected)
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
