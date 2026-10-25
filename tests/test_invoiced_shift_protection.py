import unittest
from unittest.mock import patch

import test_ob_transport as transport
import test_customer_pricing as pricing
import test_catalog_sync as catalog


class InvoicedServerProtectionTests(unittest.TestCase):
    setUpClass = classmethod(transport.OBTransportTests.setUpClass.__func__)
    tearDownClass = classmethod(transport.OBTransportTests.tearDownClass.__func__)
    setUp = transport.OBTransportTests.setUp
    _add_company = transport.OBTransportTests._add_company
    submit = transport.OBTransportTests.submit
    records = transport.OBTransportTests.records
    def _worker(self, failure=None):
        with patch.object(self.desktop, "SYNC_API_KEY", "isolated-sync-test-key"):
            return catalog.CatalogSyncTests._worker(self, failure=failure)

    def request_desktop_delete(self, sync_id):
        connection = self.desktop.get_db_connection()
        try:
            row = connection.execute("SELECT * FROM shifts WHERE sync_id=?", (sync_id,)).fetchone()
        finally:
            connection.close()
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        self.addCleanup(window.deleteLater)
        window.table_shifts = self.desktop.QTableWidget(1, 1)
        window.table_shifts.setItem(0, 0, self.desktop.QTableWidgetItem(str(row["id"])))
        window.table_shifts.selectRow(0)
        with patch.object(self.desktop.QMessageBox, "question",
                          return_value=self.desktop.QMessageBox.StandardButton.Yes), \
                patch.object(self.desktop.QMessageBox, "information") as information, \
                patch.object(window, "load_shifts_history"), \
                patch.object(window, "background_sync_with_cloud"):
            window.delete_shift()
        self.assertIn("очеред", information.call_args.args[2])

    def test_offline_delete_keeps_row_then_server_rechecks_newly_closed_status(self):
        import requests
        self.submit("offline-delete", "work", "Travel B", 1)
        self._worker()
        self.request_desktop_delete("offline-delete")
        original_post = self.server_client.post

        def disconnected(path, **kwargs):
            if path == "/sync/push":
                raise requests.ConnectionError("isolated offline deletion")
            return original_post(path, **kwargs)

        with patch.object(self.server_client, "post", side_effect=disconnected):
            with self.assertRaises(requests.ConnectionError):
                self._worker()
        connection = self.desktop.get_db_connection()
        try:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM shifts WHERE sync_id='offline-delete'").fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT state FROM shift_sync_outbox WHERE sync_id='offline-delete'").fetchone()[0], "pending")
        finally:
            connection.close()
        connection = self.server.get_db_connection()
        try:
            connection.execute("UPDATE incoming_submissions SET invoiced=1 WHERE sync_id='offline-delete'")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self._worker()["conflicts"], 1)
        connection = self.desktop.get_db_connection()
        try:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM shifts WHERE sync_id='offline-delete'").fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT state FROM shift_sync_outbox WHERE sync_id='offline-delete'").fetchone()[0], "conflict")
        finally:
            connection.close()
        self.assertEqual(len(self.records()), 1)

    def test_allowed_desktop_delete_only_removes_after_ack(self):
        for kind in ("work", "ob"):
            with self.subTest(kind=kind):
                sync_id = "allowed-desktop-delete-" + kind
                self.submit(sync_id, kind, "Travel B", 1)
                self._worker()
                self.request_desktop_delete(sync_id)
                connection = self.desktop.get_db_connection()
                try:
                    self.assertEqual(connection.execute(
                        "SELECT COUNT(*) FROM shifts WHERE sync_id=?", (sync_id,)).fetchone()[0], 1)
                finally:
                    connection.close()
                self._worker()
                connection = self.desktop.get_db_connection()
                try:
                    self.assertEqual(connection.execute(
                        "SELECT COUNT(*) FROM shifts WHERE sync_id=?", (sync_id,)).fetchone()[0], 0)
                    self.assertEqual(connection.execute(
                        "SELECT COUNT(*) FROM shift_sync_outbox WHERE sync_id=? AND state='pending'",
                        (sync_id,)).fetchone()[0], 0)
                finally:
                    connection.close()
        self.assertEqual(self.records(), [])

    def test_lost_delete_ack_keeps_local_row_until_server_tombstone_arrives(self):
        import requests
        self.submit("lost-delete-ack", "work", "Travel B", 1)
        self._worker()
        self.request_desktop_delete("lost-delete-ack")
        with self.assertRaises(requests.ConnectionError):
            self._worker(failure="lost_shift_ack")
        connection = self.desktop.get_db_connection()
        try:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM shifts WHERE sync_id='lost-delete-ack'").fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT state FROM shift_sync_outbox WHERE sync_id='lost-delete-ack'").fetchone()[0], "pending")
        finally:
            connection.close()
        self.assertEqual(self.records(), [])
        self._worker()
        connection = self.desktop.get_db_connection()
        try:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM shifts WHERE sync_id='lost-delete-ack'").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM shift_sync_outbox WHERE sync_id='lost-delete-ack'").fetchone()[0], 0)
        finally:
            connection.close()

    def test_rejected_stale_desktop_delete_restores_protected_row_and_stops_retrying(self):
        self.submit("closed-while-offline", "work", "Travel B", 1)
        self._worker()
        connection = self.desktop.get_db_connection()
        try:
            row = connection.execute("SELECT * FROM shifts WHERE sync_id='closed-while-offline'").fetchone()
            self.desktop.enqueue_shift_operation(
                connection.cursor(), row["sync_id"], "delete", row["sync_version"], {"record_type": "work"})
            connection.execute("DELETE FROM shifts WHERE sync_id=?", (row["sync_id"],))
            connection.commit()
        finally:
            connection.close()
        connection = self.server.get_db_connection()
        try:
            connection.execute("UPDATE incoming_submissions SET invoiced=1 WHERE sync_id='closed-while-offline'")
            connection.commit()
        finally:
            connection.close()
        result = self._worker()
        self.assertEqual(result["conflicts"], 1)
        for _ in range(2):
            connection = self.desktop.get_db_connection()
            try:
                row = connection.execute("SELECT * FROM shifts WHERE sync_id='closed-while-offline'").fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["invoiced"], 1)
                self.assertEqual(connection.execute(
                    "SELECT state FROM shift_sync_outbox WHERE sync_id='closed-while-offline'").fetchone()[0], "conflict")
            finally:
                connection.close()
            self._worker()
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["invoiced"], 1)

    def test_all_delete_routes_reject_invoiced_work_and_ob_without_changes(self):
        for kind in ("work", "ob"):
            self.submit("protected-" + kind, kind, "Travel B", 1)
        connection = self.server.get_db_connection()
        self.addCleanup(connection.close)
        connection.execute("UPDATE incoming_submissions SET invoiced=1")
        connection.commit()
        before = [tuple(r) for r in connection.execute("SELECT * FROM incoming_submissions ORDER BY id")]
        segments = [tuple(r) for r in connection.execute("SELECT * FROM ob_segments ORDER BY id")]
        sequence = connection.execute("SELECT * FROM sync_metadata").fetchall()
        connection.close()
        for row in self.records():
            for path, payload, headers in (
                ("/delete-shift", {"sync_id": row["sync_id"], "version": row["version"],
                                   "operation_id": "blocked-web-" + kind}, {}),
                ("/delete-cloud-shift", {"sync_id": row["sync_id"]},
                 {"X-Sync-Key": "isolated-sync-test-key"}),
                ("/delete-cloud-shift", {"date": row["date"], "employee": row["employee"],
                                        "object_name": row["object_name"], "hours": row["hours"]},
                 {"X-Sync-Key": "isolated-sync-test-key"}),
                ("/sync/push", {"changes": [{"sync_id": row["sync_id"], "base_version": row["version"],
                                            "operation_id": "blocked-sync-" + row["sync_id"],
                                            "operation": "delete", "shift": {"record_type": kind}}]},
                 {"X-Sync-Key": "isolated-sync-test-key"}),
            ):
                with self.subTest(kind=row["submission_type"], path=path, payload=payload):
                    if path == "/sync/push":
                        payload["changes"][0]["shift"]["record_type"] = row["submission_type"]
                    response = self.server_client.post(path, json=payload, headers=headers)
                    if path == "/sync/push":
                        self.assertEqual(response.status_code, 200, response.get_json())
                        result = response.get_json()["results"][0]
                        self.assertFalse(result["ok"])
                        self.assertTrue(result["conflict"])
                        self.assertEqual(result["reason"], "invoiced")
                        self.assertFalse(result["current"]["deleted"])
                    else:
                        self.assertIn(response.status_code, (400, 409), response.get_json())
                        self.assertIn("работ", response.get_json()["error"])
        connection = self.server.get_db_connection()
        self.assertEqual([tuple(r) for r in connection.execute("SELECT * FROM incoming_submissions ORDER BY id")], before)
        self.assertEqual([tuple(r) for r in connection.execute("SELECT * FROM ob_segments ORDER BY id")], segments)
        self.assertEqual(connection.execute("SELECT * FROM sync_metadata").fetchall(), sequence)
        connection.close()

    def test_open_delete_and_deleted_retry_remain_idempotent(self):
        self.submit("open-delete", "work", "Travel B", 1)
        row = self.records()[0]
        request = {"sync_id": row["sync_id"], "version": row["version"], "operation_id": "allowed-delete"}
        for _ in range(2):
            response = self.server_client.post("/delete-shift", json=request)
            self.assertEqual(response.status_code, 200, response.get_json())
        connection = self.server.get_db_connection()
        try:
            self.assertEqual(connection.execute(
                "SELECT is_deleted FROM incoming_submissions WHERE sync_id='open-delete'").fetchone()[0], 1)
        finally:
            connection.close()


class InvoicedDesktopProtectionTests(pricing.HistoricalPricesTests):
    def test_closed_period_delete_and_edit_are_blocked_before_dialog_or_sync(self):
        self.shift(confirmed=True)
        self.window._set_period_invoiced(self.window.collect_invoice_report(), True)
        self.load_history()
        self.window.table_shifts.selectRow(0)
        before = [tuple(r) for r in self.conn.execute("SELECT * FROM shifts ORDER BY id")]
        outbox = [tuple(r) for r in self.conn.execute("SELECT * FROM shift_sync_outbox")]
        balance = [tuple(r) for r in self.conn.execute("SELECT * FROM balance")]
        with patch.object(self.desktop.QMessageBox, "question") as question, \
                patch.object(self.desktop.QMessageBox, "warning") as warning, \
                patch.object(self.desktop.QDialog, "exec") as dialog, \
                patch.object(self.window, "background_sync_with_cloud") as sync:
            self.window.delete_shift()
            self.window.edit_shift()
        self.assertEqual(warning.call_count, 2)
        question.assert_not_called()
        dialog.assert_not_called()
        sync.assert_not_called()
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT * FROM shifts ORDER BY id")], before)
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT * FROM shift_sync_outbox")], outbox)
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT * FROM balance")], balance)

    def test_open_row_deleted_normally_after_explicit_reopen(self):
        self.shift(confirmed=True)
        self.window._set_period_invoiced(self.window.collect_invoice_report(), True)
        self.window._set_period_invoiced(self.window.collect_invoice_report(), False)
        self.load_history()
        self.window.table_shifts.selectRow(0)
        with patch.object(self.desktop.QMessageBox, "question",
                          return_value=self.desktop.QMessageBox.StandardButton.Yes), \
                patch.object(self.desktop.QMessageBox, "information"), \
                patch.object(self.window, "load_shifts_history"), \
                patch.object(self.window, "background_sync_with_cloud"):
            self.window.delete_shift()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM shift_sync_outbox WHERE operation='delete' AND state='pending'").fetchone()[0], 1)
