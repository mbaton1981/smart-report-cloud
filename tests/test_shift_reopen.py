import json
import unittest
from unittest.mock import patch

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
        window.load_dropdowns = lambda: None
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
        window.load_dropdowns = lambda: None
        with patch.object(self.desktop.desktop_auth, "current_user", return_value={"role": "admin"}):
            window.setup_hours_tab()
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
        window.inv_date_from = self.desktop.QDateEdit()
        window.inv_date_to = self.desktop.QDateEdit()
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
