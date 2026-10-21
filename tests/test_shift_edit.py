import unittest
from pathlib import Path
from unittest.mock import patch

import test_ob_transport as transport
import test_catalog_sync as catalog


class ShiftEditTests(unittest.TestCase):
    setUpClass = classmethod(transport.OBTransportTests.setUpClass.__func__)
    tearDownClass = classmethod(transport.OBTransportTests.tearDownClass.__func__)
    setUp = transport.OBTransportTests.setUp
    _add_company = transport.OBTransportTests._add_company
    submit = transport.OBTransportTests.submit
    records = transport.OBTransportTests.records
    _worker = catalog.CatalogSyncTests._worker

    def test_actual_editor_binary_checkbox_arrows_saved_interval_and_no_recalculation(self):
        from shift_edit_acceptance import verify_shift_edit
        result = verify_shift_edit(self.desktop, Path(self._case.name))
        self.assertEqual(result["timezone"], "Europe/Stockholm")
        self.assertTrue(result["ob_segments_byte_unchanged"])
        self.assertEqual(len(result["shifts"]), 2)

    def test_editor_full_sync_existing_records_retries_without_duplicate(self):
        from shift_edit_acceptance import verify_shift_edit
        connection = self.server.get_db_connection()
        connection.execute("INSERT INTO meta_companies(name) VALUES ('ISOLATED SHIFT EDIT')")
        connection.execute("INSERT INTO meta_employees(name,salary_rate,is_active) VALUES ('ISOLATED EMPLOYEE',200,1)")
        connection.execute("""INSERT INTO meta_objects(company,name,markning,rate,transport_rate,is_active)
            VALUES ('ISOLATED SHIFT EDIT','ISOLATED OBJECT','isolated',300,450,1)""")
        connection.commit()
        connection.close()
        calls = []
        def sync():
            calls.append(1)
            try:
                return self._worker(failure="lost_shift_ack" if len(calls) == 3 else None)
            except self.desktop.requests.ConnectionError:
                self.assertEqual(len(calls), 3, "Only the simulated lost ACK is expected")
        result = verify_shift_edit(self.desktop, Path(self._case.name), sync=sync)
        self.assertTrue(result["synchronized_without_duplicate"])
        connection = self.server.get_db_connection()
        self.assertEqual(connection.execute(
            "SELECT COUNT(*),SUM(transport) FROM incoming_submissions").fetchone()[:], (2, 2))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM ob_segments").fetchone()[0], 2)
        connection.close()

    def test_transport_only_sync_preserves_stored_server_ob_even_if_salary_changes(self):
        self.submit("preserve-ob", "ob", "Travel B", 0)
        connection = self.server.get_db_connection()
        before = dict(connection.execute("SELECT * FROM incoming_submissions WHERE sync_id='preserve-ob'").fetchone())
        segments = [tuple(row) for row in connection.execute("SELECT * FROM ob_segments ORDER BY id")]
        connection.execute("UPDATE meta_employees SET salary_rate=333 WHERE name='Travel Test'")
        connection.commit()
        connection.close()
        payload = {key: before[key] for key in (
            "date", "employee", "company", "object_name", "hours", "rate", "transport",
            "comment", "invoiced", "start_time", "end_time")}
        payload.update(transport=1, record_type="ob")
        request = {"changes": [{"sync_id": "preserve-ob", "operation_id": "transport-only",
                               "operation": "upsert", "base_version": before["version"], "shift": payload}]}
        with patch.object(self.server, "calculate_work_interval", side_effect=AssertionError("No OB recalculation")):
            for _ in range(2):
                response = self.server_client.post("/sync/push", json=request,
                    headers={"X-Sync-Key": "isolated-sync-test-key"})
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertTrue(response.get_json()["results"][0]["ok"])
        connection = self.server.get_db_connection()
        after = dict(connection.execute("SELECT * FROM incoming_submissions WHERE sync_id='preserve-ob'").fetchone())
        self.assertEqual(after["transport"], 1)
        for key in ("date", "hours", "start_time", "end_time", "ob_base_rate", "rate"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM ob_segments ORDER BY id")], segments)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM incoming_submissions").fetchone()[0], 1)
        connection.close()

    def test_web_transport_only_edit_preserves_segment_rows(self):
        self.submit("web-preserve-ob", "ob", "Travel B", 0)
        before = self.records()[0]
        connection = self.server.get_db_connection()
        segments = [tuple(row) for row in connection.execute("SELECT * FROM ob_segments ORDER BY id")]
        connection.close()
        with patch.object(self.server, "calculate_work_interval", side_effect=AssertionError("No OB recalculation")):
            response = self.server_client.post("/edit-shift", json={
                "sync_id": before["sync_id"], "version": before["version"],
                "operation_id": "web-transport-only", "transport": 1})
        self.assertEqual(response.status_code, 200, response.get_json())
        connection = self.server.get_db_connection()
        self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM ob_segments ORDER BY id")], segments)
        connection.close()

    def test_changed_ob_interval_still_recalculates(self):
        self.submit("changed-ob", "ob", "Travel B", 0)
        before = self.records()[0]
        with patch.object(self.server, "calculate_work_interval", wraps=self.server.calculate_work_interval) as calculate:
            response = self.server_client.post("/edit-shift", json={
                "sync_id": before["sync_id"], "version": before["version"],
                "operation_id": "new-interval", "start_time": "17:00", "end_time": "22:00", "transport": 1})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(calculate.call_count, 1)
        self.assertEqual(response.get_json()["hours"], 5)

    def test_unrelated_remote_operation_does_not_acknowledge_pending_local_edit(self):
        from types import SimpleNamespace
        connection = self.desktop.get_db_connection()
        connection.execute("""INSERT INTO shifts(date,employee,company,object_name,hours,rate,transport,
            sync_id,sync_version,entry_type) VALUES ('2026-10-21','Travel Test','Renatur','Travel B',
            8,300,1,'real-conflict',1,'work')""")
        payload = {"date": "2026-10-21", "employee": "Travel Test", "company": "Renatur",
                   "object_name": "Travel B", "hours": 8, "rate": 300, "transport": 1,
                   "comment": "", "record_type": "work"}
        self.desktop.enqueue_shift_operation(connection.cursor(), "real-conflict", "upsert", 1, payload)
        remote = {**payload, "sync_id": "real-conflict", "version": 2, "transport": 0,
                  "last_operation_id": "some-other-editor", "invoiced": 0}
        self.desktop.SmartReportApp.apply_remote_shift(SimpleNamespace(), connection.cursor(), remote)
        self.assertEqual(connection.execute("SELECT state FROM shift_sync_outbox").fetchone()[0], "conflict")
        connection.close()
