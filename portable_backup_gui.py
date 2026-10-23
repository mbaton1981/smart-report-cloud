"""Create portable encrypted server snapshots in the owner's chosen storage."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from contextlib import closing
import requests
from cryptography.fernet import InvalidToken

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFormLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QVBoxLayout,
)

import portable_recovery as recovery


class BackupWorker(QThread):
    complete = pyqtSignal(dict)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, args, password):
        super().__init__()
        self.args, self.password = args, password

    def run(self):
        stage = "Подготовка"
        try:
            args = self.args
            os.environ["SMARTREPORT_DATA_DIR"] = str(args.profile / "profile")
            os.environ["SMARTREPORT_DB_FILE"] = str(args.profile / "smart_report.db")
            import desktop_auth
            import server_backup
            from sync_dataset import file_dataset
            from cryptography.fernet import Fernet

            protected = args.protected_recovery
            key = desktop_auth._unprotect((protected / "card-key.dpapi").read_bytes())
            config = json.loads(desktop_auth._unprotect((protected / "configuration.dpapi").read_bytes()))
            required = ("SECRET_KEY", "SYNC_API_KEY", "ADMIN_PASSWORD", "ADMIN_USERNAME")
            config = {name: config[name] for name in required if name in config}
            archive_bytes = subprocess.check_output(
                ["git", "-C", str(Path(__file__).parent), "archive", "--format=zip", args.commit]
            )
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                source = {"source/" + name: archive.read(name) for name in archive.namelist()
                          if not name.endswith("/")}
            source.update({
                "recovery/employee-card.key": key,
                "recovery/configuration.json": json.dumps(config).encode(),
                "recovery/release.json": json.dumps({"commit": args.commit}).encode(),
            })
            verifier = Path(__file__).with_name("portable_recovery.py")
            # No unencrypted snapshot, key or configuration is written to OneDrive.
            args.destination.mkdir(parents=True, exist_ok=True)
            (args.destination / "portable_recovery.py").write_bytes(verifier.read_bytes())
            (args.destination / "RECOVERY-REQUIREMENTS.txt").write_bytes(
                (Path(__file__).parent / "requirements.txt").read_bytes()
            )
            (args.destination / "VERIFIED-DEPENDENCIES.txt").write_bytes(
                subprocess.check_output(
                    [str(args.verifier), "-I", "-m", "pip", "freeze"], timeout=60,
                )
            )
            (args.destination / "PORTABLE_RECOVERY.md").write_bytes(
                Path(__file__).with_name("PORTABLE_RECOVERY.md").read_bytes()
            )
            created = []
            for number in range(2):
                stage = f"Снимок сервера {number + 1}/2"
                self.progress.emit(stage)
                with tempfile.TemporaryDirectory(prefix="smartreport-backup-") as temp:
                    if os.name == "nt":
                        identity = subprocess.check_output(["whoami"]).decode().strip()
                        subprocess.run(
                            ["icacls", temp, "/inheritance:r", "/grant:r", identity + ":(OI)(CI)F",
                             "*S-1-5-18:(OI)(CI)F"], check=True, capture_output=True,
                        )
                    snapshot, manifest = server_backup.pull_backup(
                        desktop_auth.server_url(), desktop_auth.load_saved_token(), Path(temp),
                        expected_dataset=file_dataset(args.profile / "smart_report.db"),
                    )
                    if manifest["status"] != "complete":
                        raise recovery.RecoveryError("Incomplete server backup.")
                    with closing(sqlite3.connect(
                            (snapshot / "server_snapshot.db").as_uri() + "?mode=ro", uri=True)) as conn:
                        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise recovery.RecoveryError("Invalid database snapshot.")
                        cards = conn.execute(
                            "SELECT card_cipher FROM meta_employees WHERE card_cipher IS NOT NULL "
                            "AND card_cipher != ''"
                        ).fetchall()
                        if not cards:
                            raise recovery.RecoveryError("No encrypted cards to verify.")
                        for row in cards:
                            Fernet(key).decrypt(row[0].encode())
                    files = dict(source)
                    files.update({
                        "backup/" + str(path.relative_to(snapshot)).replace("\\", "/"): path.read_bytes()
                        for path in snapshot.rglob("*")
                        if path.is_file() and not path.name.endswith(("-wal", "-shm"))
                    })
                    stage = "Шифрование и проверка записанной версии"
                    self.progress.emit(stage)
                    bundle = recovery.publish(args.destination, files, self.password)
                stage = "Восстановление в независимой среде"
                self.progress.emit(stage)
                with tempfile.TemporaryDirectory(prefix="smartreport-clean-profile-") as clean:
                    environment = {
                        "SystemRoot": os.environ["SystemRoot"], "TEMP": clean, "TMP": clean,
                        "USERPROFILE": clean, "HOME": clean, "APPDATA": clean, "LOCALAPPDATA": clean,
                    }
                    check = subprocess.run(
                        [str(args.verifier), "-I", str(verifier), str(bundle),
                         "--verify-only", "--password-stdin"],
                        input=(self.password + "\n").encode("utf-8"), env=environment, cwd=clean,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
                    )
                    if check.returncode:
                        raise recovery.RecoveryError("Independent verifier failed.")
                    result = json.loads(check.stdout.decode("utf-8"))
                    if result["dpapi_used"] or not result["all_business_table_hashes_unchanged"]:
                        raise recovery.RecoveryError("Independent verification did not pass.")
                created.append({
                    "file": bundle.name, "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                    "server_snapshot_sha256": manifest["snapshot_sha256"], "verification": result,
                })
            proof = {
                "format": 1, "compatible_commit": args.commit, "versions": created,
                "password_stored": False, "decryption_custody": "Owner's independent password manager",
                "onedrive_cloud_upload_confirmed": False,
                "test_environment": "Fresh isolated venv, empty disposable Windows profile paths, "
                                    "network blocked by verifier; same physical host",
                "production_business_data_modified": False,
                "normal_auth_access_metadata_may_update": True,
                "retention": "Append-only unique filenames; no automatic deletion or replacement",
            }
            report = json.dumps(proof, ensure_ascii=False, indent=2)
            report_name = "verification-" + created[-1]["file"].removeprefix("SmartReport-").removesuffix(".srbackup") + ".json"
            with (args.destination / report_name).open("x", encoding="utf-8") as output:
                output.write(report)
            (args.destination / "verification.json").write_text(report, encoding="utf-8")
            self.complete.emit(proof)
        except (OSError, ValueError, RuntimeError, KeyError, InvalidToken,
                requests.RequestException, sqlite3.Error, subprocess.SubprocessError):
            self.failed.emit(
                stage + ": операция не завершена. Старые копии не удалены. "
                "Проверьте подключение к серверу, место на диске и среду проверки. "
                "Не считайте новую копию проверенной без verification.json."
            )
        finally:
            self.password = None


class BackupDialog(QDialog):
    def reject(self):
        if not hasattr(self, "worker") or not self.worker.isRunning():
            super().reject()

    def closeEvent(self, event):
        if hasattr(self, "worker") and self.worker.isRunning():
            event.ignore()
        else:
            super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--protected-recovery", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--verifier", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    dialog = BackupDialog()
    dialog.setWindowTitle("SmartReport — независимое аварийное восстановление")
    dialog.setMinimumWidth(620)
    layout = QVBoxLayout(dialog)
    description = QLabel(
        "Создать две новые зашифрованные версии базы, ключа и конфигурации.\n"
        "Рабочие данные не изменяются. Старые копии не удаляются.\n\n"
        "1. Создайте и сохраните уникальный длинный пароль в менеджере паролей.\n"
        "2. Убедитесь, что менеджер доступен без этого компьютера и Windows.\n"
        "3. Введите пароль ниже (не в переписку). Минимум 16 символов.\n\n"
        "OneDrive: " + str(args.destination)
    )
    description.setWordWrap(True)
    layout.addWidget(description)
    form = QFormLayout()
    password, repeat = QLineEdit(), QLineEdit()
    for edit in (password, repeat):
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        edit.setMaxLength(1024)
    form.addRow("Пароль восстановления:", password)
    form.addRow("Повторите пароль:", repeat)
    layout.addLayout(form)
    acknowledged = QCheckBox("Пароль сохранён отдельно, доступ к менеджеру не зависит от этого ПК")
    layout.addWidget(acknowledged)
    status = QLabel("Пароль не сохраняется программой и не передаётся в аргументах команд.")
    status.setWordWrap(True)
    layout.addWidget(status)
    start = QPushButton("Создать копии и проверить восстановление")
    layout.addWidget(start)
    def begin():
        secret = password.text()
        if not acknowledged.isChecked() or len(secret) < 16 or secret != repeat.text() or "\n" in secret or "\r" in secret:
            QMessageBox.warning(dialog, "Проверьте пароль",
                                "Нужны одинаковые пароли от 16 символов без перевода строки "
                                "и подтверждение отдельного хранения.")
            return
        start.setEnabled(False)
        acknowledged.setEnabled(False)
        worker = BackupWorker(args, secret)
        dialog.worker = worker
        password.clear()
        repeat.clear()
        password.setEnabled(False)
        repeat.setEnabled(False)
        worker.progress.connect(status.setText)

        def finish(proof):
            status.setText("Проверено: две версии. Осталось подтвердить загрузку OneDrive.")
            QMessageBox.information(
                dialog, "Копии созданы и проверены",
                "Обе версии расшифрованы и база с карточками открыта в чистой среде без DPAPI.\n\n"
                "ВАЖНО: проверьте загрузку через сайт OneDrive с другого устройства. "
                "Локальная папка не подтверждает загрузку в облако.\n"
                "Пароль храните только в своём менеджере паролей.",
            )
            dialog.accept()

        def fail(message):
            status.setText(message)
            QMessageBox.critical(dialog, "Резервная копия не завершена", message)
            dialog.reject()

        worker.complete.connect(finish)
        worker.failed.connect(fail)
        worker.start()

    start.clicked.connect(begin)
    result = dialog.exec()
    return 0 if result == QDialog.DialogCode.Accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
