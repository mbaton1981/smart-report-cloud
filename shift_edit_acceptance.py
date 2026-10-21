"""Local-only acceptance of the actual desktop shift edit dialog."""
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo


def verify_shift_edit(desktop, work, sync=None):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QCheckBox, QStyle, QStyleOptionSpinBox
    from ob_calculator import calculate_work_interval, load_ob_rules

    application = QApplication.instance() or QApplication([])
    import sys
    from pathlib import Path
    resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for name in ("spin-up.svg", "spin-down.svg"):
        assert not desktop.QPixmap(str(resource_root / "assets" / name)).isNull(), name
    application.setStyleSheet(desktop.DARK_THEME_QSS)
    zone = ZoneInfo(load_ob_rules()["timezone"])
    calculation = calculate_work_interval("2026-10-21", "16:00", "20:00", 200)
    assert calculation["total_hours"] == 4
    assert calculate_work_interval("2026-10-24", "22:00", "04:00", 200)["total_hours"] == 7
    assert calculate_work_interval("2026-03-28", "22:00", "04:00", 200)["total_hours"] == 5
    desktop.init_db_once()
    connection = desktop.get_db_connection()
    try:
        assert connection.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] == 0
        connection.execute("INSERT OR IGNORE INTO companies(name) VALUES ('ISOLATED SHIFT EDIT')")
        connection.execute("INSERT INTO employees(name,salary_rate,is_active) VALUES ('ISOLATED EMPLOYEE',200,1)")
        connection.execute("""INSERT INTO objects(company,name,rate,transport_rate,is_active)
            VALUES ('ISOLATED SHIFT EDIT','ISOLATED OBJECT',300,450,1)""")
        ids = []
        for kind, hours in (("work", 8), ("ob", 4)):
            sync_id = "isolated-edit-" + kind
            ids.append(connection.execute("""
                INSERT INTO shifts(date,employee,company,object_name,hours,rate,transport,comment,
                    sync_id,sync_version,entry_type,start_time,end_time,ob_base_rate)
                VALUES ('2026-10-21','ISOLATED EMPLOYEE','ISOLATED SHIFT EDIT','ISOLATED OBJECT',
                        ?,?,0,'Original note',?,0,?,?,?,?)
            """, (hours, 300 if kind == "work" else 0, sync_id, kind,
                  "16:00" if kind == "ob" else None, "20:00" if kind == "ob" else None,
                  200 if kind == "ob" else None)).lastrowid)
            if kind == "ob":
                desktop.replace_ob_segments(connection.cursor(), sync_id, calculation["segments"])
            if sync:
                desktop.enqueue_shift_operation(connection.cursor(), sync_id, "upsert", 0, {
                    "date": "2026-10-21", "employee": "ISOLATED EMPLOYEE",
                    "company": "ISOLATED SHIFT EDIT", "object_name": "ISOLATED OBJECT",
                    "hours": hours, "rate": 300 if kind == "work" else 0, "transport": 0,
                    "comment": "Original note", "invoiced": 0, "record_type": kind,
                    **({"start_time": "16:00", "end_time": "20:00"} if kind == "ob" else {}),
                })
        connection.commit()
    finally:
        connection.close()

    window = desktop.SmartReportApp.__new__(desktop.SmartReportApp)
    desktop.QMainWindow.__init__(window)
    window.table_shifts = desktop.QTableWidget(1, 1)
    window.load_shifts_history = lambda: None
    window.background_sync_with_cloud = lambda: None
    def row(local_id):
        connection = desktop.get_db_connection()
        try:
            return dict(connection.execute("SELECT * FROM shifts WHERE id=?", (local_id,)).fetchone())
        finally:
            connection.close()
    def segments():
        connection = desktop.get_db_connection()
        try:
            return [tuple(item) for item in connection.execute("SELECT * FROM ob_segments ORDER BY id")]
        finally:
            connection.close()
    if sync:
        sync()
        sync()
    baseline_segments = segments()
    before_sync = {}
    outcomes = []
    try:
        for local_id in ids:
            before = row(local_id)
            before_sync[local_id] = before
            window.table_shifts.setItem(0, 0, desktop.QTableWidgetItem(str(local_id)))
            window.table_shifts.selectRow(0)
            def inspect(dialog):
                travel = dialog.findChild(QCheckBox, "edit_shift_transport")
                assert travel and not travel.isChecked() and travel.isEnabled()
                dialog.show()
                application.processEvents()
                for name in ("edit_shift_hours", "edit_shift_rate"):
                    spin = dialog.findChild(desktop.QDoubleSpinBox, name)
                    if before["entry_type"] == "work":
                        original_value = spin.value()
                        option = QStyleOptionSpinBox()
                        spin.initStyleOption(option)
                        up = spin.style().subControlRect(
                            QStyle.ComplexControl.CC_SpinBox, option, QStyle.SubControl.SC_SpinBoxUp, spin)
                        down = spin.style().subControlRect(
                            QStyle.ComplexControl.CC_SpinBox, option, QStyle.SubControl.SC_SpinBoxDown, spin)
                        assert up.width() >= 24 and down.width() >= 24
                        QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=up.center())
                        assert spin.value() > original_value
                        QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=down.center())
                        assert spin.value() == original_value
                QTest.mouseClick(travel, Qt.MouseButton.LeftButton)
                assert travel.isChecked()
                dialog.grab().save(str(work / (before["entry_type"] + "-edit.png")))
                return desktop.QDialog.DialogCode.Accepted
            with patch.object(desktop.QDialog, "exec", inspect), \
                    patch.object(desktop, "calculate_work_interval", side_effect=AssertionError("No metadata recalculation")), \
                    patch.object(desktop.QMessageBox, "warning", side_effect=AssertionError("Unexpected warning")), \
                    patch.object(desktop.QMessageBox, "information"):
                window.edit_shift()
            after = row(local_id)
            allowed = {"transport", "work_rate_cache", "work_transport_cache"}
            assert after["transport"] == 1
            assert {key: value for key, value in before.items() if key not in allowed} == {
                key: value for key, value in after.items() if key not in allowed}
            def reopen(dialog):
                travel = dialog.findChild(QCheckBox, "edit_shift_transport")
                assert travel and travel.isChecked()
                return desktop.QDialog.DialogCode.Rejected
            with patch.object(desktop.QDialog, "exec", reopen), \
                    patch.object(desktop, "calculate_work_interval", side_effect=AssertionError("No reopen recalculation")):
                window.edit_shift()
            outcomes.append({"kind": before["entry_type"], "transport": 1, "other_fields_unchanged": True,
                             "reopened_checked": True, "local_id": local_id})
        assert segments() == baseline_segments
        connection = desktop.get_db_connection()
        try:
            assert connection.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] == 2
            assert connection.execute("SELECT COUNT(*) FROM shift_sync_outbox WHERE state IN ('pending','blocked')").fetchone()[0] == 2
        finally:
            connection.close()
        if sync:
            sync()
            sync()
            sync()
            for local_id in ids:
                after = row(local_id)
                assert after["transport"] == 1
                for key in ("date", "employee", "company", "object_name", "hours", "rate", "start_time", "end_time", "ob_base_rate", "comment"):
                    assert after[key] == before_sync[local_id][key], key
                window.table_shifts.setItem(0, 0, desktop.QTableWidgetItem(str(local_id)))
                with patch.object(desktop.QDialog, "exec", reopen), \
                        patch.object(desktop, "calculate_work_interval", side_effect=AssertionError("No synced reopen recalculation")):
                    window.edit_shift()
            assert segments() == baseline_segments
            connection = desktop.get_db_connection()
            try:
                assert connection.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] == 2
                remaining = [dict(item) for item in connection.execute(
                    "SELECT sync_id,state,base_version FROM shift_sync_outbox WHERE state IN ('pending','blocked','conflict')")]
                assert not remaining, remaining
            finally:
                connection.close()
        return {"timezone": str(zone), "stockholm_interval": True, "dst_spring_and_autumn": True,
                "spin_mouse_arrows": True, "transport_checkbox": True, "ob_segments_byte_unchanged": True,
                "shifts": outcomes, "synchronized_without_duplicate": bool(sync)}
    finally:
        window.close()
        window.deleteLater()
