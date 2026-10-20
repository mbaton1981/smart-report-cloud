"""Verify an installed 1.0.7 -> 1.0.8 upgrade using isolated data and localhost only."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

import requests


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    old_installer, new_installer, report_dir = map(Path, sys.argv[1:])
    root = Path(__file__).resolve().parent.parent
    report_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="smartreport-upgrade-1.0.8-"))
    install_dir = work / "installed"
    data_dir = work / "profile" / "SmartReport"
    data_dir.mkdir(parents=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    password, key = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    server_db = work / "server.db"
    env = {
        "SystemRoot": os.environ["SystemRoot"],
        "PATH": r"C:\Windows\System32;C:\Windows",
        "TEMP": str(work), "TMP": str(work),
        "LOCALAPPDATA": str(work / "profile"),
        "APPDATA": str(work / "roaming"),
        "USERPROFILE": str(work / "user"),
        "QT_QPA_PLATFORM": "offscreen",
        "SMARTREPORT_DATA_DIR": str(data_dir),
        "SMARTREPORT_CLOUD_URL": url,
        "SMARTREPORT_DOCUMENTS_DIR": str(data_dir / "SupplierDocuments"),
        "PYTHONIOENCODING": "utf-8",
    }
    server_env = dict(
        os.environ, DATABASE_PATH=str(server_db), SYNC_API_KEY=key,
        SECRET_KEY=secrets.token_hex(24), ADMIN_PASSWORD=password,
        SMARTREPORT_DOCUMENTS_DIR=str(work / "server_documents"),
        PYTHONIOENCODING="utf-8",
    )
    steps = []
    server_process = None
    server_log = None

    def record(name, **values):
        steps.append({"step": name, **values})

    def install(installer, label):
        completed = subprocess.run([
            str(installer.resolve()), "/VERYSILENT", "/SUPPRESSMSGBOXES",
            "/CURRENTUSER", "/NORESTART", f"/DIR={install_dir}",
            f"/LOG={work / (label + '_install.log')}",
        ], env=env, timeout=600)
        if completed.returncode != 0 or not (install_dir / "SmartReport.exe").is_file():
            raise RuntimeError(f"{label} install failed: {completed.returncode}")
        record(label + "_installed", exit_code=completed.returncode,
               exe_sha256=sha256(install_dir / "SmartReport.exe"))

    def client(action, expect_success=True, **extra):
        output = work / f"client_{len(steps)}_{action}.json"
        completed = subprocess.run([
            str(install_dir / "SmartReport.exe"), "--diagnostic", action,
            "--work", str(work / "client_work"), "--case", "upgrade",
            "--out", str(output),
        ], env=dict(env, **extra), capture_output=True, timeout=300)
        result = json.loads(output.read_text(encoding="utf-8")) if output.is_file() else {}
        record("installed_client_" + action, exit_code=completed.returncode, result=result)
        if expect_success != (completed.returncode == 0):
            raise RuntimeError(f"{action} unexpected result: {result}")
        return result

    def start_server():
        nonlocal server_process, server_log
        server_log = (work / "server.log").open("ab")
        code = (
            "import sys;sys.path.insert(0,sys.argv[1]);import server;"
            "from werkzeug.serving import make_server;"
            "make_server('127.0.0.1',int(sys.argv[2]),server.app,threaded=True).serve_forever()"
        )
        server_process = subprocess.Popen(
            [sys.executable, "-c", code, str(root), str(port)],
            env=server_env, stdout=server_log, stderr=server_log,
        )
        for _ in range(60):
            if server_process.poll() is not None:
                raise RuntimeError("Isolated server exited; see server.log")
            try:
                response = requests.get(url + "/api/auth/me", timeout=1)
                if response.status_code in (200, 401):
                    return
            except requests.ConnectionError:
                pass
            time.sleep(0.5)
        raise RuntimeError("Isolated server not ready")

    def stop_server():
        nonlocal server_process, server_log
        if server_process:
            server_process.terminate()
            server_process.wait(timeout=20)
            server_process = None
        if server_log:
            server_log.close()
            server_log = None

    def snapshot_files():
        return {str(path.relative_to(data_dir)): sha256(path)
                for path in data_dir.rglob("*") if path.is_file()}

    def server_shift():
        with sqlite3.connect(server_db) as conn:
            return conn.execute(
                "SELECT version,is_deleted,last_operation_id FROM incoming_submissions WHERE sync_id=?",
                (sync_id,),
            ).fetchall()

    outcome = "failed"
    try:
        install(old_installer, "1.0.7")
        start_server()
        client("selftest")
        client("login", SMARTREPORT_E2E_USER="admin", SMARTREPORT_E2E_PASS=password)
        settings = data_dir / "connection.json"
        settings.write_text(json.dumps({"server_url": url, "username": "admin"}), encoding="utf-8")
        stop_server()
        offline = client("send")
        if offline["queue_after"] != 1:
            raise RuntimeError("Old client did not preserve its offline expense operation")
        attachment_dir = data_dir / "SupplierDocuments"
        attachment_dir.mkdir(exist_ok=True)
        attachment = attachment_dir / "upgrade_preservation.pdf"
        attachment.write_bytes((work / "client_work" / "upgrade.pdf").read_bytes())
        database = data_dir / "smart_report.db"
        sync_id, operation_id = str(uuid.uuid4()), str(uuid.uuid4())
        payload = {
            "date": "2026-10-16", "employee": "Upgrade Test Employee",
            "company": "Upgrade Test Company", "object_name": "Upgrade Test Object",
            "hours": 8.0, "rate": 100.0, "transport": 0.0,
            "comment": "Isolated upgrade fixture", "record_type": "work", "invoiced": 0,
        }
        with sqlite3.connect(database) as conn:
            conn.execute(
                "INSERT INTO companies(name) VALUES (?)", (payload["company"],)
            )
            conn.execute(
                "INSERT INTO employees(name,salary_rate) VALUES (?,100)", (payload["employee"],)
            )
            conn.execute("""
                INSERT INTO objects(name,company,markning,rate,transport_rate,is_active)
                VALUES (?,?,'upgrade-test',100,0,1)
            """, (payload["object_name"], payload["company"]))
            conn.execute("""
                INSERT INTO shifts(date,employee,company,object_name,hours,rate,transport,
                                   comment,invoiced,sync_id,sync_version,entry_type)
                VALUES (?,?,?,?,8,100,0,?,0,?,0,'work')
            """, (payload["date"], payload["employee"], payload["company"],
                  payload["object_name"], payload["comment"], sync_id))
            conn.execute("""
                INSERT INTO shift_sync_outbox(operation_id,sync_id,operation,base_version,
                                              payload_json,state,created_at)
                VALUES (?,?,'upsert',0,?,'pending','2026-10-20T00:00:00')
            """, (operation_id, sync_id, json.dumps(payload)))
        before = snapshot_files()
        old_exe_hash = sha256(install_dir / "SmartReport.exe")
        install(new_installer, "1.0.8")
        if snapshot_files() != before:
            raise RuntimeError("Installer changed profile files before first launch")
        if sha256(install_dir / "SmartReport.exe") == old_exe_hash:
            raise RuntimeError("Executable was not replaced by the upgrade")
        record("upgrade_preserves_all_profile_files", file_count=len(before),
               database=True, settings=True, session=True, attachments=True)
        client("selftest")
        if sha256(settings) != before["connection.json"] or sha256(attachment) != before[str(attachment.relative_to(data_dir))]:
            raise RuntimeError("First launch changed settings or preserved attachment")
        failed_sync = client("sync", expect_success=False)
        with sqlite3.connect(database) as conn:
            if conn.execute("SELECT COUNT(*) FROM shift_sync_outbox").fetchone()[0] != 1:
                raise RuntimeError("Offline sync lost the queued shift")
            if conn.execute("SELECT COUNT(*) FROM expense_document_outbox").fetchone()[0] != 1:
                raise RuntimeError("Offline sync lost the queued expense")
        record("offline_full_sync_keeps_both_queues", failed=bool(failed_sync.get("error")))
        start_server()
        with sqlite3.connect(server_db) as conn:
            conn.execute("INSERT INTO meta_companies(name) VALUES (?)", (payload["company"],))
            conn.execute("""
                INSERT INTO meta_objects(name,company,markning,rate,transport_rate,is_active)
                VALUES (?,?,'upgrade-test',100,0,1)
            """, (payload["object_name"], payload["company"]))
            conn.execute("INSERT INTO meta_employees(name,salary_rate,is_active) VALUES (?,100,1)",
                         (payload["employee"],))
        synced = client("sync")
        if any(synced["queues"].values()) or synced["exchange"]["conflicts"]:
            raise RuntimeError("Full installed sync left a queue or conflict")
        first = server_shift()
        if len(first) != 1 or first[0][:2] != (1, 0):
            raise RuntimeError("Expected exactly one live server shift at version 1")
        repeated = client("sync")
        if any(repeated["queues"].values()) or server_shift() != first:
            raise RuntimeError("Repeated sync changed or duplicated the shift")
        with sqlite3.connect(server_db) as conn:
            if conn.execute("SELECT COUNT(*) FROM expense_documents WHERE is_deleted=0").fetchone()[0] != 1:
                raise RuntimeError("Expense was missing or duplicated")
        record("full_sync_queues_empty_no_duplicates", shift_rows=1, expense_rows=1)
        delete_id = str(uuid.uuid4())
        with sqlite3.connect(server_db) as conn:
            conn.execute("DELETE FROM meta_objects WHERE name=?", (payload["object_name"],))
        with sqlite3.connect(database) as conn:
            conn.execute("""
                INSERT INTO shift_sync_outbox(operation_id,sync_id,operation,base_version,
                                              payload_json,state,created_at)
                VALUES (?,?,'delete',1,?,'pending','2026-10-20T00:00:01')
            """, (delete_id, sync_id, json.dumps(payload)))
        deleted = client("sync")
        if any(deleted["queues"].values()) or server_shift() != [(2, 1, delete_id)]:
            raise RuntimeError("Delete did not finish after catalog object removal")
        client("sync")
        if server_shift() != [(2, 1, delete_id)]:
            raise RuntimeError("Repeated delete sync was not stable")
        with sqlite3.connect(database) as conn:
            if conn.execute("SELECT COUNT(*) FROM shifts WHERE sync_id=?", (sync_id,)).fetchone()[0] != 0:
                raise RuntimeError("Deleted shift was not removed by normal pull")
        record("deleted_shift_reconciled_without_duplicate", server_rows=1, version=2)
        storage = client("storage")["storage"]["documents"]
        if storage["missing_files"] or storage["hash_mismatches"]:
            raise RuntimeError("Attachment verification failed")
        client("backup")
        record("upgrade_settings_session_attachment_preserved", settings=True,
               session=True, local_attachment=True, server_attachment_hash=True)
        outcome = "passed"
    except Exception as error:
        record("failure", error=f"{type(error).__name__}: {error}")
    finally:
        stop_server()
        report = {
            "outcome": outcome, "isolated_directory": str(work), "server": url,
            "old_installer_sha256": sha256(old_installer),
            "new_installer_sha256": sha256(new_installer), "steps": steps,
        }
        (report_dir / "upgrade_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(outcome, flush=True)
        if outcome != "passed":
            print(json.dumps(steps[-1], ensure_ascii=True), flush=True)
    return 0 if outcome == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
