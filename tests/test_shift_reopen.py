import json
import unittest
from unittest.mock import patch
from PyQt6.QtWidgets import QDateEdit

import test_catalog_sync as catalog
import test_ob_transport as transport


class ReopenShiftTests(unittest.TestCase):
    setUpClass = classmethod(transport.OBTransportTests.setUpClass.__func__)
    tearDownClass = classmethod(transport.OBTransportTests.tearDownClass.__func__)
    _add_company = transport.OBTransportTests._add_company
    submit = transport.OBTransportTests.submit
    records = transport.OBTransportTests.records

    def setUp(self):
        transport.OBTransportTests.setUp(self)
        auth = patch.object(self.server, "bearer_admin_user", return_value={"role": "admin", "username": "test-admin"})
        auth.start()
        self.addCleanup(auth.stop)
        self.addCleanup(self.qt_app.processEvents)

    def worker(self):
        with patch.object(self.desktop, "SYNC_API_KEY", "isolated-sync-test-key"):
            return catalog.CatalogSyncTests._worker(self)

    def closed(self, kind="work"):
        self.submit("reopen-target", kind, "Travel B", 1)
        self.submit("untouched-neighbor", "work", "Travel A", 0)
        c = self.server.get_db_connection()
        try:
            c.execute("UPDATE incoming_submissions SET invoiced=1")
            c.commit()
        finally:
            c.close()
        self.worker()
        return next(row for row in self.records() if row["sync_id"] == "reopen-target")

    def server_rows(self):
        c = self.server.get_db_connection()
        try:
            return {r["sync_id"]: dict(r) for r in c.execute("SELECT * FROM incoming_submissions")}
        finally:
            c.close()

    def reopen(self, row, operation="reopen-1"):
        return self.server_client.post("/api/admin/shifts/reopen", json={
            "sync_id": row["sync_id"], "version": row["version"], "operation_id": operation})

    def window(self):
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        self.addCleanup(window.deleteLater)
        window.table_shifts = self.desktop.QTableWidget(0, 15)
        window.reopen_shift_btn = self.desktop.QPushButton()
        window.reopen_shift_btn.clicked.connect(window.reopen_selected_shift)
        window.load_dropdowns = lambda **kwargs: None
        window.apply_shifts_filters = lambda: None
        window.load_shifts_history()
        for index in range(window.table_shifts.rowCount()):
            c = self.desktop.get_db_connection()
            try:
                target = c.execute("SELECT id FROM shifts WHERE sync_id='reopen-target'").fetchone()[0]
            finally:
                c.close()
            if window.table_shifts.item(index, 0).text() == str(target):
                window.table_shifts.selectRow(index)
                break
        return window

    def local_rows(self):
        c = self.desktop.get_db_connection()
        try:
            return {r["sync_id"]: dict(r) for r in c.execute("SELECT * FROM shifts")}
        finally:
            c.close()

    def test_journal_filters_before_limit_and_company_controls_object_choices(self):
        self._add_company("Other")
        c = self.desktop.get_db_connection()
        self.addCleanup(c.close)
        c.execute("INSERT INTO employees(name,is_active) VALUES ('Filter Person',1)")
        c.executemany(
            "INSERT INTO objects(company,name,markning,is_active,sync_id) VALUES (?,?,?,1,?)",
            [("Renatur", "Current object", "a", "object-a"),
             ("Other", "Other object", "b", "object-b")],
        )
        c.execute("INSERT INTO object_name_history(sync_id,company,name) VALUES ('object-a','Renatur','Old object')")
        c.executemany(
            "INSERT INTO shifts(date,employee,company,object_name,hours,rate,invoiced,hours_confirmed,sync_id,entry_type) "
            "VALUES ('2026-11-09',?,?,?,?,0,?,?,?,'work')",
            [("Filter Person", "Renatur", "Old object", 8, 0, 0, "shift-a"),
             ("Filter Person", "Renatur", "Current object", 8, 0, 1, "shift-b"),
             ("Filter Person", "Renatur", "Current object", 8, 1, 1, "shift-c"),
             ("Filter Person", "Other", "Other object", 8, 0, 0, "shift-d"),
             ("Filter Person", "", "", 8, 0, 0, "shift-empty")],
        )
        c.executemany(
            "INSERT INTO shifts(date,employee,company,object_name,hours,rate,invoiced,sync_id,entry_type) "
            "VALUES ('2026-11-10','Other person','Other','Other object',8,0,0,?,'work')",
            [(f"filler-{i}",) for i in range(205)],
        )
        c.commit()
        c.close()
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        self.addCleanup(window.close)
        self.addCleanup(window.deleteLater)
        window.tab_hours = self.desktop.QWidget()
        window.setCentralWidget(window.tab_hours)
        with patch.object(self.desktop.desktop_auth, "current_user", return_value={"role": "admin"}), \
                patch.object(self.desktop.QMessageBox, "critical") as critical:
            window.setup_hours_tab()
            window.filter_date_from.setDate(self.desktop.QDate(2026, 11, 1))
            window.filter_date_to.setDate(self.desktop.QDate(2026, 11, 30))
            def ids():
                return {window.table_shifts.item(i, 0).data(self.desktop.Qt.ItemDataRole.UserRole + 1)
                        for i in range(window.table_shifts.rowCount())}
            self.assertEqual(len(ids()), 200)
            self.assertNotIn("shift-a", ids())
            window.filter_emp.setCurrentText("Filter Person")
            self.assertEqual(ids(), {"shift-a", "shift-b", "shift-c", "shift-d", "shift-empty"})
            window.filter_emp.setCurrentIndex(0)
            window.filter_comp.setCurrentText("Renatur")
            self.assertEqual(ids(), {"shift-a", "shift-b", "shift-c"})
            window.filter_comp.setCurrentIndex(0)
            window.filter_status.setCurrentText("Часы подтверждены")
            self.assertEqual(ids(), {"shift-b"})
            window.filter_status.setCurrentText("Выставлен в счёт")
            self.assertEqual(ids(), {"shift-c"})
            window.filter_status.setCurrentIndex(0)
            window.filter_obj.setCurrentIndex(window.filter_obj.findData("object-a"))
            self.assertEqual(ids(), {"shift-a", "shift-b", "shift-c"})
            window.filter_emp.setCurrentText("Filter Person")
            window.filter_comp.setCurrentText("Renatur")
            self.assertEqual(window.filter_obj.currentData(), "object-a")
            self.assertEqual(window.filter_obj.findData("object-b"), -1)
            window.filter_status.setCurrentText("В работе")
            self.assertEqual(ids(), {"shift-a"})
            window.filter_status.setCurrentText("Часы подтверждены")
            self.assertEqual(ids(), {"shift-b"})
            window.filter_status.setCurrentText("Выставлен в счёт")
            self.assertEqual(ids(), {"shift-c"})
            window.filter_status.setCurrentIndex(0)
            window.filter_obj.setCurrentIndex(0)
            self.assertEqual(ids(), {"shift-a", "shift-b", "shift-c"})
            window.filter_comp.setCurrentIndex(0)
            self.assertEqual(ids(), {"shift-a", "shift-b", "shift-c", "shift-d", "shift-empty"})
            window.filter_emp.setCurrentIndex(0)
            self.assertEqual(len(ids()), 200)
            critical.assert_not_called()

    def test_new_ob_hides_work_time_editor_preserving_unsaved_work_draft(self):
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        self.addCleanup(window.close)
        self.addCleanup(window.deleteLater)
        window.tab_hours = self.desktop.QWidget()
        window.setCentralWidget(window.tab_hours)
        with patch.object(self.desktop.desktop_auth, "current_user", return_value={"role": "admin"}):
            window.setup_hours_tab()
            window._new_time_facts = {"draft": "preserved"}
            window.record_type_cb.setCurrentIndex(window.record_type_cb.findData("ob"))
            self.assertTrue(all(widget.isHidden() for widget in window._work_time_widgets))
            self.assertFalse(window.ob_start_time.isHidden())
            with patch.object(self.desktop.QMessageBox, "question",
                              return_value=self.desktop.QMessageBox.StandardButton.No):
                self.assertIsNone(window.save_shift())
            self.assertEqual(window._new_time_facts, {"draft": "preserved"})
            window.record_type_cb.setCurrentIndex(window.record_type_cb.findData("work"))
            self.assertTrue(all(not widget.isHidden() for widget in window._work_time_widgets))
            self.assertEqual(window._new_time_facts, {"draft": "preserved"})

    def test_desktop_work_and_ob_save_sync_and_reopen_in_journal(self):
        self.worker()
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        self.addCleanup(window.close)
        self.addCleanup(window.deleteLater)
        window.tab_hours = self.desktop.QWidget()
        window.setCentralWidget(window.tab_hours)
        with patch.object(self.desktop.desktop_auth, "current_user", return_value={"role": "admin"}), \
                patch.object(window, "background_sync_with_cloud"), \
                patch.object(self.desktop.QMessageBox, "information"), \
                patch.object(self.desktop.QMessageBox, "warning") as warning, \
                patch.object(self.desktop.QMessageBox, "critical") as critical, \
                patch.object(self.desktop.QMessageBox, "question",
                             return_value=self.desktop.QMessageBox.StandardButton.Yes):
            window.setup_hours_tab()
            window.date_input.setDate(self.desktop.QDate(2026, 10, 20))
            window.filter_date_from.setDate(self.desktop.QDate(2026, 10, 1))
            window.filter_date_to.setDate(self.desktop.QDate(2026, 10, 31))
            window.emp_cb.setCurrentText("Travel Test")
            window.comp_cb.setCurrentText("Renatur")
            window.obj_cb.setCurrentIndex(window.obj_cb.findData("Travel B"))
            for invalid in ("0", "0.25", "8.25", "24.5"):
                window.hours_input.setText(invalid)
                self.assertIsNone(window.save_shift())
                self.assertTrue(warning.called)
                warning.reset_mock()
            c = self.desktop.get_db_connection()
            self.assertEqual(c.execute("SELECT COUNT(*) FROM shifts").fetchone()[0], 0)
            c.close()
            window.hours_input.setText("8")
            window.trans_input.setChecked(True)
            self.assertTrue(window.save_shift())
            window._new_time_facts = {"draft": "preserved until confirmed"}
            window.record_type_cb.setCurrentIndex(window.record_type_cb.findData("ob"))
            window.ob_start_time.setCurrentText("16:00")
            window.ob_end_time.setCurrentText("20:00")
            self.assertTrue(window.save_shift())
            self.assertIsNone(window._new_time_facts)
            c = self.desktop.get_db_connection()
            local = [dict(row) for row in c.execute("SELECT * FROM shifts ORDER BY id")]
            c.close()
            self.assertEqual([(r["entry_type"], r["hours"], r["transport"]) for r in local],
                             [("work", 8, 1), ("ob", 4, 1)])
            self.assertIsNone(local[1]["time_facts"])
            self.worker()
            self.worker()
            remote = self.server_rows()
            for row in local:
                self.assertIn(row["sync_id"], remote)
                self.assertEqual(remote[row["sync_id"]]["hours"], row["hours"])
                self.assertEqual(remote[row["sync_id"]]["transport"], 1)
            window.load_shifts_history()
            self.assertEqual(window.table_shifts.rowCount(), 2)
            self.assertEqual({window.table_shifts.item(i, 0).data(self.desktop.Qt.ItemDataRole.UserRole + 1)
                              for i in range(2)}, {row["sync_id"] for row in local})
            ob_row = next(i for i in range(2) if window.table_shifts.item(i, 5).text() == "OB")
            self.assertEqual(window.table_shifts.item(ob_row, 6).text(), "4.0")
            self.assertIn("16:00", window.table_shifts.item(ob_row, 7).text())
            for i in range(2):
                expected_type = "ob" if window.table_shifts.item(i, 5).text() == "OB" else "work"
                window.table_shifts.setCurrentCell(i, 1)
                def inspect(dialog):
                    kind = next(combo for combo in dialog.findChildren(self.desktop.QComboBox)
                                if combo.findData("ob") >= 0 and combo.findData("work") >= 0)
                    self.assertEqual(kind.currentData(), expected_type)
                    hours = dialog.findChild(self.desktop.QDoubleSpinBox, "edit_shift_hours")
                    self.assertEqual(hours.value(), 4 if expected_type == "ob" else 8)
                    return self.desktop.QDialog.DialogCode.Rejected
                with patch.object(self.desktop.QDialog, "exec", inspect):
                    window.edit_shift()
            warning.assert_not_called()
            critical.assert_not_called()

    def gui_reopen(self, window, mode=None, role="admin", saved_role=None):
        def post(url, **kwargs):
            if mode == "offline":
                raise self.desktop.requests.ConnectionError("isolated offline")
            response = self.server_client.post("/api/admin/shifts/reopen", json=kwargs["json"])
            class Response:
                status_code = response.status_code
                def json(self):
                    return response.get_json()
            return Response()
        with patch.object(self.desktop.desktop_auth, "current_user", return_value={"role": role}), \
                patch.object(self.desktop.desktop_auth, "saved_account_role", return_value=saved_role), \
                patch.object(self.desktop.requests, "post", side_effect=post) as request, \
                patch.object(self.desktop.QMessageBox, "question",
                             return_value=self.desktop.QMessageBox.StandardButton.Yes) as confirm, \
                patch.object(self.desktop.QMessageBox, "warning") as warning, \
                patch.object(self.desktop.QMessageBox, "information") as info:
            window.reopen_selected_shift()
        return request, confirm, warning, info

    def test_gui_success_single_row_clears_rejected_delete_without_replaying_it(self):
        self.closed()
        c = self.desktop.get_db_connection()
        try:
            row = c.execute("SELECT * FROM shifts WHERE sync_id='reopen-target'").fetchone()
            self.desktop.enqueue_shift_operation(c.cursor(), row["sync_id"], "delete", row["sync_version"],
                                                self.desktop.shift_payload(row), state="conflict")
            c.commit()
        finally:
            c.close()
        before, remote_before = self.local_rows(), self.server_rows()
        window = self.window()
        request, confirm, warning, info = self.gui_reopen(window)
        warning.assert_not_called()
        info.assert_called_once()
        request.assert_called_once()
        for expected in ("Travel Test", "2026-10-20", "Travel B", "8 ч"):
            self.assertIn(expected, confirm.call_args.args[2])
        after = self.local_rows()
        self.assertEqual(after["reopen-target"]["invoiced"], 0)
        self.assertEqual(after["untouched-neighbor"], before["untouched-neighbor"])
        self.assertEqual(self.server_rows()["untouched-neighbor"], remote_before["untouched-neighbor"])
        self.assertNotIn("Удаление отклонено", window.table_shifts.item(window.table_shifts.currentRow(), 11).text())
        self.assertFalse(window.reopen_shift_btn.isEnabled())
        c = self.desktop.get_db_connection()
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM shift_sync_outbox").fetchone()[0], 0)
        finally:
            c.close()
        repeated = self.gui_reopen(window)
        repeated[0].assert_not_called()
        repeated[2].assert_called_once()
        for _ in range(2):
            self.worker()
        self.assertEqual(self.local_rows()["reopen-target"]["invoiced"], 0)
        self.assertFalse(self.server_rows()["reopen-target"]["is_deleted"])

    def test_hours_tab_stockholm_month_quick_periods_and_add_shift_dialog(self):
        self.closed()
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        self.addCleanup(window.close)
        self.addCleanup(window.deleteLater)
        window.tab_hours = self.desktop.QWidget()
        window.setCentralWidget(window.tab_hours)
        window.load_dropdowns = lambda **kwargs: None
        with patch.object(self.desktop.desktop_auth, "current_user", return_value={"role": "admin"}):
            window.setup_hours_tab()

        # Запуск: текущий календарный месяц по времени Стокгольма, а не прежний октябрь.
        start, end = self.desktop.stockholm_month_bounds()
        self.assertEqual(window.filter_date_from.text(), start)
        self.assertEqual(window.filter_date_to.text(), end)

        # Быстрый выбор «Прошлый месяц»; произвольный период остаётся доступным.
        window.set_shift_period_month(-1)
        previous_start, previous_end = self.desktop.stockholm_month_bounds(-1)
        self.assertEqual(window.filter_date_from.text(), previous_start)
        self.assertEqual(window.filter_date_to.text(), previous_end)

        # Выбранный период сохраняется при обновлении журнала и синхронизации.
        window.load_shifts_history()
        self.assertEqual(window.filter_date_from.text(), previous_start)
        self.assertEqual(window.filter_date_to.text(), previous_end)

        # Добавление смены через отдельное окно обновляет журнал.
        conn = self.desktop.get_db_connection()
        conn.execute("INSERT INTO employees (name,is_active) VALUES ('Dialog Employee',1)")
        conn.execute("INSERT INTO objects (company,name,markning,rate,transport_rate,is_active) "
                     "VALUES ('Renatur','Dialog object','p-dialog',500,0,1)")
        conn.commit()
        rows_before = conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0]
        conn.close()
        window.emp_cb.addItem("Dialog Employee")
        window.comp_cb.setCurrentText("Renatur")
        window.obj_cb.addItem("Dialog object")
        window.obj_cb.setCurrentText("Dialog object")
        window.date_input.setDate(self.desktop.QDate.fromString(previous_start, "yyyy-MM-dd"))
        window.hours_input.setText("8.0")
        table_rows_before = window.table_shifts.rowCount()
        with patch.object(self.desktop.QMessageBox, "information"), \
                patch.object(self.desktop.QMessageBox, "warning") as warning, \
                patch.object(window, "background_sync_with_cloud"):
            window._save_shift_from_dialog()
        self.assertEqual(warning.call_count, 0,
                         warning.call_args_list and warning.call_args_list[0].args)
        self.assertEqual(window.shift_form_dialog.result(),
                         self.desktop.QDialog.DialogCode.Accepted)
        self.assertEqual(window.table_shifts.rowCount(), table_rows_before + 1)
        conn = self.desktop.get_db_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0], rows_before + 1)
        conn.close()
        # Период не сброшен после сохранения и обновления журнала.
        self.assertEqual(window.filter_date_from.text(), previous_start)

        # Отмена ничего не записывает.
        window.hours_input.setText("5.5")
        window.shift_form_dialog.reject()
        conn = self.desktop.get_db_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0], rows_before + 1)
        conn.close()
        self.assertEqual(window.table_shifts.rowCount(), table_rows_before + 1)

    def test_real_hours_tab_mouse_selection_enables_reopen_for_first_current_row(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        self.closed()
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        self.addCleanup(window.close)
        self.addCleanup(window.deleteLater)
        window.tab_hours = self.desktop.QWidget()
        window.setCentralWidget(window.tab_hours)
        window.load_dropdowns = lambda **kwargs: None
        with patch.object(self.desktop.desktop_auth, "current_user", return_value={"role": "admin"}):
            window.setup_hours_tab()
            # Журнал по умолчанию показывает текущий месяц; тестовые смены — октябрь 2026.
            window.filter_date_from.setDate(self.desktop.QDate.fromString("2026-10-01", "yyyy-MM-dd"))
            window.filter_date_to.setDate(self.desktop.QDate.fromString("2026-10-31", "yyyy-MM-dd"))
            window.resize(1450, 1000)
            window.show()
            self.qt_app.processEvents()
            self.assertEqual(window.table_shifts.currentRow(), -1)
            item = window.table_shifts.item(0, 2)
            QTest.mouseClick(window.table_shifts.viewport(), Qt.MouseButton.LeftButton,
                             pos=window.table_shifts.visualItemRect(item).center())
            self.assertEqual(window.table_shifts.currentRow(), 0)
            self.assertTrue(window.reopen_shift_btn.isEnabled())
            window.table_shifts.setCurrentCell(-1, -1)
            self.assertFalse(window.reopen_shift_btn.isEnabled())
            selection = window.table_shifts.selectionModel()
            index = window.table_shifts.model().index(0, 2)
            from PyQt6.QtCore import QItemSelectionModel
            selection.select(index, QItemSelectionModel.SelectionFlag.ClearAndSelect
                             | QItemSelectionModel.SelectionFlag.Rows)
            self.assertFalse(window.reopen_shift_btn.isEnabled())
            selection.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
            self.assertEqual(window.table_shifts.currentRow(), 0)
            self.assertTrue(window.reopen_shift_btn.isEnabled())

    def test_gui_offline_and_no_rights_leave_local_status_and_queue_unchanged(self):
        self.closed()
        before = self.local_rows()
        window = self.window()
        request, _, warning, info = self.gui_reopen(window, mode="offline")
        request.assert_called_once()
        warning.assert_called_once()
        info.assert_not_called()
        self.assertEqual(self.local_rows(), before)
        denied = self.gui_reopen(window, role="employee")
        denied[0].assert_not_called()
        denied[2].assert_called_once()
        with patch.object(self.server, "bearer_admin_user", return_value=None):
            refused = self.gui_reopen(window)
        refused[2].assert_called_once()
        refused[3].assert_not_called()
        self.assertEqual(self.local_rows(), before)

    def test_offline_cached_director_can_request_but_cannot_change_status_without_server(self):
        self.closed()
        window = self.window()
        before = self.local_rows()
        request, _, warning, info = self.gui_reopen(window, mode="offline", role="", saved_role="admin")
        request.assert_called_once()
        self.assertIn("Связь прервана", warning.call_args.args[2])
        info.assert_not_called()
        self.assertEqual(self.local_rows(), before)
        denied = self.gui_reopen(window, role="employee", saved_role="admin")
        denied[0].assert_not_called()
        self.assertIn("директору", denied[2].call_args.args[2])

    def test_server_role_and_stale_version_enforced(self):
        row = self.closed()
        before = self.server_rows()
        with patch.object(self.server, "bearer_admin_user", return_value=None):
            response = self.reopen(row)
            self.assertEqual(response.status_code, 403)
        stale = dict(row, version=row["version"] + 1)
        self.assertEqual(self.reopen(stale).status_code, 409)
        self.assertEqual(self.server_rows(), before)

    def test_hours_confirmation_is_server_locked_and_admin_reopen_clears_only_the_lock(self):
        self.submit("hours-confirmed", "work", "Travel B", 1)
        self.worker()
        connection = self.desktop.get_db_connection()
        try:
            local = connection.execute(
                "SELECT * FROM shifts WHERE sync_id='hours-confirmed'").fetchone()
            connection.execute("UPDATE shifts SET hours_confirmed=1 WHERE sync_id=?", (local["sync_id"],))
            local = connection.execute(
                "SELECT * FROM shifts WHERE sync_id='hours-confirmed'").fetchone()
            self.desktop.enqueue_shift_operation(
                connection.cursor(), local["sync_id"], "upsert", local["sync_version"],
                self.desktop.shift_payload(local))
            connection.commit()
        finally:
            connection.close()
        self.worker()
        row = next(item for item in self.records() if item["sync_id"] == "hours-confirmed")
        self.assertEqual(row["hours_confirmed"], 1)
        before = self.server_rows()["hours-confirmed"]

        edit = self.server_client.post("/edit-shift", json={
            "sync_id": row["sync_id"], "version": row["version"],
            "operation_id": "edit-confirmed-hours", "date": "2026-10-21",
        })
        self.assertEqual(edit.status_code, 400, edit.get_json())
        self.assertIn("защищены", edit.get_json()["error"])
        delete = self.server_client.post("/delete-shift", json={
            "sync_id": row["sync_id"], "version": row["version"],
            "operation_id": "delete-confirmed-hours",
        })
        self.assertEqual(delete.status_code, 409, delete.get_json())
        self.assertEqual(delete.get_json()["reason"], "hours_confirmed")

        rejected = self.server_client.post("/sync/push", headers={
            "X-Sync-Key": "isolated-sync-test-key",
        }, json={"changes": [{
            "sync_id": row["sync_id"], "base_version": row["version"],
            "operation_id": "sync-edit-confirmed-hours", "operation": "upsert",
            "shift": dict(row, date="2026-10-21"),
        }]})
        self.assertEqual(rejected.status_code, 400, rejected.get_json())
        self.assertIn("защищены", rejected.get_json()["error"])
        self.assertEqual(self.server_rows()["hours-confirmed"], before)

        reopened = self.reopen(row, "reopen-confirmed-hours")
        self.assertEqual(reopened.status_code, 200, reopened.get_json())
        self.assertTrue(reopened.get_json()["changed"])
        self.assertEqual(reopened.get_json()["shift"]["hours_confirmed"], 0)
        self.worker()
        self.assertEqual(self.local_rows()["hours-confirmed"]["hours_confirmed"], 0)

    def test_dataset_boundary_applies_before_single_shift_reopen(self):
        row = self.closed()
        before = self.server_rows()
        with patch("sync_dataset.file_dataset", return_value="isolated-reopen-dataset"):
            response = self.server_client.post("/api/admin/shifts/reopen", json={
                "sync_id": row["sync_id"], "version": row["version"], "operation_id": "wrong-dataset"},
                headers={"X-SmartReport-Dataset": "another-dataset"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "dataset_mismatch")
        self.assertEqual(self.server_rows(), before)

    def test_lost_reopen_reply_reconciles_without_replaying_rejected_delete(self):
        self.closed()
        c = self.desktop.get_db_connection()
        try:
            row = c.execute("SELECT * FROM shifts WHERE sync_id='reopen-target'").fetchone()
            self.desktop.enqueue_shift_operation(c.cursor(), row["sync_id"], "delete", row["sync_version"],
                                                self.desktop.shift_payload(row), state="conflict")
            c.commit()
        finally:
            c.close()
        row = next(r for r in self.records() if r["sync_id"] == "reopen-target")
        self.assertEqual(self.reopen(row).status_code, 200)
        self.worker()
        self.assertEqual(self.local_rows()["reopen-target"]["invoiced"], 0)
        c = self.desktop.get_db_connection()
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM shift_sync_outbox").fetchone()[0], 0)
        finally:
            c.close()
        self.worker()
        self.assertFalse(self.server_rows()["reopen-target"]["is_deleted"])

    def test_ob_reopen_preserves_payroll_segments_and_all_historical_prices(self):
        row = self.closed("ob")
        before = self.server_rows()
        c = self.server.get_db_connection()
        try:
            segments = [tuple(r) for r in c.execute("SELECT * FROM ob_segments")]
        finally:
            c.close()
        response = self.reopen(row)
        self.assertEqual(response.status_code, 200, response.get_json())
        retried = self.reopen(row)
        self.assertEqual(retried.status_code, 200, retried.get_json())
        self.assertFalse(retried.get_json()["changed"])
        after = self.server_rows()
        for key in ("customer_pricing", "hours", "start_time", "end_time", "ob_base_rate", "rate", "transport"):
            self.assertEqual(before[row["sync_id"]][key], after[row["sync_id"]][key], key)
        c = self.server.get_db_connection()
        try:
            self.assertEqual([tuple(r) for r in c.execute("SELECT * FROM ob_segments")], segments)
        finally:
            c.close()
        self.assertEqual(len(response.get_json()["shift"]["ob_segments"]), len(segments))

    def test_closed_payroll_period_server_refusal_keeps_gui_green(self):
        self.closed()
        from test_payroll_history import PARAMS
        import payroll_history
        headers = {"X-Sync-Key": "isolated-sync-test-key"}

        def payroll(kind, **values):
            current = self.server_client.get("/sync/payroll", headers=headers).get_json()
            response = self.server_client.post("/sync/payroll", headers=headers, json=dict(
                kind=kind, employee="Travel Test", operation_id="reopen-payroll-" + kind,
                expected_seq=len(current["events"]), **values))
            self.assertEqual(response.status_code, 200, response.get_json())
            return response.get_json()

        with patch.object(payroll_history, "WRITES_ENABLED", True):
            payroll("rate", effective_from="2026-10-01", rate=200)
            draft = payroll("preview", date_from="2026-10-01", date_to="2026-10-31",
                            parameters=PARAMS)["preview"]
            payroll("close", date_from="2026-10-01", date_to="2026-10-31",
                    parameters=PARAMS, preview_hash=draft["preview_hash"])
        before = self.local_rows()
        window = self.window()
        _, _, warning, info = self.gui_reopen(window)
        warning.assert_called_once()
        self.assertIn("остаётся закрытым", warning.call_args.args[2])
        info.assert_not_called()
        self.assertEqual(self.local_rows(), before)
        self.assertEqual(self.server_rows()["reopen-target"]["invoiced"], 1)

    def fixed_package(self, single=False):
        from fixed_pricing import create_fixed, fixed_quote
        self.closed()
        rows = [dict(row, entry_type=row["submission_type"], invoiced=0)
                for row in self.server_rows().values() if not single or row["sync_id"] == "reopen-target"]
        c = self.desktop.get_db_connection()
        try:
            obj = c.execute("SELECT * FROM objects WHERE name='Travel B'").fetchone()
            object_id, object_sync_id = obj["id"], obj["sync_id"]
        finally:
            c.close()
        fixed = create_fixed({
            "journal_rows": rows, "company": "Renatur", "sync_id": object_sync_id,
            "date_from": "2026-10-01", "date_to": "2026-10-31", "fast_pris_amount": "1000.00"})
        for connect, table in ((self.server.get_db_connection, "incoming_submissions"),
                               (self.desktop.get_db_connection, "shifts")):
            c = connect()
            try:
                for row in rows:
                    c.execute(f"UPDATE {table} SET customer_pricing=? WHERE sync_id=?",
                              (json.dumps(fixed_quote(row, fixed)), row["sync_id"]))
                c.commit()
            finally:
                c.close()
        return object_id

    def test_fixed_package_refuses_single_row_and_offers_correct_report_without_changes(self):
        object_id = self.fixed_package()
        before = self.server_rows()
        row = next(r for r in self.records() if r["sync_id"] == "reopen-target")
        response = self.reopen(row)
        self.assertEqual(response.status_code, 409)
        self.assertIn("целиком", response.get_json()["error"])
        window = self.window()
        window.inv_comp_cb = self.desktop.QComboBox()
        window.inv_comp_cb.addItem("Renatur")
        window.inv_date_from = QDateEdit()
        window.inv_date_to = QDateEdit()
        window.inv_obj_cb = self.desktop.QComboBox()
        window.inv_obj_cb.addItem("Travel B", object_id)
        window.inv_show_archived = self.desktop.QCheckBox()
        window.load_invoice_objects = lambda company: None
        window.tab_invoice = self.desktop.QWidget()
        window.tabs = self.desktop.QTabWidget()
        window.tabs.addTab(self.desktop.QWidget(), "Input")
        window.tabs.addTab(window.tab_invoice, "Report")
        request, confirm, warning, info = self.gui_reopen(window)
        request.assert_not_called()
        warning.assert_not_called()
        info.assert_not_called()
        self.assertIn("Никакие смены", confirm.call_args.args[2])
        self.assertIs(window.tabs.currentWidget(), window.tab_invoice)
        self.assertEqual(window.inv_date_from.date().toString("yyyy-MM-dd"), "2026-10-01")
        self.assertEqual(window.inv_date_to.date().toString("yyyy-MM-dd"), "2026-10-31")
        self.assertEqual(window.inv_obj_cb.currentData(), object_id)
        self.assertEqual(self.server_rows(), before)
        self.assertTrue(all(row["invoiced"] for row in self.local_rows().values()))

    def test_single_member_fixed_reopen_preserves_original_agreement_in_audit(self):
        self.fixed_package(single=True)
        before = self.server_rows()
        row = next(r for r in self.records() if r["sync_id"] == "reopen-target")
        response = self.reopen(row)
        self.assertEqual(response.status_code, 200, response.get_json())
        after = self.server_rows()
        old_quote = json.loads(before["reopen-target"]["customer_pricing"])
        new_quote = json.loads(after["reopen-target"]["customer_pricing"])
        old_quote.pop("fixed")
        self.assertEqual(new_quote, old_quote)
        c = self.server.get_db_connection()
        try:
            audit = c.execute("SELECT previous_json FROM customer_price_audit "
                              "WHERE shift_sync_id='reopen-target' ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(audit[0], before["reopen-target"]["customer_pricing"])
        finally:
            c.close()
        self.assertEqual(after["untouched-neighbor"], before["untouched-neighbor"])
