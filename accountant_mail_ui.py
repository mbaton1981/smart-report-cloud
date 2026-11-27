"""Compose a Loopia message or inspect the saved accountant packages."""
from pathlib import Path
import tempfile

from PyQt6.QtCore import QEventLoop, Qt, QThreadPool
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPlainTextEdit, QProgressDialog, QPushButton, QVBoxLayout,
)

import accountant_mail as mail
import info_mail
from payroll_preview import open_preview


def compose(parent, obj, period, path, desktop):
    from object_card_ui import InfoMailConnectionDialog
    saved = info_mail.load_settings()
    if not saved:
        connection = InfoMailConnectionDialog(parent)
        connection.host.setText(mail.MAIL_HOST)
        if not connection.exec():
            return
        saved = info_mail.load_settings()
    if not saved:
        return
    settings, password = saved
    if settings.host.strip().lower() != mail.MAIL_HOST:
        raise ValueError("Сохранённый ящик info использует другой почтовый сервер. Проверьте настройки подключения Loopia.")
    with mail.archive() as db:
        previous = db.execute("SELECT recipient FROM mail WHERE sender=? AND status='sent' ORDER BY id DESC LIMIT 1",
                              (settings.email_address,)).fetchone()
    dialog = QDialog(parent)
    dialog.setWindowTitle("Отправить бухгалтеру")
    dialog.resize(650, 380)
    layout = QVBoxLayout(dialog)
    form = QFormLayout()
    recipient = QLineEdit(previous["recipient"] if previous else "")
    subject = QLineEdit(f"Faktureringsunderlag · {obj['name']} · {period}")
    body = QPlainTextEdit(f"Hej!\n\nHär kommer underlaget för fakturering av {obj['name']} för perioden {period}.\n")
    form.addRow("От кого", QLabel(settings.email_address))
    form.addRow("Кому", recipient)
    form.addRow("Тема", subject)
    form.addRow("Письмо", body)
    attachment = QPushButton(f"Посмотреть PDF · {path.name}")
    attachment.clicked.connect(lambda: open_preview(path, Path(desktop.REPORTS_DIR) / path.name, dialog,
                                                    title="Вложение бухгалтеру"))
    form.addRow("Вложение", attachment)
    layout.addLayout(form)
    buttons = QDialogButtonBox()
    send = buttons.addButton("Отправить", QDialogButtonBox.ButtonRole.AcceptRole)
    cancel = buttons.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
    cancel.setDefault(True)
    send.setAutoDefault(False)
    def validate():
        try:
            mail.address_value(recipient.text())
        except ValueError as error:
            QMessageBox.warning(dialog, "Адрес бухгалтера", str(error))
            return
        dialog.accept()
    send.clicked.connect(validate)
    cancel.clicked.connect(dialog.reject)
    layout.addWidget(buttons)
    if not dialog.exec():
        return
    arguments = (settings, password, recipient.text().strip(), subject.text(), body.toPlainText(),
                 path, obj["server_id"], period)
    loop = QEventLoop()
    outcome = []
    def done(_, result, error):
        outcome.append((result, error))
        loop.quit()
    task = desktop.ObjectPackageTask(0, lambda _update: mail.send_package(*arguments))
    task.signals.completed.connect(done)
    progress = QProgressDialog("Отправка PDF через Loopia…", "", 0, 0, parent)
    progress.setCancelButton(None)
    progress.setWindowModality(Qt.WindowModality.ApplicationModal)
    progress.show()
    QThreadPool.globalInstance().start(task)
    loop.exec()
    progress.close()
    result, error = outcome[0]
    if error:
        QMessageBox.warning(parent, "Отправка бухгалтеру", error)
    else:
        QMessageBox.information(parent, "Письмо принято Loopia",
                                f"Получатель: {arguments[2]}\nКопия PDF сохранена в истории отправок объекта.")
        return result


def show_history(parent, obj, reports_dir):
    with mail.archive() as db:
        rows = db.execute("""SELECT id,created,period,recipient,status FROM mail
            WHERE project=? ORDER BY id DESC""", (obj["server_id"],)).fetchall()
    if not rows:
        QMessageBox.information(parent, "История отправок", "В этом профиле пока нет отправок по объекту.")
        return
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"История отправок бухгалтеру · {obj['name']}")
    dialog.resize(820, 400)
    layout = QVBoxLayout(dialog)
    records = QListWidget()
    for row in rows:
        records.addItem(f"{row['created'][:16].replace('T', ' ')} · {mail.STATUS[row['status']]} · "
                        f"{row['recipient']} · {row['period']}")
    layout.addWidget(records)
    records.setCurrentRow(0)
    view = QPushButton("Открыть сохранённый PDF")
    layout.addWidget(view)
    def preview():
        try:
            with mail.archive() as db:
                item = db.execute("SELECT filename,pdf FROM mail WHERE id=?", (rows[records.currentRow()]["id"],)).fetchone()
            with tempfile.TemporaryDirectory(prefix="smartreport-mail-history-") as folder:
                path = Path(folder) / Path(item["filename"]).name
                path.write_bytes(item["pdf"])
                open_preview(path, Path(reports_dir) / path.name, dialog, title="Сохранённое вложение")
        except Exception as error:
            QMessageBox.warning(dialog, "Не удалось открыть PDF", str(error))
    view.clicked.connect(preview)
    dialog.exec()
