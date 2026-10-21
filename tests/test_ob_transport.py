import csv
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfReader

import test_catalog_sync as catalog


class OBTransportTests(unittest.TestCase):
    setUpClass = classmethod(catalog.CatalogSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog.CatalogSyncTests.tearDownClass.__func__)
    _add_company = catalog.CatalogSyncTests._add_company

    def setUp(self):
        catalog.CatalogSyncTests.setUp(self)
        self.addCleanup(self._case.cleanup)
        conn = self.server.get_db_connection()
        conn.executescript("""
            ALTER TABLE incoming_submissions ADD COLUMN start_time TEXT;
            ALTER TABLE incoming_submissions ADD COLUMN end_time TEXT;
            ALTER TABLE incoming_submissions ADD COLUMN ob_base_rate REAL;
            CREATE TABLE ob_segments (
                id INTEGER PRIMARY KEY, shift_id TEXT, segment_date TEXT,
                start_time TEXT, end_time TEXT, hours REAL, category TEXT,
                premium_percent REAL, multiplier REAL, base_rate REAL,
                calculated_rate REAL, amount REAL, created_at TEXT, updated_at TEXT);
            INSERT INTO meta_employees(name,salary_rate,is_active) VALUES ('Travel Test',200,1);
            INSERT INTO meta_objects(company,name,markning,rate,transport_rate,is_active)
                VALUES ('Renatur','Travel A','travel-a',300,450,1),
                       ('Renatur','Travel B','travel-b',300,450,1);
        """)
        conn.commit()
        conn.close()
        with self.server_client.session_transaction() as session:
            session.update(user_id=1, role="admin")

    def submit(self, request_id, kind, object_name, transport):
        data = {
            "request_id": request_id, "date": "2026-10-20", "employee": "Travel Test",
            "company": "Renatur", "object_name": object_name, "submission_type": kind,
            "transport": transport, "hours": 8, "start_time": "18:00", "end_time": "22:00",
        }
        response = self.server_client.post("/submit-shift", json=data)
        self.assertEqual(response.status_code, 201, response.get_json())
        return data

    def records(self):
        response = self.server_client.post("/check-employee-shifts", json={
            "employee": "Travel Test", "month": "2026-10"})
        self.assertEqual(response.status_code, 200)
        return response.get_json()["recent_shifts"]

    def test_two_objects_two_independent_trips_ob_segments_and_reopening(self):
        self.submit("work-one", "work", "Travel A", 1)
        data = self.submit("ob-one", "ob", "Travel B", 1)
        repeated = self.server_client.post("/submit-shift", json=data)
        self.assertTrue(repeated.get_json()["idempotent"])
        rows = self.records()
        self.assertEqual([(row["object_name"], row["transport"]) for row in rows],
                         [("Travel A", 1), ("Travel B", 1)])
        ob = rows[1]
        self.assertEqual(ob["hours"], 4)
        self.assertEqual(len(ob["ob_segments"]), 2)
        self.assertTrue(all("transport" not in segment for segment in ob["ob_segments"]))
        conn = self.server.get_db_connection()
        self.assertEqual(conn.execute("SELECT SUM(transport) FROM incoming_submissions").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ob_segments").fetchone()[0], 2)
        conn.close()
        edited = self.server_client.post("/edit-shift", json={
            "sync_id": ob["sync_id"], "operation_id": "comment-only", "version": ob["version"],
            "comment": "Edited without changing travel",
        })
        self.assertEqual(edited.status_code, 200, edited.get_json())
        updated = next(row for row in self.records() if row["sync_id"] == ob["sync_id"])
        self.assertEqual(updated["transport"], 1)
        edited = self.server_client.post("/edit-shift", json={
            "sync_id": ob["sync_id"], "operation_id": "uncheck", "version": updated["version"],
            "transport": 0,
        })
        self.assertEqual(edited.status_code, 200, edited.get_json())
        self.assertEqual([row["transport"] for row in self.records()], [1, 0])

    def test_validation_object_required_and_invalid_transport_not_silently_cleared(self):
        for transport in (-1, 10001, "nan", "inf", "invalid"):
            response = self.server_client.post("/submit-shift", json={
                "request_id": "bad", "date": "2026-10-20", "employee": "Travel Test",
                "company": "Renatur", "object_name": "Travel B", "submission_type": "ob",
                "start_time": "18:00", "end_time": "22:00", "transport": transport,
            })
            self.assertEqual(response.status_code, 400, (transport, response.get_json()))
        response = self.server_client.post("/submit-shift", json={
            "request_id": "no-object", "date": "2026-10-20", "employee": "Travel Test",
            "company": "Renatur", "object_name": "", "submission_type": "ob",
            "start_time": "18:00", "end_time": "22:00", "transport": 1,
        })
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(self.records(), [])

    def test_desktop_editor_keeps_travel_visible_for_ob_and_restores_after_absence(self):
        conn = self.desktop.get_db_connection()
        conn.execute("INSERT INTO employees(name,salary_rate,is_active) VALUES ('Travel Test',200,1)")
        conn.execute("""INSERT INTO objects(company,name,rate,transport_rate,is_active)
            VALUES ('Renatur','Travel B',300,450,1)""")
        shift_id = conn.execute("""INSERT INTO shifts(date,employee,company,object_name,hours,
            rate,transport,sync_id,sync_version,entry_type)
            VALUES ('2026-10-20','Travel Test','Renatur','Travel B',8,300,2,'ui-travel',1,'work')""").lastrowid
        conn.commit()
        conn.close()
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.table_shifts = self.desktop.QTableWidget(1, 1)
        window.table_shifts.setItem(0, 0, self.desktop.QTableWidgetItem(str(shift_id)))
        window.table_shifts.selectRow(0)

        def inspect(dialog):
            kind = next(combo for combo in dialog.findChildren(self.desktop.QComboBox)
                        if combo.findData("ob") >= 0 and combo.findData("work") >= 0)
            form = dialog.layout()
            travel = next(widget for widget in dialog.findChildren(self.desktop.QDoubleSpinBox)
                          if form.labelForField(widget).text() == "Транспорт:")
            kind.setCurrentIndex(kind.findData("ob"))
            self.assertFalse(travel.isHidden())
            self.assertEqual(travel.value(), 2)
            travel.setValue(3)
            kind.setCurrentIndex(kind.findData("sick"))
            self.assertEqual(travel.value(), 0)
            kind.setCurrentIndex(kind.findData("ob"))
            self.assertEqual(travel.value(), 3)
            kind.setCurrentIndex(kind.findData("work"))
            self.assertEqual(travel.value(), 3)
            return self.desktop.QDialog.DialogCode.Rejected

        try:
            with patch.object(self.desktop.QDialog, "exec", inspect):
                window.edit_shift()
            conn = self.desktop.get_db_connection()
            self.assertEqual(tuple(conn.execute("SELECT hours,transport FROM shifts WHERE id=?",
                                               (shift_id,)).fetchone()), (8, 2))
            conn.close()
        finally:
            window.close()
            window.deleteLater()

    def test_server_history_preview_pdf_and_csv_show_travel_once_per_record(self):
        self.submit("work-report", "work", "Travel A", 1)
        self.submit("ob-report", "ob", "Travel B", 1)
        conn = self.server.get_db_connection()
        report, error = self.server.fetch_arbetsdagbok_records(conn.cursor(), "Travel Test", "2026-10")
        conn.close()
        self.assertIsNone(error)
        self.assertEqual(report["total_transport"], 2)
        self.assertEqual(report["total_hours"], 12)
        self.assertEqual(len(report["shifts"]), 2)
        preview = self.server_client.get("/report-preview?employee=Travel%20Test&month=2026-10")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.get_data(as_text=True).count("Resa: 1 (denna post)"), 2)
        pdf = self.server_client.get("/download-arbetsdagbok.pdf?employee=Travel%20Test&month=2026-10")
        self.assertEqual(pdf.status_code, 200)
        text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.data)).pages)
        self.assertEqual(text.count("Resa: 1"), 2, text)
        pdf.close()
        response = self.server_client.post("/export-shifts-csv", json={
            "employee": "Travel Test", "month": "2026-10"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        rows = list(csv.DictReader(io.StringIO(response.data.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(float(row["Транспорт"]) for row in rows), 2)

    def test_sync_preserves_one_ob_trip_both_directions_and_aggregated_invoice(self):
        self.submit("work-sync", "work", "Travel A", 1)
        self.submit("ob-sync", "ob", "Travel B", 1)
        response = self.server_client.get("/sync/pull", headers={"X-Sync-Key": "isolated-sync-test-key"})
        self.assertEqual(response.status_code, 200)
        changes = response.get_json()["changes"]
        conn = self.desktop.get_db_connection()
        for change in changes:
            self.desktop.SmartReportApp.apply_remote_shift(SimpleNamespace(), conn.cursor(), change)
        conn.commit()
        ob = conn.execute("SELECT * FROM shifts WHERE entry_type='ob'").fetchone()
        self.assertEqual(ob["transport"], 1)
        self.assertEqual(conn.execute("SELECT SUM(transport) FROM shifts").fetchone()[0], 2)
        self.assertEqual(len(self.desktop.get_ob_segments(conn.cursor(), ob["sync_id"])), 2)
        payload = self.desktop.shift_payload(ob)
        conn.execute("""INSERT INTO objects(company,name,markning,rate,transport_rate,sync_id,is_active)
            VALUES ('Renatur','Travel B','travel-b',300,450,'travel-b-id',1)""")
        conn.commit()
        conn.close()
        pushed = self.server_client.post("/sync/push", json={"changes": [{
            "sync_id": ob["sync_id"], "operation_id": "desktop-ob-edit", "operation": "upsert",
            "base_version": ob["sync_version"], "shift": {**payload, "comment": "Round trip"},
        }]}, headers={"X-Sync-Key": "isolated-sync-test-key"})
        self.assertEqual(pushed.status_code, 200, pushed.get_json())
        self.assertEqual(next(row for row in self.records() if row["sync_id"] == ob["sync_id"])["transport"], 1)
        window = self.desktop.SmartReportApp.__new__(self.desktop.SmartReportApp)
        self.desktop.QMainWindow.__init__(window)
        window.tab_invoice = self.desktop.QWidget()
        window.setup_invoice_tab()
        try:
            window.inv_comp_cb.setCurrentText("Renatur")
            window.inv_obj_cb.setCurrentIndex(window.inv_obj_cb.findText(
                "Travel B", self.desktop.Qt.MatchFlag.MatchContains))
            window.inv_date_from.setDate(self.desktop.QDate(2026, 10, 1))
            window.inv_date_to.setDate(self.desktop.QDate(2026, 10, 31))
            report = window._prepare_invoice_view(window.collect_invoice_report())
            self.assertEqual(report["totals"]["transport_amount"], 450)
        finally:
            window.close()
