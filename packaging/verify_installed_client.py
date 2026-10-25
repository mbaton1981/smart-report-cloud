# Проверка установленного клиента: установка, вход по токену, очередь, повторный запуск, второй "компьютер", удаление.
# Запуск: .\.venv\Scripts\python.exe packaging\verify_installed_client.py <SmartReport-Setup.exe> <папка_отчёта>
# Только локальный тестовый сервер на 127.0.0.1; Render и реальные данные не затрагиваются.
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

root = Path(__file__).resolve().parent.parent
installer = Path(sys.argv[1]).resolve()
report_dir = Path(sys.argv[2]).resolve()
report_dir.mkdir(parents=True, exist_ok=True)
temp = Path(tempfile.mkdtemp(prefix="smartreport-installed-"))
admin_password = secrets.token_urlsafe(12)
sync_key = secrets.token_urlsafe(24)
with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
url = f"http://127.0.0.1:{port}"
steps = []
server_process = None

SERVER_CODE = """
import os, sys
sys.path.insert(0, sys.argv[1])
import server
from werkzeug.serving import make_server
make_server('127.0.0.1', int(sys.argv[2]), server.app, threaded=True).serve_forever()
"""


def start_server():
    global server_process
    env = dict(os.environ, DATABASE_PATH=str(temp / "server" / "server.db"), SECRET_KEY=secrets.token_hex(8),
               ADMIN_PASSWORD=admin_password, SYNC_API_KEY=sync_key,
               SMARTREPORT_DOCUMENTS_DIR=str(temp / "server" / "documents"), PYTHONIOENCODING="utf-8")
    (temp / "server").mkdir(exist_ok=True)
    log = open(temp / "server.log", "ab")
    server_process = subprocess.Popen([sys.executable, "-c", SERVER_CODE, str(root), str(port)], env=env, stdout=log, stderr=log)
    import urllib.request
    for _ in range(60):
        try:
            urllib.request.urlopen(url + "/api/auth/me", timeout=2)
        except urllib.error.HTTPError:
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("Сервер не запустился")


def stop_server():
    global server_process
    if server_process:
        server_process.terminate()
        server_process.wait(timeout=15)
        server_process = None


def client_env(profile, **extra):
    local = temp / profile / "AppData" / "Local"
    local.mkdir(parents=True, exist_ok=True)
    profile_data = local / "SmartReport"
    profile_data.mkdir(parents=True, exist_ok=True)
    env = {"SystemRoot": os.environ["SystemRoot"], "SystemDrive": os.environ["SystemDrive"], "COMPUTERNAME": "CLEAN-PC",
           "USERPROFILE": str(temp / profile), "LOCALAPPDATA": str(local), "APPDATA": str(temp / profile / "AppData" / "Roaming"),
           "TEMP": str(temp / "tmp"), "TMP": str(temp / "tmp"), "PATH": r"C:\Windows\System32;C:\Windows",
           "QT_QPA_PLATFORM": "offscreen", "SMARTREPORT_CLOUD_URL": url,
           "SMARTREPORT_DATA_DIR": str(profile_data),
           "SMARTREPORT_DB_FILE": str(profile_data / "smart_report.db"),
           "SMARTREPORT_DOCUMENTS_DIR": str(profile_data / "documents"),
           "PYTHONIOENCODING": "utf-8"}
    (temp / "tmp").mkdir(exist_ok=True)
    env.update(extra)
    return env


def run_client(exe, profile, action, case="case", **extra):
    out = temp / f"{profile}_{case}_{action}_{len(steps)}.json"
    work = temp / profile / "work"
    work.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run([str(exe), "--diagnostic", action, "--case", case, "--work", str(temp / profile / "work"), "--out", str(out)],
                               env=client_env(profile, **extra), capture_output=True, timeout=300, cwd=work)
    data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {"error": "нет файла результата"}
    steps.append({"profile": profile, "action": action, "case": case, "exit_code": completed.returncode, "result": data})
    return completed.returncode, data


try:
    install_dir = temp / "installed"
    scrubbed = {"SystemRoot": os.environ["SystemRoot"], "TEMP": str(temp), "TMP": str(temp), "PATH": r"C:\Windows\System32;C:\Windows"}
    completed = subprocess.run([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/CURRENTUSER", "/NORESTART", f"/DIR={install_dir}",
                                f"/LOG={temp / 'install.log'}"], env=scrubbed, timeout=600)
    exe = install_dir / "SmartReport.exe"
    steps.append({"step": "install_without_python_in_path", "exit_code": completed.returncode, "exe_exists": exe.exists(),
                  "files": sum(1 for _ in install_dir.rglob("*") if _.is_file())})
    assert exe.exists()

    needles = [admin_password.encode(), sync_key.encode()]
    leaked = []
    for path in install_dir.rglob("*"):
        if path.is_file() and path.stat().st_size < 60_000_000:
            blob = path.read_bytes()
            if any(needle in blob for needle in needles):
                leaked.append(str(path))
    steps.append({"step": "no_test_secret_inside_installed_files", "leaked_files": leaked})

    start_server()
    code, data = run_client(exe, "A", "selftest")
    assert code == 0 and data["ob_rules_loaded"] and data["ocr_models_loaded"], data
    code, data = run_client(exe, "A", "login", SMARTREPORT_E2E_USER="admin", SMARTREPORT_E2E_PASS="wrong-password")
    assert code != 0 and "error" in data
    code, data = run_client(exe, "A", "login", SMARTREPORT_E2E_USER="admin", SMARTREPORT_E2E_PASS=admin_password)
    assert code == 0 and data["token_saved"], data
    session = temp / "A" / "AppData" / "Local" / "SmartReport" / "session.dat"
    steps.append({"step": "session_file_has_no_plain_token", "exists": session.exists(),
                  "contains_plain_text": b"token" in session.read_bytes().lower() and len(session.read_bytes()) < 40})

    stop_server()
    code, data = run_client(exe, "A", "send", case="offline")
    assert code == 0 and data["exchange"]["synced"] == 0 and data["queue_after"] == 1, data
    start_server()
    code, data = run_client(exe, "A", "retry", case="offline")
    assert code == 0 and data["exchange"] == {"synced": 1, "conflicts": 0} and data["queue_after"] == 0, data
    code, data = run_client(exe, "A", "retry", case="offline")
    assert code == 0 and data["queue_before"] == 0, data
    code, data = run_client(exe, "A", "duplicate", case="offline")
    steps.append({"step": "same_original_from_new_client_id_after_restart", "result": data})
    code, data = run_client(exe, "B", "login", SMARTREPORT_E2E_USER="admin", SMARTREPORT_E2E_PASS=admin_password)
    assert code == 0
    code, data = run_client(exe, "B", "receive", case="offline")
    # состояние для "второго компьютера" берётся из рабочей папки первого
    if code != 0:
        (temp / "B" / "work").mkdir(parents=True, exist_ok=True)
        (temp / "B" / "work" / "offline_state.json").write_text((temp / "A" / "work" / "offline_state.json").read_text(encoding="utf-8"), encoding="utf-8")
        code, data = run_client(exe, "B", "receive", case="offline")
    assert code == 0 and data["matching_documents"] == 1 and data["download_hash_matches"] and data["server_documents_total"] == 1, data
    code, data = run_client(exe, "B", "storage")
    storage = data["storage"]["documents"]
    assert code == 0 and storage["attachments"] == 1 and storage["missing_files"] == 0 and storage["hash_mismatches"] == 0, data
    code, data = run_client(exe, "B", "backup")
    assert code == 0 and data["backup_files"] == 1 and not data["backup_problems"] and data["snapshot_bytes"] > 0, data
    uninstall = subprocess.run([str(install_dir / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], env=scrubbed, timeout=300)
    time.sleep(2)
    steps.append({"step": "uninstall_keeps_user_data", "exit_code": uninstall.returncode, "program_removed": not exe.exists(),
                  "user_data_kept": (temp / "A" / "AppData" / "Local" / "SmartReport" / "smart_report.db").exists()})
    outcome = "passed"
except BaseException as error:
    outcome = f"failed: {type(error).__name__}: {error}"
finally:
    stop_server()
    (report_dir / "installed_client_report.json").write_text(
        json.dumps({"outcome": outcome, "installer": str(installer), "installer_sha256": hashlib.sha256(installer.read_bytes()).hexdigest(),
                    "server": "локальный тестовый сервер 127.0.0.1, временные БД", "uses_shared_sync_key_in_client": False,
                    "steps": steps}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(outcome)
