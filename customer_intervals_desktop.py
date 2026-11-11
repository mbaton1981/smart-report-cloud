"""Native object terms, explicit historical preview and factual-time correction."""
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from requests import RequestException
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QHeaderView,
)
import customer_intervals as intervals
from customer_intervals_ui import CustomerTermsDialog, TimeFactsEditor
from customer_pricing import ensure_pricing_schema, row_prices, store_pricing
from customer_report import prepare_customer_costs


def cache_terms(cursor, values):
    for item in values:
        normalized = intervals.encoded(intervals.terms(json.loads(item["conditions_json"])))
        previous = cursor.execute("SELECT * FROM customer_terms WHERE id=?", (item["id"],)).fetchone()
        collision = cursor.execute("SELECT id FROM customer_terms WHERE company=? AND object_name=? AND revision=?",
                                   (item["company"], item["object_name"], item["revision"])).fetchone()
        if collision is not None and collision["id"] != item["id"]:
            raise ValueError("Конфликт идентификаторов клиентской истории объекта.")
        if previous is not None and dict(previous) != dict(item, conditions_json=normalized):
            raise ValueError("Сервер изменил уже сохранённую версию клиентских условий.")
        cursor.execute("""INSERT OR IGNORE INTO customer_terms
            VALUES(:id,:company,:object_name,:revision,:conditions_json,:created_at)""",
                       dict(item, conditions_json=normalized))


class CustomerIntervalsMixin:
    def _terms_versions(self, obj, *, refresh=True):
        app = sys.modules[self.__class__.__module__]
        if refresh:
            data = app.request_expense_api("GET", "/api/customer-terms")
            self._customer_schedules_supported = data.get("customer_schedules") == 1
            with closing(app.get_db_connection()) as db, db:
                ensure_pricing_schema(db.cursor(), "shifts")
                cache_terms(db.cursor(), data["versions"])
        with closing(app.get_db_connection()) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM customer_terms WHERE company=? AND object_name=? ORDER BY revision",
                (obj["company"], obj["name"]))]

    def customer_terms_dialog(self):
        app = sys.modules[self.__class__.__module__]
        try:
            row = self.table_objects.currentRow()
            if row < 0:
                raise ValueError("Выберите строительный объект.")
            item = self.table_objects.item(row, 0)
            object_id = item.data(Qt.ItemDataRole.UserRole)
            with closing(app.get_db_connection()) as db:
                obj = db.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
                if obj is None or obj["service_type"]:
                    raise ValueError("Выберите строительный объект, не служебную категорию.")
                obj = dict(obj)
                objects = [dict(r) for r in db.execute("SELECT * FROM objects WHERE service_type IS NULL ORDER BY name")]
            if not obj.get("server_id"):
                raise ValueError("Сначала синхронизируйте новый объект с сервером.")
            versions = self._terms_versions(obj)
            dialog = CustomerTermsDialog(obj, versions, objects, self._terms_versions, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            conditions = dialog.value()
            if conditions.get("schedule") and not self._customer_schedules_supported:
                raise ValueError("Сервер ещё не поддерживает типовой график. Обновите сервер перед сохранением.")
            data = app.request_expense_api("POST", "/api/customer-terms", json={
                "object_id": obj["server_id"], "expected_revision": versions[-1]["revision"] if versions else 0,
                "conditions": conditions})
            with closing(app.get_db_connection()) as db, db:
                cache_terms(db.cursor(), [data["version"]])
            QMessageBox.information(self, "Клиентские доплаты",
                                    "Новая версия сохранена. Старые записи не пересчитаны.\n"
                                    "Отчёты → Другие действия → Предпросмотр условий для открытых записей.")
        except (ValueError, RuntimeError, sqlite3.Error, RequestException) as error:
            QMessageBox.warning(self, "Клиентские доплаты", str(error))

    def preview_customer_terms(self):
        app = sys.modules[self.__class__.__module__]
        try:
            report = self.collect_invoice_report()
            self._terms_versions({"company": report["company"], "name": report["object"]})
            proposals = []
            def preview_total(row):
                segments = report.get("journal_segments", {}).get(row["sync_id"], [])
                split_ok = row["entry_type"] != "ob" or abs(
                    sum(float(segment["hours"]) for segment in segments) - row["hours"]) < 0.000001
                calculated = prepare_customer_costs(dict(
                    report, journal_rows=[row], warnings=[], totals={"without_ob_hours": 0.0 if split_ok else None}))
                return calculated["totals"]["invoice_total"]
            with closing(app.get_db_connection()) as db:
                for row in report["journal_rows"]:
                    if row["entry_type"] not in ("work", "ob") or row["invoiced"]:
                        continue
                    prices = row_prices(row)
                    if prices.get("fixed"):
                        continue
                    proposed = intervals.attach_terms(db.cursor(), row, dict(prices))
                    if "interval_terms" not in proposed:
                        continue
                    proposed.pop("interval_calculation", None)
                    proposed.pop("schedule_application", None)
                    # Preserve individual base/travel prices, never replace them from today's catalogue.
                    proposed.update(version=1, company=row["company"], object_name=row["object_name"],
                                    date_from=row["date"], date_to=row["date"], source="Условия объекта: явный предпросмотр",
                                    confirmed=True, confirmed_at=datetime.now(timezone.utc).isoformat())
                    try:
                        if not row.get("time_facts"):
                            if not getattr(self, "_customer_schedules_supported", False):
                                raise ValueError("Сервер ещё не поддерживает типовой график. Обновите сервер.")
                            proposed = intervals.apply_schedule(row, proposed)
                        parts = intervals.calculated_row(row, proposed)
                        single = dict(row, customer_pricing=intervals.encoded(proposed))
                        total = preview_total(single)
                        description = f"{total:.2f} SEK" if total is not None else "Уточните клиентские цены/проценты/транспорт"
                        source = "Типовой график\n" if proposed.get("schedule_application") else "Фактическое время\n"
                        source += "\n".join(f"{p['start'][11:16]}–{p['end'][11:16]} "
                                            f"{'OT' if p['mode'] == 'overtime' else 'обычная'}" for p in parts)
                    except ValueError as error:
                        description = str(error)
                        source, total = "Требуется уточнение администратора", None
                    old_total = preview_total(row)
                    before = f"{old_total:.2f} SEK" if old_total is not None else "Итог не определён"
                    proposals.append((row, proposed, description, source, before, total is not None))
            if not proposals:
                raise ValueError("Нет открытых записей с действующей версией условий; задайте её в карточке объекта.")
            dialog = QDialog(self)
            dialog.setWindowTitle("Предпросмотр условий — только отмеченные открытые записи")
            layout = QVBoxLayout(dialog)
            note = QLabel("Отметьте только согласованные записи. Типовой график — допущение для клиентского "
                          "расчёта, а не фактическое время. Часы, OB, транспорт и зарплата не меняются. "
                          "Закрытые и Fast pris исключены. Если смена отличалась — нажмите «Уточнить».")
            note.setWordWrap(True)
            layout.addWidget(note)
            table = QTableWidget(len(proposals), 6)
            table.setObjectName("customerTermsPreview")
            table.setHorizontalHeaderLabels(["Применить", "Запись", "Было, без НДС",
                                             "Источник / режим", "Станет / уточнение", "Уточнение"])
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
            table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            for index, (row, proposed, description, source, before, eligible) in enumerate(proposals):
                check = QTableWidgetItem()
                check.setFlags((Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                               if eligible else Qt.ItemFlag.NoItemFlags)
                check.setCheckState(Qt.CheckState.Unchecked)
                table.setItem(index, 0, check)
                for column, value in enumerate((
                    f"№{row['id']} · {row['date']}\n{row['employee']}",
                    before, source, description,
                ), 1):
                    cell = QTableWidgetItem(value)
                    cell.setToolTip(value)
                    cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    table.setItem(index, column, cell)
                button = QPushButton("Уточнить")
                def clarify(_=False, identifier=row["id"]):
                    self.edit_time_facts(identifier)
                    dialog.reject()
                button.clicked.connect(clarify)
                table.setCellWidget(index, 5, button)
            layout.addWidget(table)
            table.horizontalHeader().sectionResized.connect(lambda *_: table.resizeRowsToContents())
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить выбор")
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            dialog.resize(1100, 500)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            chosen = [p for index, p in enumerate(proposals)
                      if table.item(index, 0).checkState() == Qt.CheckState.Checked]
            if not chosen:
                raise ValueError("Не отмечены записи для применения условий.")
            db = app.get_db_connection()
            try:
                db.execute("BEGIN IMMEDIATE")
                for old, quote, *_ in chosen:
                    actual = db.execute("SELECT * FROM shifts WHERE id=?", (old["id"],)).fetchone()
                    if actual is None or dict(actual) != old:
                        raise ValueError("Записи изменились после предпросмотра; обновите отчёт.")
                    store_pricing(db.cursor(), "shifts", old["sync_id"], quote)
                    app.enqueue_matching_shifts(db.cursor(), "id=?", (old["id"],))
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            self.generate_invoice()
            self.background_sync_with_cloud()
        except (ValueError, RuntimeError, sqlite3.Error, RequestException) as error:
            QMessageBox.warning(self, "Предпросмотр условий", str(error))

    def prepare_new_time_facts(self):
        try:
            if self.record_type_cb.currentData() != "work":
                raise ValueError("Для нового ввода выберите «Работа». Зарплатный OB — отдельный прежний механизм.")
            dialog = QDialog(self)
            dialog.setWindowTitle("Фактическое время новой смены")
            layout = QVBoxLayout(dialog)
            editor = TimeFactsEditor(getattr(self, "__dict__", {}).get("_new_time_facts"))
            layout.addWidget(editor)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            def validate_new_time():
                try:
                    editor.value(self.date_input.text())
                except ValueError as error:
                    QMessageBox.warning(dialog, "Проверьте время", str(error))
                    return
                dialog.accept()
            buttons.accepted.connect(validate_new_time)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            facts = editor.value(self.date_input.text())
            hours = intervals.paid_hours(facts, self.date_input.text())
            self._new_time_facts = facts
            self.hours_input.setText(f"{hours:g}")
            self.new_time_status.setText(f"{facts['start']}–{facts['end']} · {hours:g} ч без перерывов")
        except ValueError as error:
            QMessageBox.warning(self, "Время", str(error))

    def clear_new_time_facts(self):
        self._new_time_facts = None
        self.new_time_status.setText("Только часы; фактические интервалы не заданы.")

    def edit_time_facts(self, identifier=None):
        app = sys.modules[self.__class__.__module__]
        if identifier is None:
            selected = self.table_shifts.currentRow()
            if selected < 0:
                QMessageBox.warning(self, "Время и режим", "Выберите смену.")
                return
            identifier = int(self.table_shifts.item(selected, 0).text())
        try:
            with closing(app.get_db_connection()) as db:
                row = db.execute("SELECT * FROM shifts WHERE id=?", (identifier,)).fetchone()
                if row is None:
                    raise ValueError("Смена не найдена.")
                row = dict(row)
                if row["invoiced"]:
                    raise ValueError("Закрытая смена защищена. Сначала явно верните её в работу.")
                if row["entry_type"] not in ("work", "ob"):
                    raise ValueError("Фактические интервалы вводятся только для работы.")
            dialog = QDialog(self)
            dialog.setWindowTitle(f"Время и режим — №{identifier} · {row['employee']} · {row['date']}")
            layout = QVBoxLayout(dialog)
            note = QLabel(f"Сохранено {row['hours']:g} ч. Старые OB-часы не превращаются в интервалы автоматически.\n"
                          "Зарплатные OB-сегменты не меняются. Для OB сумма часов должна остаться прежней.")
            note.setWordWrap(True)
            layout.addWidget(note)
            value = row.get("time_facts")
            if not value and row.get("start_time") and row.get("end_time"):
                value = {"start": row["start_time"], "end": row["end_time"],
                         "next_day": row["end_time"] <= row["start_time"]}
            editor = TimeFactsEditor(value)
            layout.addWidget(editor)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            def validate_edited_time():
                try:
                    editor.value(row["date"], row["hours"] if row["entry_type"] == "ob" else None)
                except ValueError as error:
                    QMessageBox.warning(dialog, "Проверьте время", str(error))
                    return
                dialog.accept()
            buttons.accepted.connect(validate_edited_time)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            facts = editor.value(row["date"], row["hours"] if row["entry_type"] == "ob" else None)
            hours = intervals.paid_hours(facts, row["date"])
            if hours != row["hours"] and QMessageBox.question(
                    self, "Изменение оплачиваемых часов",
                    f"{row['hours']:g} → {hours:g} ч. Это изменит также базовые зарплатные часы этой рабочей записи. "
                    "Клиентские проценты в зарплату не передаются. Сохранить?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            db = app.get_db_connection()
            try:
                db.execute("BEGIN IMMEDIATE")
                actual = db.execute("SELECT * FROM shifts WHERE id=?", (identifier,)).fetchone()
                if actual is None or dict(actual) != row:
                    raise ValueError("Смена изменилась в другом окне; откройте её заново.")
                if db.execute("SELECT 1 FROM shift_sync_outbox WHERE sync_id=? AND operation='delete' "
                              "AND state IN ('pending','blocked')", (row["sync_id"],)).fetchone():
                    raise ValueError("Сначала дождитесь ответа на запрос удаления.")
                ensure_pricing_schema(db.cursor(), "shifts")
                db.execute("UPDATE shifts SET hours=? WHERE id=?", (hours, identifier))
                intervals.store_time(db.cursor(), "shifts", row["sync_id"], facts, previous_row=row)
                quote = row_prices(row)
                if quote.get("interval_calculation") or quote.get("schedule_application") or quote.get("supplement_allocation"):
                    quote.pop("interval_calculation", None)
                    quote.pop("schedule_application", None)
                    quote.pop("supplement_allocation", None)
                    store_pricing(db.cursor(), "shifts", row["sync_id"], quote)
                app.enqueue_matching_shifts(db.cursor(), "id=?", (identifier,))
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            if hasattr(self, "table_shifts"):
                self.load_shifts_history()
            self.background_sync_with_cloud()
        except (ValueError, RuntimeError, sqlite3.Error) as error:
            QMessageBox.warning(self, "Время не сохранено", str(error))

    def customer_interval_breakdown(self):
        try:
            report = self._prepare_invoice_view(self.collect_invoice_report())
            dialog = QDialog(self)
            dialog.setWindowTitle("Клиентский расчёт по интервалам — без НДС")
            layout = QVBoxLayout(dialog)
            note = QLabel("Часы учитываются один раз. Клиентские проценты не меняют зарплатные OB. "
                          "Выберите строку и «Уточнить время и режим»; затем заново откройте расчёт.")
            note.setWordWrap(True)
            layout.addWidget(note)
            unresolved = [line["label"] for line in report["cost_lines"] if line["amount"] is None]
            if unresolved:
                status = QLabel("\n".join(unresolved))
                status.setWordWrap(True)
                layout.addWidget(status)
            columns = ["Дата", "Сотрудник", "Интервал", "Режим", "Часы", "База", "Доплата %", "Доплата SEK",
                       "Итог SEK", "Источник времени"]
            parts = report.get("interval_details", [])
            table = QTableWidget(len(parts), len(columns))
            table.setObjectName("customerIntervalBreakdown")
            table.setHorizontalHeaderLabels(columns)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            for index, part in enumerate(parts):
                values = [part["date"], part["employee"], part["start"][11:16] + "–" + part["end"][11:16],
                          "Сверхурочная" if part["mode"] == "overtime" else "Обычная",
                          f"{float(part['hours']):g}", part["base"], part["percent"], part["premium_amount"], part["total"],
                          "График — подтверждён администратором" if part.get("time_source") == "schedule" else "Фактическое"]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value) if value is not None else "Не задано")
                    item.setData(Qt.ItemDataRole.UserRole, part["shift_id"])
                    table.setItem(index, column, item)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            layout.addWidget(table)
            total = report["totals"]["invoice_total"]
            total_label = QLabel(
                f"Всего: {report['totals']['hours']:g} ч · "
                + (f"{total:,.2f} SEK без НДС" if total is not None else "Итог не определён — уточните данные и условия."))
            total_label.setObjectName("customerIntervalTotal")
            layout.addWidget(total_label)
            scheduled = {p["shift_id"] for p in parts if p.get("time_source") == "schedule"}
            if scheduled:
                layout.addWidget(QLabel("По типовым графикам, явно подтверждённым администратором: "
                                        + ", ".join(f"№{identifier}" for identifier in sorted(scheduled))))
            missing = [row for row in report["journal_rows"] if row["entry_type"] in ("work", "ob")
                       and not row.get("time_facts") and not row_prices(row).get("schedule_application")]
            missing_label = QLabel("Уточните начало, конец, перерывы и обычный/сверхурочный режим: "
                                   + ", ".join(f"№{r['id']}" for r in missing)
                                   if missing else "Источник каждого участка указан в расшифровке.")
            missing_label.setWordWrap(True)
            layout.addWidget(missing_label)
            actions = QHBoxLayout()
            edit = QPushButton("Уточнить время и режим")
            def correct():
                item = table.item(table.currentRow(), 0)
                if item:
                    self.edit_time_facts(item.data(Qt.ItemDataRole.UserRole))
                    dialog.accept()
            edit.clicked.connect(correct)
            actions.addWidget(edit)
            for row in missing:
                button = QPushButton(f"Уточнить №{row['id']}")
                button.clicked.connect(lambda _=False, identifier=row["id"]: self.edit_time_facts(identifier))
                actions.addWidget(button)
            layout.addLayout(actions)
            dialog.resize(1250, 600)
            dialog.exec()
        except (ValueError, sqlite3.Error) as error:
            QMessageBox.warning(self, "Клиентский расчёт", str(error))
