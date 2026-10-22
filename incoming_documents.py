"""Reviewed object costs delivered through the ordinary versioned expense outbox."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from expense_documents import decimal_text, parse_invoice_money
from offline_review import EDITABLE, OfflineReviewStore, ReviewError, current_classification, digest, packed


def enabled():
    return os.environ.get("SMARTREPORT_INCOMING_DOCUMENTS", "1") == "1"


class IncomingStore(OfflineReviewStore):
    def __init__(self, root: Path, host):
        self.host = host
        super().__init__(root)
        with self.conn:
            self.conn.execute("""CREATE TABLE IF NOT EXISTS deliveries(
                document_id INTEGER PRIMARY KEY REFERENCES documents,
                state TEXT NOT NULL CHECK(state IN ('queued')),
                client_id TEXT NOT NULL UNIQUE)""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS mail_epoch(
                folder TEXT PRIMARY KEY, epoch TEXT NOT NULL)""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS mail_messages(
                folder TEXT NOT NULL, epoch TEXT NOT NULL, uid INTEGER NOT NULL,
                sha256 TEXT NOT NULL, PRIMARY KEY(folder,epoch,uid))""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS mail_excluded_pdfs(
                sha256 TEXT PRIMARY KEY, reason TEXT NOT NULL)""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS document_actions(
                document_id INTEGER NOT NULL REFERENCES documents,
                revision INTEGER NOT NULL, action TEXT NOT NULL
                CHECK(action IN ('ignore','later','expense')),
                created_at TEXT NOT NULL, PRIMARY KEY(document_id,revision))""")

    def objects(self):
        provider = getattr(self.host, "incoming_allocation_objects", None)
        if provider:
            return provider()
        return list(self.host._expense_objects)

    def allocation_proposal(self, document_id):
        from document_costs import propose_object_allocations
        return propose_object_allocations(self.base(document_id), self.objects())

    @staticmethod
    def allocations(payload):
        if "allocations" in payload:
            return payload["allocations"]
        return ([{"object_id": payload["object_id"], "gross_amount": payload.get("gross_amount")}]
                if payload.get("object_id") is not None else [])

    def clean_payload(self, payload):
        cleaned = super().clean_payload({key: value for key, value in payload.items()
                                         if key not in ("allocations", "server_version")})
        parts = self.allocations(payload)
        if not isinstance(parts, list) or len(parts) > 100:
            raise ReviewError("Распределение: не более 100 строк.")
        cleaned["allocations"] = []
        for part in parts:
            if not isinstance(part, dict):
                raise ReviewError("Некорректная строка распределения.")
            raw = part.get("gross_amount")
            amount = parse_invoice_money(raw)
            if raw not in (None, "") and (amount is None or not amount.is_finite()):
                raise ReviewError("Некорректная сумма распределения.")
            cleaned["allocations"].append({"object_id": part.get("object_id"),
                                           "gross_amount": decimal_text(amount)})
        cleaned["object_id"] = (parts[0].get("object_id") if len(parts) == 1 else None)
        if payload.get("server_version") is not None:
            cleaned["server_version"] = payload["server_version"]
        return cleaned

    def current_review(self, document_id):
        revision, payload = self.latest(document_id)
        card = self.card(document_id)
        if card:
            payload = dict(payload)
            payload.pop("server_version", None)
        delivered = card and self.conn.execute("SELECT 1 FROM deliveries WHERE document_id=?",
                                               (document_id,)).fetchone()
        if delivered and not self.host.incoming_delivery_pending(card["client_id"]):
            try:
                existing = self._existing(card["client_id"])
            except ValueError:
                return revision, payload
            if existing:
                payload = dict(payload)
                for key in EDITABLE:
                    if key in existing:
                        payload[key] = existing[key]
                payload["allocations"] = [{"object_id": row.get("object_id"),
                                           "gross_amount": row.get("gross_amount")}
                                          for row in existing.get("allocations", [])]
                payload["object_id"] = (payload["allocations"][0]["object_id"]
                                        if len(payload["allocations"]) == 1 else None)
                payload["server_version"] = existing["sync_version"]
        return revision, payload

    def save(self, document_id, payload, expected_revision):
        cleaned = self.clean_payload(payload)
        card = self.card(document_id)
        if card is None:
            return self.save_revision(document_id, cleaned, expected_revision)
        if self.host.incoming_delivery_pending(card["client_id"]):
            raise ReviewError("Сначала завершите передачу или разберите конфликт в обычном реестре.")
        existing = self._existing(card["client_id"], required=True)
        if (not existing or cleaned.get("server_version") is None
                or cleaned["server_version"] != existing["sync_version"]):
            raise ReviewError("Распределение изменилось. Откройте актуальное распределение; правки не отправлены.")
        problems = self.issues(document_id, cleaned)
        if problems:
            raise ReviewError("\n".join(problems))
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            revision, previous = self.latest(document_id)
            if revision != expected_revision:
                raise ReviewError("Карточка изменена в другом окне. Обновите распределение.")
            self.conn.execute("INSERT INTO revisions VALUES (?,?,?,?,?)", (
                document_id, revision + 1, packed(cleaned),
                packed({key: {"before": previous.get(key), "after": value}
                        for key, value in cleaned.items() if previous.get(key) != value}),
                datetime.now(timezone.utc).isoformat()))
            self.conn.execute("UPDATE register SET payload_json=?,revision=? WHERE document_id=?",
                              (packed(cleaned), revision + 1, document_id))
            self.conn.execute("DELETE FROM deliveries WHERE document_id=?", (document_id,))
        self.deliver(self.card(document_id))
        return revision + 1

    def action(self, document_id):
        row = self.conn.execute("SELECT revision,action FROM document_actions WHERE document_id=? "
                                "ORDER BY revision DESC LIMIT 1", (document_id,)).fetchone()
        return (row["revision"], row["action"]) if row else (0, None)

    def set_action(self, document_id, action, expected_revision):
        if action not in ("ignore", "later", "expense"):
            raise ReviewError("Неизвестное действие документа.")
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self.base(document_id)
            revision, previous = self.action(document_id)
            if revision != expected_revision:
                raise ReviewError("Статус изменён в другом окне. Откройте документ заново.")
            if self.card(document_id):
                raise ReviewError("Расход уже создан: статус не отменяет расход. Измените существующую запись.")
            if previous == action:
                return revision
            self.conn.execute("INSERT INTO document_actions VALUES (?,?,?,?)", (
                document_id, revision + 1, action, datetime.now(timezone.utc).isoformat()))
        return revision + 1

    def cost_fields(self, document_id):
        from document_costs import extract_cost_fields
        base = self.base(document_id)
        if base.get("object_cost_fields"):
            return base["object_cost_fields"]
        return extract_cost_fields([{"page": 1, "text": base.get("raw_text") or ""}], base)

    def transfer(self, document_id, expected_revision):
        if self.action(document_id)[1] in ("ignore", "later"):
            raise ReviewError("Сначала выберите «Учесть как расход».")
        revision, reviewed = self.latest(document_id)
        if revision != expected_revision or not revision:
            raise ReviewError("Сохраните актуальные исправления перед добавлением.")
        problems = self.issues(document_id, reviewed)
        if problems:
            raise ReviewError("\n".join(problems))
        if not getattr(self.host, "_incoming_costs_supported", False):
            raise ReviewError("Сервер ещё не поддерживает расходы объекта без платёжных условий. "
                              "Обновите соединение с сервером; черновик и PDF сохранены.")
        self._check_existing_register(document_id, reviewed)
        card = self.card(document_id)
        if card is None:
            import uuid
            with self.conn:
                self.conn.execute("BEGIN IMMEDIATE")
                if self.latest(document_id)[0] != revision:
                    raise ReviewError("Черновик изменён в другом окне. Откройте его заново.")
                if self.action(document_id)[1] in ("ignore", "later"):
                    raise ReviewError("Статус изменён: сначала выберите «Учесть как расход».")
                if self.card(document_id) is None:
                    self._check_existing_register(document_id, reviewed)
                    self.conn.execute("""INSERT INTO register
                        (document_id,client_id,revision,business_key,payload_json,sha256,debt_amount,created_at)
                        VALUES (?,?,?,?,?,?,?,?)""", (
                            document_id, uuid.uuid4().hex, revision, str(document_id), packed(reviewed),
                            digest(self.original(document_id).read_bytes()), "0.00",
                            datetime.now(timezone.utc).isoformat()))
            card = self.card(document_id)
        return self.deliver(card)

    def latest(self, document_id):
        revision, payload = super().latest(document_id)
        if revision == 0:
            base = self.base(document_id)
            costs = self.cost_fields(document_id)
            for key in EDITABLE:
                if key in costs:
                    payload[key] = costs[key]
            amounts = [parse_invoice_money(base.get(key)) for key in ("net_amount", "vat_amount")]
            rounding = parse_invoice_money(base.get("rounding_amount")) or Decimal(0)
            if all(value is not None for value in amounts) and base.get("document_type") != "tax_decision":
                payload["gross_amount"] = decimal_text(sum(amounts) + rounding)
        return revision, payload

    def provenance(self, document_id, payload=None):
        result = super().provenance(document_id, payload)
        base = self.base(document_id)
        if base.get("document_type") == "tax_decision":
            costs = self.cost_fields(document_id)
            current = payload or self.latest(document_id)[1]
            for key, value in costs.items():
                if key in result and value not in (None, "", "UNKNOWN") and current.get(key) == value:
                    sources = costs.get("field_sources", {}).get(key) or []
                    result[key] = "Считано из решения — требуется сверка"
                    if sources:
                        result[key] += ": " + "; ".join(source.get("text", "") for source in sources)
            if costs.get("gross_amount") is None:
                candidate = next((candidate for candidate in costs.get("amount_candidates", [])
                                  if candidate["amount"] == current.get("gross_amount")), None)
                if candidate:
                    result["gross_amount"] = "Выбрано из считанных сумм — сверьте оригинал: " + "; ".join(
                        item["text"] for item in candidate["evidence"])
            return result
        current = payload or self.latest(document_id)[1]
        amounts = [parse_invoice_money(base.get(key)) for key in ("net_amount", "vat_amount")]
        if all(value is not None for value in amounts):
            total = decimal_text(sum(amounts) + (parse_invoice_money(base.get("rounding_amount")) or Decimal(0)))
            if current.get("gross_amount") == total and total != base.get("gross_amount"):
                result["gross_amount"] = "Вычислено: нетто + НДС + указанное округление; сверьте итог оригинала"
        return result

    def issues(self, document_id, payload):
        base = current_classification(self.base(document_id))
        problems = []
        if base.get("suggested_document_type") == "payment_reminder":
            problems.append("Напоминание не является новым расходом: сверьте исходный документ.")
        if payload.get("original_checked") is not True:
            problems.append("Подтвердите сверку оригинала.")
        parts = self.allocations(payload)
        ids = {row["id"] for row in self.objects()}
        if not parts or any(part.get("object_id") not in ids for part in parts):
            problems.append("Обязательно выберите объект для каждой части.")
        amounts = [parse_invoice_money(part.get("gross_amount")) for part in parts]
        if any(amount is None or not amount.is_finite() or amount <= 0 for amount in amounts):
            problems.append("Укажите положительную сумму каждой части.")
        elif sum(amounts, Decimal(0)) != parse_invoice_money(payload.get("gross_amount")):
            problems.append("Сумма частей должна точно совпадать с общей суммой документа.")
        if len({part.get("object_id") for part in parts}) != len(parts):
            problems.append("Укажите каждый объект одной строкой.")
        for key in ("supplier_name", "document_date", "gross_amount", "currency_code"):
            if payload.get(key) in (None, "", "UNKNOWN"):
                problems.append(f"Не заполнено обязательное поле: {key}.")
        try:
            date.fromisoformat(payload.get("document_date") or "")
        except (ValueError, TypeError):
            problems.append("Укажите корректную дату ГГГГ-ММ-ДД.")
        gross = parse_invoice_money(payload.get("gross_amount"))
        vat = parse_invoice_money(payload.get("vat_amount"))
        if gross is None or not gross.is_finite() or gross <= 0:
            problems.append("Укажите положительную сумму расхода.")
        if payload.get("vat_amount") not in (None, "") and (
                vat is None or not vat.is_finite() or vat < 0 or gross is not None and vat > gross):
            problems.append("Проверьте НДС; неизвестный НДС оставьте пустым.")
        return problems

    def add_pdf(self, content, filename, result):
        sha = digest(content)
        if self.conn.execute("SELECT 1 FROM documents WHERE sha256=?", (sha,)).fetchone():
            return False
        fields = current_classification(result.get("fields") or {})
        fields["_recognition_state"] = result.get("state")
        source = {"original": sha + ".pdf", "result": result,
                  "sources": [{"filename": filename}]}
        initial = {**{key: fields.get(key) for key in EDITABLE},
                   "confirmed_type": None, "object_id": None, "original_checked": False}
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO originals VALUES (?,?,?)", (sha, content, sha + ".pdf"))
            cursor = self.conn.execute("INSERT INTO documents(sha256,source_json,base_json) VALUES (?,?,?)",
                                       (sha, packed(source), packed(fields)))
            self.conn.execute("INSERT INTO revisions VALUES (?,?,?,?,?)",
                              (cursor.lastrowid, 0, packed(initial), "{}",
                               datetime.now(timezone.utc).isoformat()))
        return True

    def record_recognition(self, document_id, result):
        fields = current_classification(result.get("fields") or {})
        fields["_recognition_state"] = result.get("state")
        revision, _ = self.latest(document_id)
        if revision or self.card(document_id):
            raise ReviewError("Распознавание не заменяет сохранённые ручные исправления.")
        initial = {**{key: fields.get(key) for key in EDITABLE},
                   "confirmed_type": None, "object_id": None, "original_checked": False}
        with self.conn:
            self.conn.execute("UPDATE documents SET base_json=? WHERE id=?", (packed(fields), document_id))
            self.conn.execute("UPDATE revisions SET payload_json=? WHERE document_id=? AND revision=0",
                              (packed(initial), document_id))

    def recognize_document(self, document_id, recognizer=None):
        from invoice_processing import recognize_invoice
        try:
            result = (recognizer or recognize_invoice)([self.original(document_id)])
        except (OSError, ValueError, RuntimeError):
            result = {"fields": {}, "state": "failed",
                      "errors": ["recognition_failed_original_retained_manual_fields_available"]}
        self.record_recognition(document_id, result)
        return result

    def reassign(self, document_id, object_id, expected_revision):
        card = self.card(document_id)
        revision, payload = self.latest(document_id)
        if card is None or revision != expected_revision:
            raise ReviewError("Обновите документ перед сменой объекта.")
        existing = self._existing(card["client_id"], required=True)
        if (len(self.allocations(payload)) != 1 or existing and
                (len(existing.get("allocations", [])) != 1 or
                 existing["allocations"][0].get("object_id") != payload.get("object_id"))):
            raise ReviewError("Фактура распределена или изменена на сервере. Откройте актуальное распределение.")
        if existing and payload.get("server_version") != existing.get("sync_version"):
            raise ReviewError("Откройте актуальное распределение перед изменением объекта.")
        if object_id not in {row["id"] for row in self.objects()}:
            raise ReviewError("Выберите действующий объект.")
        if payload["object_id"] == object_id:
            return card
        self._existing(card["client_id"], required=True)
        previous = payload["object_id"]
        payload["object_id"] = object_id
        payload["allocations"] = [{"object_id": object_id, "gross_amount": payload["gross_amount"]}]
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if self.latest(document_id)[0] != revision:
                raise ReviewError("Привязка изменена в другом окне. Обновите документ.")
            self.conn.execute("INSERT INTO revisions VALUES (?,?,?,?,?)", (
                document_id, revision + 1, packed(payload),
                packed({"object_id": {"before": previous, "after": object_id}}),
                datetime.now(timezone.utc).isoformat()))
            self.conn.execute("UPDATE register SET payload_json=?,revision=? WHERE document_id=?",
                              (packed(payload), revision + 1, document_id))
            self.conn.execute("DELETE FROM deliveries WHERE document_id=?", (document_id,))
        return self.deliver(self.card(document_id))

    def _existing(self, client_id, required=False):
        matches = [row for row in self.host.incoming_register_snapshot() if row.get("client_id") == client_id]
        if required and not matches:
            raise ReviewError("Сначала синхронизируйте документ и обновите реестр; новая карточка не создаётся.")
        return next((row for row in matches if row.get("id") and not row.get("local_pending")), None)

    def accounting_status(self, document_id):
        card = self.card(document_id)
        if card is None:
            action = self.action(document_id)[1]
            if (action not in ("ignore", "later") and self.base(
                    document_id).get("suggested_document_type") == "payment_reminder"):
                return "Напоминание / требование оплаты — не новая покупка; перенос запрещён", False
            return {"ignore": "Не учитывать — PDF сохранён, расход не создан",
                    "later": "Разобрать позже — PDF сохранён, расход не создан",
                    "expense": "Проверьте данные и выберите объект — расход ещё не создан"}.get(
                        action, "Выберите действие — расход не создан"), False
        pending = self.host.incoming_delivery_pending(card["client_id"])
        if pending:
            return ("Сохранено локально — ожидает синхронизации"
                    if not pending["last_error"] else "Не учтён на сервере: " + pending["last_error"]), False
        if not self.conn.execute("SELECT 1 FROM deliveries WHERE document_id=?", (document_id,)).fetchone():
            return "Передача прервана — нажмите повтор передачи", False
        try:
            existing = self._existing(card["client_id"])
        except ValueError:
            return "Обновите реестр для проверки сохранения", False
        if existing and existing.get("verification_status") == "confirmed":
            return "Учтён на объекте", True
        return "Ожидает проверки в реестре", False

    @staticmethod
    def due_fields(base, reviewed):
        unchanged = reviewed.get("due_date") == base.get("due_date")
        return {
            "due_date_kind": (base.get("due_date_kind") or
                              ("explicit" if reviewed.get("due_date") else "absent")) if unchanged
                             else ("manual" if reviewed.get("due_date") else "absent"),
            "due_date_review_required": unchanged and base.get("due_date_review_required") is True,
            "due_date_reviewed": unchanged and base.get("due_date_reviewed") is True,
            "due_date_evidence": (base.get("due_date_evidence") or []) if unchanged else [],
        }

    def _check_existing_register(self, document_id, payload):
        # Host supplies the cached ordinary register, never triggers a read/network request.
        own = self.card(document_id)
        try:
            snapshot = self.host.incoming_register_snapshot()
        except ValueError as error:
            raise ReviewError(str(error)) from error
        for existing in snapshot:
            if own and existing.get("client_id") == own["client_id"]:
                continue
            if (str(existing.get("supplier_name") or "").strip().casefold() ==
                    str(payload.get("supplier_name") or "").strip().casefold()
                    and (
                        payload.get("invoice_number") and str(existing.get("invoice_number") or "").strip().casefold() ==
                        str(payload["invoice_number"]).strip().casefold()
                        or existing.get("document_date") == payload.get("document_date")
                        and existing.get("gross_amount") == payload.get("gross_amount"))):
                raise ReviewError("В обычном реестре или очереди уже есть такая фактура. "
                                  "Нужна ручная сверка, второй расход не создан.")
            if any(row.get("sha256") == self.conn.execute(
                    "SELECT sha256 FROM documents WHERE id=?", (document_id,)).fetchone()[0]
                   for row in existing.get("attachments", [])):
                raise ReviewError("Этот PDF уже есть в реестре. Второй расход не создан.")
        for row in self.conn.execute("SELECT document_id,payload_json FROM register WHERE document_id<>?",
                                     (document_id,)):
            other = json.loads(row["payload_json"])
            if (other.get("supplier_name", "").strip().casefold() == payload["supplier_name"].strip().casefold()
                    and (payload.get("invoice_number") and other.get("invoice_number") == payload["invoice_number"]
                         or other.get("document_date") == payload["document_date"]
                         and other.get("gross_amount") == payload["gross_amount"])):
                raise ReviewError("Возможный повтор покупки с другим PDF. Нужна ручная сверка.")

    def deliver(self, card):
        delivered = self.conn.execute(
            "SELECT * FROM deliveries WHERE document_id=?", (card["document_id"],)).fetchone()
        if delivered:
            return card
        payload = json.loads(card["payload_json"])
        base = self.base(card["document_id"])
        payload.update(
            client_id=card["client_id"], document_type="manual", allocation_basis="gross", usage="object_cost",
            cost_operation_id=f"{card['client_id']}:{card['revision']}",
            verification_status="confirmed",
            recognized_text=base.get("raw_text") or "",
            required_attachment_sha256=[card["sha256"]],
            allocations=[{"destination": "object", **part} for part in self.allocations(payload)],
        )
        for key in ("amount_due", "settlement_status", "due_date"):
            payload.pop(key, None)
        # Retry the same client_id through the normal outbox. The queue is durable before
        # recording delivery; a lost response cannot change the document's identity.
        original = self.original(card["document_id"])
        staged = self.host._store_expense_originals([str(original)], card["client_id"])
        existing = self._existing(card["client_id"])
        self.host.queue_expense_document_operation(
            card["client_id"], "upsert", payload,
            server_document_id=existing.get("id") if existing else None,
            server_version=payload.get("server_version") if payload.get("server_version") is not None
                           else existing.get("sync_version") if existing else None,
            attachment_paths=staged)
        with self.conn:
            self.conn.execute("INSERT INTO deliveries VALUES (?,'queued',?)",
                              (card["document_id"], card["client_id"]))
        callback = getattr(self.host, "incoming_after_queue", None)
        if callback:
            callback()
        return card


def open_incoming(host, root):
    if not enabled():
        raise ReviewError("Функция «Входящие документы» выключена.")
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtWidgets import QFileDialog, QMessageBox, QPushButton
    from offline_review_ui import OfflineReviewWindow

    store = IncomingStore(root, host)
    window = OfflineReviewWindow(store, integrated=True)
    window.setParent(host, Qt.WindowType.Window)
    window.setWindowTitle("SmartReport — Входящие документы")
    retry = QPushButton("Повторить передачу подтверждённого документа")

    def repeat():
        card = store.card(getattr(window, "document_id", None))
        if card is None:
            QMessageBox.warning(window, "Передача запрещена", "Сначала явно подтвердите документ.")
            return
        try:
            store.transfer(window.document_id, card["revision"])
        except (ReviewError, OSError, sqlite3.Error) as error:
            QMessageBox.warning(window, "Передача не завершена", str(error))
            return
        window.status.setText("Документ находится в обычной очереди синхронизации. Повтор не создаёт карточку.")

    retry.clicked.connect(repeat)
    refresh = QPushButton("Проверить обычный реестр для поиска дублей")

    def refresh_register():
        try:
            host.refresh_incoming_capabilities()
        except (OSError, ValueError, RuntimeError) as error:
            QMessageBox.warning(window, "Поддержка сервера не проверена", str(error))
            return
        host._schedule_expense_read((None, None))
        window.select_document()
        window.status.setText("Запрошен полный снимок обычного реестра. До успешного получения перенос запрещён.")

    refresh.clicked.connect(refresh_register)
    window.diagnostics_layout.addWidget(refresh)
    load = QPushButton("Открыть сохранённый локальный снимок очереди — без почты")

    def import_snapshot():
        folder = QFileDialog.getExistingDirectory(window, "Каталог сохранённой очереди")
        if not folder:
            return
        try:
            store.import_snapshot(Path(folder))
        except (ReviewError, OSError, sqlite3.Error, ValueError) as error:
            QMessageBox.warning(window, "Снимок не открыт", str(error))
            return
        if not window.close():
            return
        host._incoming_viewer = None
        host.open_incoming_documents()

    load.clicked.connect(import_snapshot)
    window.diagnostics_layout.addWidget(load)
    window.diagnostics_layout.addWidget(retry)
    from folder_mail import open_import_dialog, recent_period, RECENT_DAYS, IMPORT_START_DATE
    def mail_caption():
        start, end = recent_period()
        if end < IMPORT_START_DATE:
            return "Получение фактур с 01.11.2026 · до этой даты почта не подключается"
        return f"Получить документы · INBOX · последние {RECENT_DAYS} дней ({start} — {end})"

    mail = QPushButton(mail_caption())
    mail.setObjectName("incoming_receive_mail")
    mail.setToolTip("По дате получения письма. Обработанные UID пропускаются без скачивания и OCR.")
    mail.clicked.connect(lambda: open_import_dialog(window))
    window.layout().insertWidget(1, mail)
    archive = QPushButton("Загрузить архив")
    archive.setObjectName("incoming_mail_archive")
    archive.clicked.connect(lambda: open_import_dialog(window, archive=True))
    window.layout().insertWidget(2, archive)
    files = QPushButton("Открыть PDF и распознать")

    def import_files():
        paths, _ = QFileDialog.getOpenFileNames(window, "Открыть документы", "", "PDF (*.pdf)")
        if not paths or not window.keep_unsaved():
            return
        try:
            for name in paths:
                path = Path(name)
                content = path.read_bytes()
                if store.add_pdf(content, path.name, {"fields": {}, "state": "pending"}):
                    doc_id = store.conn.execute("SELECT id FROM documents WHERE sha256=?",
                                                (digest(content),)).fetchone()[0]
                    store.recognize_document(doc_id)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            QMessageBox.warning(window, "Документ не загружен", str(error))
        window.reload_documents()

    files.clicked.connect(import_files)
    window.layout().insertWidget(3, files)
    try:
        host.refresh_incoming_capabilities()
    except (OSError, ValueError, RuntimeError):
        host._incoming_costs_supported = False
        window.status.setText("Сервер недоступен. Можно получать и править черновики; добавление заблокировано до проверки сервера.")
    host._schedule_expense_read((None, None))
    window.finished.connect(lambda _result: store.close())
    window.status_timer = QTimer(window)

    def update_status():
        mail.setText(mail_caption())
        if window.document_id is None:
            return
        text, green = store.accounting_status(window.document_id)
        if store.card(window.document_id):
            window.status.setText(text)
            window.status.setStyleSheet("background:#b9efc6;color:#14552b;padding:6px" if green else "")
        window.refresh_document_labels(server_changed=store.card(window.document_id) is not None)

    window.status_timer.timeout.connect(update_status)
    window.status_timer.start(2000)
    window.finished.connect(lambda _result: window.status_timer.stop())
    return window
