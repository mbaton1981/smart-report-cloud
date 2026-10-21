"""Explicit offline acceptance on retained PDFs; writes only a NEW evidence directory."""
import argparse
import json
import os
import socket
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

from offline_review import OfflineReviewStore, ReviewError, digest
from offline_review_ui import OfflineReviewWindow


def hashes(root):
    return {str(path.relative_to(root)): digest(path.read_bytes())
            for path in root.rglob("*") if path.is_file()}


def answer_transfer(window, answer):
    timer = QTimer()
    observed = []

    def respond():
        box = QApplication.activeModalWidget()
        if isinstance(box, QMessageBox):
            observed.append(box.standardButton(box.defaultButton()))
            button = box.button(answer)
            if button is None:
                box.reject()
            else:
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            timer.stop()

    timer.timeout.connect(respond)
    timer.start(10)
    try:
        QTest.mouseClick(window.transfer_button, Qt.MouseButton.LeftButton)
    finally:
        timer.stop()
    assert observed == [QMessageBox.StandardButton.No], "Expected default-No confirmation dialog"


def run(source: Path, output: Path):
    before = hashes(source)
    output.mkdir(parents=False)
    app = QApplication.instance() or QApplication([])
    checks = []
    with patch.object(socket.socket, "connect", side_effect=AssertionError("Network prohibited")):
        store = OfflineReviewStore(output / "acceptance-profile")
        try:
            store.import_snapshot(source)
            assert len(store.documents()) == 5
            window = OfflineReviewWindow(store)
            window.show()
            app.processEvents()
            def capture(name):
                QTest.qWait(40)
                window.gallery.preview.fit_width()
                app.processEvents()
                assert window.gallery.width() <= window.preview_host.width()
                assert window.gallery.preview.transform().m11() > 0.4
                assert window.grab().save(str(output / name))

            assert store.base(1)["payment_reference"] == "314469748389051"
            source_ref = json.loads(store.conn.execute(
                "SELECT source_json FROM documents WHERE id=1").fetchone()[0])
            assert source_ref["result"]["fields"]["payment_reference"] is None
            checks.append("Parkeringsservice: reference 314469748389051 in overlay; source remains null.")
            for index, doc in enumerate(store.documents()):
                window.selector.setCurrentIndex(index)
                app.processEvents()
                assert window.gallery is not None and window.original_available
                assert window.object.currentData() is None
                assert window.kind.currentData() is None
                if doc["id"] == 4:
                    assert not window.transfer_button.isEnabled()
                    capture("nordea-blocked.png")
                    _, forged = store.latest(4)
                    forged.update(confirmed_type="invoice", original_checked=True, object_id=1,
                                  supplier_name="Nordea", invoice_number="19778426",
                                  document_date="2026-10-11", currency_code="SEK",
                                  net_amount="2482.00", vat_amount="0.00", amount_due="2482.00")
                    revision = store.save(4, forged, 0)
                    try:
                        store.transfer(4, revision)
                    except ReviewError as error:
                        assert "классификация" in str(error)
                    else:
                        raise AssertionError("Stale Nordea invoice accepted")
                    window.select_document()
                    checks.append("Nordea: old invoice label, manual override and checked original cannot transfer.")
                    continue
                kind = store.base(doc["id"])["document_type"]
                if doc["id"] == 2:
                    assert "Вычислено" in window.origins["net_amount"].text()
                    assert window.inputs["invoice_number"].text() == ""
                    assert window.inputs["amount_due"].text() == ""
                    assert "Följesedel" in window.origins["document_date"].text()
                if doc["id"] == 5:
                    assert len(window.gallery._pages) == 2
                    window.gallery.show_page(1)
                    assert window.object.currentData() is None
                    window.gallery.show_page(0)
                if doc["id"] == 1:
                    window.inputs["payment_reference"].selectAll()
                    QTest.keyClicks(window.inputs["payment_reference"], "314469748389051")
                window.kind.setCurrentIndex(window.kind.findData(kind))
                window.object.setCurrentIndex(window.object.findData(1 if doc["id"] != 5 else 2))
                window.checked.setChecked(True)
                assert window.save_edits()
                assert store.card(doc["id"]) is None
                if doc["id"] in (1, 2, 5):
                    capture(f"draft-{doc['id']}-review.png")
                answer_transfer(window, QMessageBox.StandardButton.No)
                assert store.card(doc["id"]) is None
                answer_transfer(window, QMessageBox.StandardButton.Yes)
                card = store.card(doc["id"])
                assert card is not None
                assert digest(store.original(doc["id"]).read_bytes()) == card["sha256"]
                revision, _ = store.latest(doc["id"])
                assert store.transfer(doc["id"], revision)["client_id"] == card["client_id"]
                checks.append(f"Draft {doc['id']}: save -> No=0 -> Yes=1; repeat same ID; PDF hash matches.")
            receipt = store.card(2)
            assert receipt["debt_amount"] == "0.00"
            assert json.loads(receipt["payload_json"])["invoice_number"] is None
            cards = [dict(row) for row in store.conn.execute("SELECT * FROM register ORDER BY id")]
            assert len(cards) == 4
            window.close()
        finally:
            store.close()
        reopened = OfflineReviewStore(output / "acceptance-profile")
        try:
            for card in cards:
                assert reopened.transfer(card["document_id"], card["revision"]) == card
            checks.append("Reopen after lost acknowledgement: still exactly 4 test cards.")
        finally:
            reopened.close()
        demo = OfflineReviewStore(output / "desktop-profile")
        try:
            demo.import_snapshot(source)
            assert demo.conn.execute("SELECT COUNT(*) FROM register").fetchone()[0] == 0
        finally:
            demo.close()
    assert before == hashes(source)
    report = {"network": "blocked", "source_files_unchanged": True,
              "source_hashes": before, "checks": checks, "test_cards": cards,
              "demo_cards": 0, "original_pdfs": 5, "pages": 6}
    (output / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    print("PASS: 5 PDFs / 6 pages; 4 approved test cards; Nordea blocked; receipt debt 0.")
    print("PASS: default-No, originals, revisions, repeated transfer and reopened register.")
    print("PASS: network blocked; source directory hashes unchanged; demo profile has 0 cards.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.snapshot.resolve(), args.output.resolve())
