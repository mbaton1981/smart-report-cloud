"""Document and agreement sections embedded in the object card."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from PyQt6.QtCore import QDate, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTextEdit,
    QInputDialog, QVBoxLayout, QWidget, QHeaderView,
)

import info_mail
from object_records import (
    DOCUMENT_CATEGORIES, OFFER_STATUSES, ObjectRecordError, ObjectRecordStore,
)


FILE_FILTER = (
    "Документы и изображения (*.pdf *.doc *.docx *.jpg *.jpeg *.png *.bmp "
    "*.tif *.tiff *.webp *.gif *.heic)"
)


def _format_stockholm_datetime(value: str) -> str:
    if not value:
        return "—"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo("Europe/Stockholm")).strftime("%d.%m.%Y %H:%M")


def _show_error(parent, title: str, error: Exception):
    QMessageBox.warning(parent, title, str(error))


def _open_file(parent, path: Path):
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
        QMessageBox.warning(parent, "Открытие файла", "Windows не смогла открыть выбранное вложение.")


class AddDocumentDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Добавить документ объекта")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.category = QComboBox()
        self.category.addItems(DOCUMENT_CATEGORIES)
        self.version = QSpinBox()
        self.version.setRange(1, 9999)
        self.status = QComboBox()
        self.status.addItems(OFFER_STATUSES)
        form.addRow("Категория:", self.category)
        form.addRow("Версия оферты:", self.version)
        form.addRow("Статус оферты:", self.status)
        layout.addLayout(form)
        self.category.currentTextChanged.connect(self._category_changed)
        self._category_changed(self.category.currentText())
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Добавить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _category_changed(self, category: str):
        is_offer = category == "Оферта"
        self.version.setEnabled(is_offer)
        self.status.setEnabled(is_offer)


class AddAgreementDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Новая договорённость")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.agreement_date = QDateEdit(QDate.currentDate())
        self.agreement_date.setCalendarPopup(True)
        self.agreement_date.setDisplayFormat("yyyy-MM-dd")
        self.text = QTextEdit()
        self.text.setPlaceholderText("Коротко опишите договорённость…")
        self.text.setMaximumHeight(110)
        self.agreed = QCheckBox("Заказчик согласовал")
        self.attachment_path = QLineEdit()
        self.attachment_path.setReadOnly(True)
        attach_row = QHBoxLayout()
        attach_row.addWidget(self.attachment_path, 1)
        choose = QPushButton("Выбрать файл…")
        choose.clicked.connect(self._choose_attachment)
        attach_row.addWidget(choose)
        clear = QPushButton("Убрать")
        clear.clicked.connect(self.attachment_path.clear)
        attach_row.addWidget(clear)
        form.addRow("Дата:", self.agreement_date)
        form.addRow("Текст:", self.text)
        form.addRow("", self.agreed)
        form.addRow("Вложение:", attach_row)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _choose_attachment(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выбрать вложение", "", FILE_FILTER)
        if path:
            self.attachment_path.setText(path)


class InfoMailConnectionDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Подключение отдельного ящика info")
        self.setMinimumSize(720, 460)
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        saved = None
        try:
            saved = info_mail.load_settings()
        except info_mail.InfoMailError as error:
            QMessageBox.warning(self, "Настройки info", str(error))
        current, password = saved if saved else (None, "")
        self.email = QLineEdit(current.email_address if current else "")
        self.email.setMinimumWidth(360)
        self.host = QLineEdit(current.host if current else "")
        self.host.setMinimumWidth(360)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(current.port if current else 993)
        self.security = QComboBox()
        self.security.addItems(("SSL/TLS", "STARTTLS"))
        if current:
            self.security.setCurrentText(current.security)
        self.inbox = QLineEdit(current.inbox_folder if current else "INBOX")
        self.sent = QLineEdit(current.sent_folder if current else "Sent")
        self.password = QLineEdit(password)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Пароль или пароль приложения")
        form.addRow("Адрес ящика:", self.email)
        form.addRow("IMAP-сервер:", self.host)
        form.addRow("Порт:", self.port)
        form.addRow("Защита:", self.security)
        form.addRow("Папка входящих:", self.inbox)
        form.addRow("Папка отправленных:", self.sent)
        form.addRow("Пароль:", self.password)
        layout.addLayout(form)
        notice = QLabel(
            "Пароль хранится отдельно от фактур и защищается Windows DPAPI для этого пользователя. "
            "Подключение только для чтения; отправки писем нет."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        buttons = QDialogButtonBox()
        self.save_button = buttons.addButton(
            "Проверить и сохранить", QDialogButtonBox.ButtonRole.AcceptRole,
        )
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText("Отмена")
        self.save_button.clicked.connect(self._test_and_save)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _current_settings(self):
        settings = info_mail.MailboxSettings(
            email_address=self.email.text().strip(),
            host=self.host.text().strip(),
            port=self.port.value(),
            security=self.security.currentText(),
            inbox_folder=self.inbox.text().strip(),
            sent_folder=self.sent.text().strip(),
        )
        settings.validate()
        return settings

    def _test_and_save(self):
        try:
            settings = self._current_settings()
            password = self.password.text()
            info_mail.test_connection(settings, password)
            info_mail.save_settings(settings, password)
        except (info_mail.InfoMailError, OSError, TimeoutError) as error:
            _show_error(self, "Подключение info не сохранено", error)
            return
        self.password.clear()
        QMessageBox.information(
            self, "Ящик info подключён",
            "Обе папки проверены в режиме только для чтения. Настройки фактур не изменялись.",
        )
        self.accept()


class InfoMailPickerDialog(QDialog):
    def __init__(
        self,
        store: ObjectRecordStore,
        object_key: str,
        object_label: str,
        company: str,
        parent=None,
    ):
        super().__init__(parent)
        self.store = store
        self.object_key = object_key
        self.object_label = object_label
        self.company = company
        self.setWindowTitle("Добавить письмо из ящика info")
        self.resize(820, 650)
        self._settings = None
        self._password = ""
        self._messages: list[info_mail.MailMessage] = []
        self._content: info_mail.MailContent | None = None
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.direction = QComboBox()
        self.direction.addItem("Входящие", "incoming")
        self.direction.addItem("Отправленные", "sent")
        self.days = QComboBox()
        self.days.addItem("30 дней", 30)
        self.days.addItem("90 дней", 90)
        self.days.addItem("365 дней", 365)
        self.days.setCurrentIndex(1)
        self.refresh_button = QPushButton("Показать письма")
        self.refresh_button.clicked.connect(self._load_messages)
        settings_button = QPushButton("Настройки info…")
        settings_button.clicked.connect(self._configure)
        top.addWidget(self.direction)
        top.addWidget(self.days)
        top.addWidget(self.refresh_button)
        top.addStretch(1)
        top.addWidget(settings_button)
        layout.addLayout(top)
        self.mailbox_hint = QLabel("Показываются только заголовки писем; тело загружается после выбора.")
        layout.addWidget(self.mailbox_hint)

        self.message_table = QTableWidget(0, 3)
        self.message_table.setHorizontalHeaderLabels(("Дата", "Отправитель", "Тема"))
        self.message_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.message_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.message_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        message_header = self.message_table.horizontalHeader()
        message_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        message_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        message_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.message_table.itemSelectionChanged.connect(self._load_selected_message)
        layout.addWidget(self.message_table, 1)
        self.message_body = QTextEdit()
        self.message_body.setReadOnly(True)
        self.message_body.setPlaceholderText("Выберите письмо, чтобы просмотреть текст и вложения.")
        layout.addWidget(self.message_body, 2)
        self.attachment_list = QListWidget()
        self.attachment_list.setMaximumHeight(130)
        layout.addWidget(self.attachment_list)
        self.confirm_binding = QCheckBox(f"Подтверждаю привязку письма к объекту «{object_label}»")
        self.confirm_binding.setEnabled(False)
        self.confirm_binding.toggled.connect(self._update_save_enabled)
        layout.addWidget(self.confirm_binding)
        buttons = QDialogButtonBox()
        self.save_button = buttons.addButton(
            "Сохранить письмо и выбранные вложения", QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save_selected)
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText("Отмена")
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self._load_settings()

    def _load_settings(self):
        try:
            saved = info_mail.load_settings()
        except info_mail.InfoMailError as error:
            _show_error(self, "Настройки info", error)
            saved = None
        if saved:
            self._settings, self._password = saved
            self.mailbox_hint.setText(
                f"Ящик: {self._settings.email_address}. Заголовки выбранной папки; "
                "сохраняется только выбранное письмо."
            )
        else:
            self.mailbox_hint.setText("Сначала настройте отдельное подключение ящика info.")

    def _configure(self):
        dialog = InfoMailConnectionDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._load_settings()
            self.message_table.setRowCount(0)
            self._messages.clear()
            self._clear_preview()

    def _load_messages(self):
        if not self._settings or not self._password:
            self._configure()
            if not self._settings or not self._password:
                return
        try:
            folder = self._settings.folder(self.direction.currentData())
            self._messages = info_mail.list_messages(
                self._settings, self._password, folder, days_back=self.days.currentData(),
            )
        except (info_mail.InfoMailError, OSError, TimeoutError) as error:
            _show_error(self, "Не удалось получить список писем", error)
            return
        self.message_table.setRowCount(len(self._messages))
        for row, message in enumerate(self._messages):
            values = (
                _format_stockholm_datetime(message.sent_at),
                message.sender or "—",
                message.subject or "(без темы)",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, row)
                self.message_table.setItem(row, column, item)
        self._clear_preview()
        if self._messages:
            self.message_table.selectRow(0)
        else:
            self.mailbox_hint.setText("Писем за выбранный период не найдено.")

    def _clear_preview(self):
        self._content = None
        self.message_body.clear()
        self.attachment_list.clear()
        self.confirm_binding.setChecked(False)
        self.confirm_binding.setEnabled(False)
        self.save_button.setEnabled(False)

    def _load_selected_message(self):
        row = self.message_table.currentRow()
        if not 0 <= row < len(self._messages) or not self._settings:
            self._clear_preview()
            return
        try:
            content = info_mail.fetch_message(
                self._settings, self._password, self._messages[row],
            )
        except (info_mail.InfoMailError, OSError, TimeoutError) as error:
            self._clear_preview()
            _show_error(self, "Не удалось открыть письмо", error)
            return
        self._content = content
        self.message_body.setPlainText(
            f"От: {content.sender}\n"
            f"Тема: {content.subject or '(без темы)'}\n"
            f"Дата: {_format_stockholm_datetime(content.sent_at)} (Стокгольм)\n\n{content.body}"
        )
        self.attachment_list.clear()
        for attachment in content.attachments:
            item = QListWidgetItem(f"{attachment.filename} · {len(attachment.content):,} байт")
            item.setData(Qt.ItemDataRole.UserRole, attachment.part)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.attachment_list.addItem(item)
        self.confirm_binding.setEnabled(True)
        self._update_save_enabled()

    def _update_save_enabled(self, *_):
        self.save_button.setEnabled(self._content is not None and self.confirm_binding.isChecked())

    def _save_selected(self):
        if self._content is None or not self.confirm_binding.isChecked():
            return
        selected = {
            self.attachment_list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.attachment_list.count())
            if self.attachment_list.item(index).checkState() == Qt.CheckState.Checked
        }
        attachments = [
            {"part": attachment.part, "filename": attachment.filename, "content": attachment.content}
            for attachment in self._content.attachments if attachment.part in selected
        ]
        try:
            _, created, added = self.store.add_email(
                self.object_key, self.object_label, self.company,
                email_key=self._content.key, sender=self._content.sender,
                subject=self._content.subject, email_date=self._content.sent_at,
                body=self._content.body, attachments=attachments,
            )
        except (ObjectRecordError, OSError) as error:
            _show_error(self, "Письмо не сохранено", error)
            return
        if not created and not added:
            QMessageBox.information(
                self, "Письмо уже добавлено",
                "Это письмо уже сохранено для выбранного объекта. Дубликат не создан.",
            )
        else:
            QMessageBox.information(
                self, "Письмо сохранено",
                f"Письмо сохранено для объекта «{self.object_label}»; вложений добавлено: {added}.",
            )
        self.accept()

    def closeEvent(self, event):
        self._password = ""
        super().closeEvent(event)


class ObjectDocumentsPage(QWidget):
    def __init__(self, store: ObjectRecordStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.object_key: str | None = None
        self.object_label = ""
        self.company = ""
        layout = QVBoxLayout(self)
        actions = QHBoxLayout()
        self.add_file_button = QPushButton("Добавить файл…")
        self.add_file_button.clicked.connect(self._add_files)
        self.add_mail_button = QPushButton("Добавить из почты")
        self.add_mail_button.clicked.connect(self._add_email)
        self.open_button = QPushButton("Открыть")
        self.open_button.clicked.connect(self._open_selected)
        self.offer_status_button = QPushButton("Статус оферты…")
        self.offer_status_button.clicked.connect(self._change_offer_status)
        actions.addWidget(self.add_file_button)
        actions.addWidget(self.add_mail_button)
        actions.addWidget(self.open_button)
        actions.addWidget(self.offer_status_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("Категория", "Документ / письмо", "Дата", "Версия / статус"))
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.table.cellDoubleClicked.connect(lambda *_: self._open_selected())
        layout.addWidget(self.table, 1)
        self._documents: list[dict] = []
        self.set_object(None, "", "")

    def set_object(self, object_key: str | None, label: str, company: str):
        self.object_key, self.object_label, self.company = object_key, label, company
        self.refresh()

    def refresh(self):
        self.table.setRowCount(0)
        self._documents = self.store.list_documents(self.object_key) if self.object_key else []
        self.table.setColumnHidden(
            3, not any(document["category"] == "Оферта" for document in self._documents)
        )
        self.table.setRowCount(len(self._documents))
        for row, document in enumerate(self._documents):
            name = document["title"]
            status = ""
            if document["category"] == "Оферта":
                status = f"v{document['offer_version']} · {document['offer_status']}"
            values = (
                document["category"], name, document["created_at"][:10], status,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, document["id"])
                elif column == 1:
                    item.setToolTip(value)
                self.table.setItem(row, column, item)
        self._update_actions()

    def _update_actions(self):
        selected = self.object_key is not None and self.table.currentRow() >= 0
        self.add_file_button.setEnabled(self.object_key is not None)
        self.add_mail_button.setEnabled(self.object_key is not None)
        self.open_button.setEnabled(selected)
        row = self.table.currentRow()
        if 0 <= row < len(self._documents):
            self.open_button.setText(
                "Читать письмо" if self._documents[row]["email_part"] == -1 else "Открыть файл"
            )
        else:
            self.open_button.setText("Открыть")
        self.offer_status_button.setEnabled(
            0 <= row < len(self._documents) and self._documents[row]["category"] == "Оферта"
        )

    def _change_offer_status(self):
        row = self.table.currentRow()
        if not 0 <= row < len(self._documents):
            return
        document = self._documents[row]
        status, accepted = QInputDialog.getItem(
            self, "Статус оферты", "Статус:",
            list(OFFER_STATUSES), OFFER_STATUSES.index(document["offer_status"]), False,
        )
        if not accepted:
            return
        try:
            self.store.update_offer_status(document["id"], status)
        except ObjectRecordError as error:
            _show_error(self, "Статус не изменён", error)
            return
        self.refresh()

    def _add_files(self):
        if not self.object_key:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Добавить документы объекта", "", FILE_FILTER)
        if not paths:
            return
        dialog = AddDocumentDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        category = dialog.category.currentText()
        for path in paths:
            try:
                self.store.add_file(
                    self.object_key, self.object_label, self.company, Path(path), category,
                    offer_version=dialog.version.value() if category == "Оферта" else None,
                    offer_status=dialog.status.currentText() if category == "Оферта" else None,
                )
            except (ObjectRecordError, OSError) as error:
                _show_error(self, "Файл не добавлен", error)
                continue
        self.refresh()

    def _add_email(self):
        if not self.object_key:
            return
        dialog = InfoMailPickerDialog(
            self.store, self.object_key, self.object_label, self.company, self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def _open_selected(self):
        row = self.table.currentRow()
        if not 0 <= row < len(self._documents):
            return
        document = self.store.document(self._documents[row]["id"])
        if not document:
            QMessageBox.warning(self, "Документ", "Запись не найдена. Обновите список.")
            return
        try:
            if document["file_id"]:
                path = self.store.file_path(document["relative_path"], document["sha256"])
                _open_file(self, path)
            elif document["email_body"] is not None:
                dialog = QDialog(self)
                dialog.setWindowTitle(document["email_subject"] or "Текст письма")
                dialog.resize(700, 500)
                layout = QVBoxLayout(dialog)
                header = QLabel(
                    f"От: {document['email_sender'] or '—'}\n"
                    f"Тема: {document['email_subject'] or '(без темы)'}\n"
                    f"Дата: {_format_stockholm_datetime(document['email_date'])} (Стокгольм)"
                )
                layout.addWidget(header)
                body = QTextEdit()
                body.setReadOnly(True)
                body.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
                body.setPlainText(document["email_body"])
                layout.addWidget(body, 1)
                buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
                buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
                buttons.rejected.connect(dialog.reject)
                buttons.accepted.connect(dialog.accept)
                layout.addWidget(buttons)
                dialog.exec()
            else:
                QMessageBox.information(self, "Документ", "В этой записи нет открываемого вложения.")
        except (ObjectRecordError, OSError) as error:
            _show_error(self, "Не удалось открыть документ", error)


class ObjectAgreementsPage(QWidget):
    def __init__(self, store: ObjectRecordStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.object_key: str | None = None
        self.object_label = ""
        self.company = ""
        layout = QVBoxLayout(self)
        actions = QHBoxLayout()
        self.add_button = QPushButton("Добавить договорённость…")
        self.add_button.clicked.connect(self._add)
        self.open_record_button = QPushButton("Открыть запись")
        self.open_record_button.clicked.connect(self._open_record)
        self.open_button = QPushButton("Открыть вложение")
        self.open_button.clicked.connect(self._open_attachment)
        actions.addWidget(self.add_button)
        actions.addWidget(self.open_record_button)
        actions.addWidget(self.open_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("Дата", "Текст", "Согласована", "Вложение"))
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.table.cellDoubleClicked.connect(lambda *_: self._open_record())
        layout.addWidget(self.table, 1)
        self._agreements: list[dict] = []
        self.set_object(None, "", "")

    def set_object(self, object_key: str | None, label: str, company: str):
        self.object_key, self.object_label, self.company = object_key, label, company
        self.refresh()

    def refresh(self):
        self.table.setRowCount(0)
        self._agreements = self.store.list_agreements(self.object_key) if self.object_key else []
        self.table.setRowCount(len(self._agreements))
        for row, agreement in enumerate(self._agreements):
            values = (
                agreement["agreement_date"], agreement["text"],
                "Да" if agreement["agreed"] else "Нет",
                agreement["original_name"] or "",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, agreement["id"])
                self.table.setItem(row, column, item)
        self._update_actions()

    def _update_actions(self):
        self.add_button.setEnabled(self.object_key is not None)
        row = self.table.currentRow()
        self.open_record_button.setEnabled(0 <= row < len(self._agreements))
        self.open_button.setEnabled(
            0 <= row < len(self._agreements) and bool(self._agreements[row]["file_id"])
        )

    def _add(self):
        if not self.object_key:
            return
        dialog = AddAgreementDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        qdate = dialog.agreement_date.date()
        attachment = dialog.attachment_path.text().strip()
        try:
            self.store.add_agreement(
                self.object_key, self.object_label, self.company, qdate.toString("yyyy-MM-dd"),
                dialog.text.toPlainText(), dialog.agreed.isChecked(),
                Path(attachment) if attachment else None,
            )
        except (ObjectRecordError, OSError) as error:
            _show_error(self, "Договорённость не сохранена", error)
            return
        self.refresh()

    def _open_attachment(self):
        row = self.table.currentRow()
        if not 0 <= row < len(self._agreements):
            return
        agreement = self.store.agreement(self._agreements[row]["id"])
        if not agreement or not agreement["relative_path"]:
            QMessageBox.information(self, "Вложение", "У договорённости нет вложения.")
            return
        try:
            _open_file(
                self,
                self.store.file_path(agreement["relative_path"], agreement["sha256"]),
            )
        except (ObjectRecordError, OSError) as error:
            _show_error(self, "Не удалось открыть вложение", error)

    def _open_record(self):
        row = self.table.currentRow()
        if not 0 <= row < len(self._agreements):
            return
        agreement = self.store.agreement(self._agreements[row]["id"])
        if not agreement:
            QMessageBox.warning(self, "Договорённость", "Запись не найдена. Обновите список.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Договорённость от {agreement['agreement_date']}")
        dialog.resize(720, 520)
        layout = QVBoxLayout(dialog)
        meta = QLabel(
            f"Дата: {agreement['agreement_date']}\n"
            f"Согласована: {'Да' if agreement['agreed'] else 'Нет'}\n"
            f"Вложение: {agreement['original_name'] or '—'}"
        )
        meta.setWordWrap(True)
        layout.addWidget(meta)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        text.setPlainText(agreement["text"])
        layout.addWidget(text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
